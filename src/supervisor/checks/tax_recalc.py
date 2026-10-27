"""Verificador de recálculo de impostos (drift entre código e relatórios).

Reparsa as notas das sessões com o código ATUAL, recalcula os impostos e
compara com o que o bot reportou nos relatórios salvos (conciliacao_*.md):
problema que o código atual acha mas o relatório não mencionou = erro não
sinalizado; problema que só existe no relatório = obsoleto/falso positivo.
"""
from __future__ import annotations

import re
from pathlib import Path

from ...domain.tax import verify_invoice_taxes
from ..models import CheckContext, Finding, Severity
from ..session_files import get_sessions, short_path

_HEAD_RE = re.compile(
    r"^###\s+(?:(?P<nf>NFS-e|NFe|NFCe|CF-e|Fatura)\s+)?(?P<num>\S+?)"
    r"(?:\s+—\s+(?P<issuer>.*))?$")
_PROB_RE = re.compile(r"^- (?:🔴|🟠|🟡)\s+(?P<msg>.+)$")


def _is_info_line(msg: str) -> bool:
    """Linhas 🟡 informativas do relatório (não são problemas do TaxCheck):
    imposto mascarado calculado pela tabela e base/alíquota ilegíveis."""
    return ("mascarado(s) na nota" in msg
            or msg.startswith("Impostos identificados pelo valor"))


def _f(id_: str, severity: Severity, message: str, evidence: str,
       suggestion: str, count: int = 1, source: str = "") -> Finding:
    return Finding(id=id_, check="tax_recalc", severity=severity,
                   message=message, evidence=evidence, suggestion=suggestion,
                   count=count, source=source)


def parse_report_tax_sections(md_path: Path) -> dict[str, dict]:
    """Extrai, do relatório MD, número da nota -> problemas listados.

    Formato gravado por src/reporting/report.py (_tax_section):
      ### NFS-e 2490 — SKYFIT LTDA (R$ 1.234,56)
      - 🔴 ISSQN: base 100 × 5% ...
      - ✅ Cálculo de impostos conferido.
    """
    sections: dict[str, dict] = {}
    if not md_path.exists():
        return sections
    current: str | None = None
    in_tax = False
    for line in md_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("## "):
            in_tax = "fiscal" in line.lower()
            current = None
            continue
        m = _HEAD_RE.match(line)
        if m and in_tax:
            current = m.group("num")
            sections.setdefault(current, {"problems": [], "ok": False, "file": md_path.name})
            continue
        if current:
            pm = _PROB_RE.match(line)
            if pm and not _is_info_line(pm.group("msg").strip()):
                sections[current]["problems"].append(pm.group("msg").strip())
            elif line.startswith("- ✅ Cálculo de impostos conferido"):
                sections[current]["ok"] = True
    return sections


def merge_session_reports(session_dir: Path) -> dict[str, dict]:
    """Seções fiscais de TODOS os relatórios da sessão; o mais novo vence."""
    merged: dict[str, dict] = {}
    reports = sorted(session_dir.glob("reports/conciliacao_*.md"))
    for md in reports:
        for num, data in parse_report_tax_sections(md).items():
            merged[num] = data
    return merged


def run(ctx: CheckContext) -> tuple[list[Finding], dict[str, int]]:
    findings: list[Finding] = []
    stats = {"audits_with_tax": 0, "invoices_in_reports": 0,
             "invoices_compared": 0, "missed": 0, "stale": 0,
             "sessions_with_reports": 0}

    sessions = get_sessions(ctx)
    if not sessions or not ctx.sessions_dir:
        return findings, stats

    # audits frescos por chat (número -> problema)
    fresh: dict[str, dict[str, list[str]]] = {}
    fresh_src: dict[tuple[str, str], str] = {}
    for sf in sessions:
        for a in sf.parsed.audits:
            if not a.tax_lines:
                continue
            stats["audits_with_tax"] += 1
            if not a.checks:
                verify_invoice_taxes(a)
            num = (a.number or "").strip()
            if not num:
                continue
            bucket = fresh.setdefault(sf.chat_id, {})
            msgs = bucket.setdefault(num, [])
            for p in a.problems:
                if p.message not in msgs:
                    msgs.append(p.message)
            fresh_src.setdefault((sf.chat_id, num), short_path(sf.chat_id, sf.path))

    for chat_id, by_num in fresh.items():
        session_dir = Path(ctx.sessions_dir) / chat_id
        reports = merge_session_reports(session_dir)
        if reports:
            stats["sessions_with_reports"] += 1
        stats["invoices_in_reports"] += len(reports)
        for num, fresh_msgs in by_num.items():
            rep = reports.get(num)
            if rep is None:
                continue  # nota nunca entrou em relatório — nada a comparar
            stats["invoices_compared"] += 1
            rep_msgs = rep["problems"]
            missing = [m for m in fresh_msgs if m not in rep_msgs]
            stale = [m for m in rep_msgs if m not in fresh_msgs]
            src = f"{fresh_src.get((chat_id, num), num)} · relatório {rep.get('file', '')}"
            if missing:
                stats["missed"] += 1
                findings.append(_f(
                    "tax_recalc.missed_problem", Severity.HIGH,
                    f"Nota {num}: código atual acha divergência não sinalizada no relatório",
                    " | ".join(missing[:3])
                    + (f" (+{len(missing) - 3} mais)" if len(missing) > 3 else ""),
                    "O relatório não lista este problema — o usuário pode ter "
                    "recebido parecer incompleto (ou o texto das mensagens "
                    "mudou entre versões do código).",
                    count=len(missing), source=src,
                ))
            if stale and not missing:
                stats["stale"] += 1
                findings.append(_f(
                    "tax_recalc.stale_problem", Severity.MEDIUM,
                    f"Nota {num}: relatório lista problema que o código atual não vê mais",
                    " | ".join(stale[:3])
                    + (f" (+{len(stale) - 3} mais)" if len(stale) > 3 else ""),
                    "O relatório antigo está desatualizado em relação ao código "
                    "atual — regere o relatório dessa sessão para confirmar.",
                    count=len(stale), source=src,
                ))

    return findings, stats
