"""Verificador de replay da conciliação (drift entre código e relatórios).

Reparsa os arquivos de cada sessão, roda a conciliação determinística com o
código ATUAL e compara as inconsistências com as do relatório salvo:
divergência nova que o relatório não tinha = possível erro não sinalizado;
divergência do relatório que sumiu = obsoleto/falso positivo antigo.

Só compara inconsistências no ESCOPO do relatório (notas/transações que
aparecem na tabela de detalhes), para não acusar drift de outros meses.
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from ...domain.rules import DeterministicMatcher
from ...engine import Reconciler
from ...llm import FallbackClient
from ..models import CheckContext, Finding, Severity
from ..session_files import get_sessions


def _f(id_: str, severity: Severity, message: str, evidence: str,
       suggestion: str, count: int = 1, source: str = "") -> Finding:
    return Finding(id=id_, check="replay", severity=severity,
                   message=message, evidence=evidence, suggestion=suggestion,
                   count=count, source=source)


def parse_report_inconsistencies(md_path: Path) -> dict:
    """Extrai do relatório MD: mensagens de inconsistência + escopo.

    Retorna {"messages": [...], "txn_ids": {...}, "inv_numbers": {...},
             "has_section": bool}.
    """
    out = {"messages": [], "txn_ids": set(), "inv_numbers": set(),
           "has_section": False}
    if not md_path.exists():
        return out
    section: str | None = None
    expecting_msg = False
    for line in md_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("## "):
            if "Inconsistências detectadas" in line:
                section = "incs"
                out["has_section"] = True
                expecting_msg = False
            elif "Nenhuma inconsistência" in line:
                section = None
                out["has_section"] = True
                expecting_msg = False
            elif "Detalhes da conciliação" in line:
                section = "table"
                expecting_msg = False
            else:
                section = None
                expecting_msg = False
            continue
        if section == "incs":
            if line.startswith("### "):
                expecting_msg = True
            elif expecting_msg and line.startswith("- "):
                out["messages"].append(line[2:].strip())
                expecting_msg = False
        elif section == "table" and line.startswith("|"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) >= 9 and cells[0] not in ("Status", "") and not cells[0].startswith("---"):
                if cells[3] and cells[3] != "—":
                    out["txn_ids"].add(cells[3])
                if cells[6] and cells[6] != "—":
                    out["inv_numbers"].add(cells[6])
    return out


def _make_reconciler(cfg: dict) -> Reconciler:
    m = cfg.get("matching") or {}
    matcher = DeterministicMatcher(
        amount_tolerance=Decimal(str(m.get("amount_tolerance", "0.01"))),
        date_window_days=int(m.get("date_window_days", 15)),
        name_threshold=float(m.get("name_threshold", 0.82)),
    )
    return Reconciler(
        FallbackClient(), matcher,
        amount_tolerance=Decimal(str(m.get("amount_tolerance", "0.01"))),
        fuzzy_confidence_threshold=float(m.get("fuzzy_confidence_threshold", 0.55)),
        late_tolerance_days=int(m.get("late_tolerance_days", 0)),
        low_confidence_threshold=float(m.get("low_confidence_threshold", 0.5)),
    )


def run(ctx: CheckContext) -> tuple[list[Finding], dict[str, int]]:
    findings: list[Finding] = []
    stats = {"sessions_replayed": 0, "sessions_skipped": 0,
             "incs_fresh": 0, "incs_in_report_scope": 0, "incs_in_report": 0,
             "missed": 0, "stale": 0}

    sessions = get_sessions(ctx)
    by_chat: dict[str, list] = {}
    for sf in sessions:
        by_chat.setdefault(sf.chat_id, []).append(sf)
    if not by_chat:
        return findings, stats

    reconciler = _make_reconciler(ctx.cfg)

    for chat_id, files in sorted(by_chat.items()):
        session_dir = Path(ctx.sessions_dir) / chat_id if ctx.sessions_dir else None
        reports = sorted(session_dir.glob("reports/conciliacao_*.md")) \
            if session_dir and session_dir.is_dir() else []
        if not reports:
            stats["sessions_skipped"] += 1
            continue

        # replica os filtros do _run do bot: arquivos que não são extrato
        # de verdade (combinados / 1-2 transações) ficam fora das transações
        txns, invs = [], []
        for sf in files:
            p = sf.parsed
            n_tx, n_inv = len(p.transactions), len(p.invoices)
            is_non_extrato = (n_tx > 0 and n_inv > 0) or (0 < n_tx <= 2 and n_inv == 0)
            if not is_non_extrato:
                txns.extend(p.transactions)
            invs.extend(p.invoices)
        seen = set()
        invs_dedup = []
        for i in invs:
            k = (i.number, str(i.amount), (i.issuer or "").lower())
            if k not in seen:
                seen.add(k)
                invs_dedup.append(i)

        if not txns or not invs_dedup:
            stats["sessions_skipped"] += 1
            continue

        report = reconciler.reconcile(txns, invs_dedup)
        rep = parse_report_inconsistencies(reports[-1])
        stats["sessions_replayed"] += 1
        stats["incs_fresh"] += len(report.inconsistencies)
        stats["incs_in_report"] += len(rep["messages"])

        fresh_msgs = []
        for inc in report.inconsistencies:
            in_scope = False
            if inc.invoice and inc.invoice.number in rep["inv_numbers"]:
                in_scope = True
            if inc.transaction and inc.transaction.id in rep["txn_ids"]:
                in_scope = True
            if in_scope:
                fresh_msgs.append(inc.message)
        stats["incs_in_report_scope"] += len(fresh_msgs)

        src = f"{chat_id}/{reports[-1].name}"
        missing = [m for m in fresh_msgs if m not in rep["messages"]]
        stale = [m for m in rep["messages"] if m not in fresh_msgs]
        if missing:
            stats["missed"] += 1
            findings.append(_f(
                "replay.missed_inconsistency", Severity.HIGH,
                f"Sessão {chat_id}: conciliação atual acha divergência não "
                "sinalizada no relatório",
                " | ".join(missing[:3])
                + (f" (+{len(missing) - 3} mais)" if len(missing) > 3 else ""),
                "O relatório entregue ao usuário não mencionou estas "
                "divergências — regere o relatório e confira se o filtro de "
                "período/arquivos do bot não escondeu o alerta.",
                count=len(missing), source=src,
            ))
        if stale and not missing:
            stats["stale"] += 1
            findings.append(_f(
                "replay.stale_inconsistency", Severity.MEDIUM,
                f"Sessão {chat_id}: relatório lista divergência que o código "
                "atual não encontra mais",
                " | ".join(stale[:3])
                + (f" (+{len(stale) - 3} mais)" if len(stale) > 3 else ""),
                "O relatório antigo está desatualizado (código ou arquivos "
                "mudaram) — regere para confirmar.",
                count=len(stale), source=src,
            ))

    return findings, stats
