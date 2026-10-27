"""Verificador de sanidade das sessões do bot.

Confere o estado persistido (.estado_bot.json), sessões vazias e relatórios
órfãos (sem arquivos de entrada).
"""
from __future__ import annotations

import json
from pathlib import Path

from ..models import CheckContext, Finding, Severity


def _f(id_: str, severity: Severity, message: str, evidence: str,
       suggestion: str, count: int = 1, source: str = "") -> Finding:
    return Finding(id=id_, check="session_sanity", severity=severity,
                   message=message, evidence=evidence, suggestion=suggestion,
                   count=count, source=source)


def run(ctx: CheckContext) -> tuple[list[Finding], dict[str, int]]:
    findings: list[Finding] = []
    stats = {"sessions": 0, "bad_state": 0, "state_ahead": 0,
             "empty": 0, "orphan_reports": 0}

    if not ctx.sessions_dir or not Path(ctx.sessions_dir).is_dir():
        return findings, stats

    bad_states: list[str] = []
    ahead: list[str] = []
    empties: list[str] = []
    orphans: list[str] = []

    for chat_dir in sorted(p for p in Path(ctx.sessions_dir).iterdir() if p.is_dir()):
        if ctx.only_chat and chat_dir.name != ctx.only_chat:
            continue
        stats["sessions"] += 1
        chat = chat_dir.name
        inputs = [f for f in chat_dir.iterdir()
                  if f.is_file() and f.name != ".estado_bot.json"]
        reports_dir = chat_dir / "reports"
        reports = list(reports_dir.glob("conciliacao_*")) if reports_dir.is_dir() else []

        estado = chat_dir / ".estado_bot.json"
        if estado.exists():
            try:
                data = json.loads(estado.read_text(encoding="utf-8"))
                executed = data.get("executed_file_count")
                if isinstance(executed, int) and executed > len(inputs):
                    stats["state_ahead"] += 1
                    ahead.append(f"{chat} (executed={executed}, arquivos={len(inputs)})")
            except (json.JSONDecodeError, OSError):
                stats["bad_state"] += 1
                bad_states.append(chat)

        if not inputs and not reports:
            stats["empty"] += 1
            empties.append(chat)
        elif not inputs and reports:
            stats["orphan_reports"] += 1
            orphans.append(chat)

    if bad_states:
        findings.append(_f(
            "session_sanity.bad_state", Severity.MEDIUM,
            "Arquivo de estado da sessão corrompido (.estado_bot.json)",
            ", ".join(bad_states),
            "O bot ignora estado corrompido e recria a sessão; apague o "
            ".estado_bot.json dessas sessões para normalizar.",
            count=len(bad_states),
        ))
    if ahead:
        findings.append(_f(
            "session_sanity.state_ahead", Severity.LOW,
            "Estado da sessão conta mais arquivos processados do que existem",
            ", ".join(ahead),
            "Arquivos foram removidos depois da última execução; o estado "
            "antigo não afeta a conciliação, mas pode confundir o fluxo "
            "'rodar de novo'. Apague o .estado_bot.json para resetar.",
            count=len(ahead),
        ))
    if empties:
        findings.append(_f(
            "session_sanity.empty_session", Severity.LOW,
            "Sessões sem nenhum arquivo nem relatório",
            ", ".join(empties[:15]) + (f" (+{len(empties) - 15} mais)" if len(empties) > 15 else ""),
            "Sessões vazias podem ser limpas (pasta da sessão) para liberar "
            "espaço e reduzir ruído na auditoria.",
            count=len(empties),
        ))
    if orphans:
        findings.append(_f(
            "session_sanity.orphan_reports", Severity.LOW,
            "Sessões com relatórios mas sem arquivos de entrada",
            ", ".join(orphans),
            "Os arquivos enviados foram apagados depois de conciliar; se "
            "precisar regerar o relatório, peça os arquivos de novo.",
            count=len(orphans),
        ))

    return findings, stats
