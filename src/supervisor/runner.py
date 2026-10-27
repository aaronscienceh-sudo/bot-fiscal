"""Orquestra os verificadores e gera o relatório de auditoria (MD + CSV)."""
from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

from .checks import CHECKS
from .models import AuditoriaReport, CheckContext, Severity

_SEV_EMOJI = {
    Severity.CRITICAL: "🚨",
    Severity.HIGH: "🔴",
    Severity.MEDIUM: "🟠",
    Severity.LOW: "🟡",
}
_SEV_ORDER = {Severity.CRITICAL: 0, Severity.HIGH: 1,
              Severity.MEDIUM: 2, Severity.LOW: 3}

# Checks ligados a UMA sessão (chat) — os demais (log_health, kb_consistency)
# são globais e ficam de fora quando a auditoria é ao vivo, no meio da conversa.
SESSION_CHECKS = ("parsing_quality", "tax_recalc", "replay", "session_sanity")


def discover_log_paths(root: str | Path | None = None) -> list[Path]:
    """Acha os logs do bot: bot.log/bot_live.log na raiz + logs/*.log."""
    root = Path(root) if root else Path.cwd()
    candidates: list[Path] = []
    for name in ("bot.log", "bot_live.log"):
        p = root / name
        if p.exists():
            candidates.append(p)
    logs_dir = root / "logs"
    if logs_dir.is_dir():
        candidates.extend(sorted(logs_dir.glob("*.log")))
    seen: set[Path] = set()
    out: list[Path] = []
    for p in candidates:
        rp = p.resolve()
        if rp not in seen:
            seen.add(rp)
            out.append(p)
    return out


def run_audit(cfg: dict | None = None,
              log_paths: list[Path] | None = None,
              sessions_dir: str | Path | None = None,
              only: list[str] | None = None,
              root: str | Path | None = None,
              only_chat: str | None = None) -> AuditoriaReport:
    """Roda os verificadores e monta o AuditoriaReport.

    only: lista de nomes de checks; vazio/None = todos.
    only_chat: restringe os checks de sessão a um chat (auditoria ao vivo);
               nesse caso só os SESSION_CHECKS entram, nunca os globais.
    """
    cfg = cfg or {}
    if log_paths is None:
        log_paths = discover_log_paths(root)
    if sessions_dir is None:
        sessions_dir = (cfg.get("bot", {}) or {}).get("sessions_dir", "data/sessions")
    if only_chat:
        only = [n for n in (only or SESSION_CHECKS) if n in SESSION_CHECKS] \
            or list(SESSION_CHECKS)
        log_paths = []
    ctx = CheckContext(
        cfg=cfg,
        log_paths=[Path(p) for p in log_paths],
        sessions_dir=Path(sessions_dir) if sessions_dir else None,
        reports_dir=Path((cfg.get("paths", {}) or {}).get("reports", "data/reports"))
        if cfg.get("paths") else None,
        only_chat=only_chat,
    )

    report = AuditoriaReport()
    for name, fn in CHECKS.items():
        if only and name not in only:
            continue
        findings, stats = fn(ctx)
        report.findings.extend(findings)
        report.checks_run.append(name)
        for k, v in stats.items():
            report.stats[k] = report.stats.get(k, 0) + v
    report.findings.sort(key=lambda f: (_SEV_ORDER[f.severity], f.id))
    return report


def run_session_audit(cfg: dict | None = None,
                      session_dir: str | Path | None = None,
                      only: list[str] | None = None) -> AuditoriaReport:
    """Auditoria ao vivo de UMA sessão do bot (data/sessions/<chat_id>/).

    Feita no meio da conversa (o bot chama depois de processar arquivos):
    só os checks de sessão, só este chat, sem varrer logs nem as outras sessões.
    """
    if not session_dir:
        return AuditoriaReport()
    p = Path(session_dir)
    return run_audit(cfg=cfg, log_paths=[], sessions_dir=str(p.parent),
                     only=only, only_chat=p.name)


def write_audit_csv(report: AuditoriaReport, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = ["severity", "check", "id", "count", "message",
            "evidence", "suggestion", "source"]
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for fd in report.findings:
            w.writerow([fd.severity.value, fd.check, fd.id, fd.count,
                        fd.message, fd.evidence, fd.suggestion, fd.source])
    return path


def write_audit_markdown(report: AuditoriaReport, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    sev = report.by_severity
    lines: list[str] = []
    lines.append("# Relatório de Auditoria — Supervisor do Bot Fiscal")
    lines.append("")
    lines.append(f"- **Gerado em:** {report.generated_at:%d/%m/%Y %H:%M}")
    lines.append(f"- **Verificadores:** {', '.join(report.checks_run) or '—'}")
    lines.append("")
    lines.append("## Resumo")
    lines.append("")
    lines.append("| Severidade | Achados |")
    lines.append("|---|---|")
    lines.append(f"| 🚨 Crítico | {sev['critical']} |")
    lines.append(f"| 🔴 Alto | {sev['high']} |")
    lines.append(f"| 🟠 Médio | {sev['medium']} |")
    lines.append(f"| 🟡 Baixo | {sev['low']} |")
    lines.append("")
    if report.findings:
        lines.append("## Achados")
        lines.append("")
        for n, fd in enumerate(report.findings, 1):
            emoji = _SEV_EMOJI[fd.severity]
            lines.append(f"### {n}. {emoji} [{fd.severity.value.upper()}] {fd.message}")
            lines.append(f"- **Check:** `{fd.check}` · **id:** `{fd.id}` · "
                         f"**ocorrências:** {fd.count}")
            if fd.source:
                lines.append(f"- **Fonte:** {fd.source}")
            if fd.evidence:
                lines.append(f"- **Evidência:** {fd.evidence}")
            if fd.suggestion:
                lines.append(f"- **Sugestão:** {fd.suggestion}")
            lines.append("")
    else:
        lines.append("## ✅ Nenhum problema encontrado")
        lines.append("")
    if report.stats:
        lines.append("## Métricas brutas")
        lines.append("")
        lines.append("| Métrica | Valor |")
        lines.append("|---|---|")
        for k in sorted(report.stats):
            lines.append(f"| {k} | {report.stats[k]} |")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def write_audit_report(report: AuditoriaReport, out_dir: str | Path,
                       stamp: str | None = None) -> tuple[Path, Path]:
    """Escreve CSV + Markdown em out_dir. Retorna (caminho_csv, caminho_md)."""
    out_dir = Path(out_dir)
    stamp = stamp or datetime.now().strftime("%Y%m%d_%H%M%S")
    return (write_audit_csv(report, out_dir / f"auditoria_{stamp}.csv"),
            write_audit_markdown(report, out_dir / f"auditoria_{stamp}.md"))
