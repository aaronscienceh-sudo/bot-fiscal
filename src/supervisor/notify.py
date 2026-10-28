"""Alerta do supervisor para o chat de administração.

O supervisor é sócio: detecta e relata. Aqui ele AVISA as pessoas quando o
achado é médio ou pior — pelo mesmo TelegramNotifier do bot, com o token/chat
que já vêm do .env (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID).

O alerta técnico (fonte, evidência, sugestão) é para o ADMIN. O usuário da
conversa nunca deve ver linguagem de governança: para ele existe
`build_user_message`, com no máximo uma frase acionável e simples.
Achado baixo não notifica ninguém: o relatório completo (CSV + MD) dá conta.
"""
from __future__ import annotations

import re

from .models import AuditoriaReport, Finding, Severity

_TOKEN_RE = re.compile(r"\d{8,10}:[A-Za-z0-9_-]{35}")

_SEV_EMOJI = {
    Severity.CRITICAL: "🚨",
    Severity.HIGH: "🔴",
    Severity.MEDIUM: "🟠",
    Severity.LOW: "🟡",
}
# severidade que gera alerta no chat de administração
ALERT_SEVERITIES = (Severity.CRITICAL, Severity.HIGH, Severity.MEDIUM)
# severidade cujo achado pode virar frase simples no chat do usuário
_USER_SEVERITIES = (Severity.CRITICAL, Severity.HIGH, Severity.MEDIUM)
_MAX_ITEMS = 6
_MAX_USER_FILES = 5


def _redact(texto) -> str:
    """Mesmo redact do bot: token do Telegram nunca sai em mensagem alguma."""
    return _TOKEN_RE.sub("XXXX", str(texto))


def _is_alert(fd: Finding) -> bool:
    return fd.severity in ALERT_SEVERITIES


def build_alert(report: AuditoriaReport, title: str = "SUPERVISOR DO BOT FISCAL") -> str | None:
    """Mensagem de alerta para achados críticos/alto. None se não há nenhum."""
    gravis = [f for f in report.findings if _is_alert(f)]
    if not gravis:
        return None
    sev = report.by_severity
    lines = [
        f"🚨 {_redact(title)}",
        "",
        f"Auditoria de {report.generated_at:%d/%m/%Y %H:%M} — "
        f"{len(report.findings)} achado(s) "
        f"(🚨 {sev['critical']} · 🔴 {sev['high']} · "
        f"🟠 {sev['medium']} · 🟡 {sev['low']})",
        f"Verificadores: {', '.join(report.checks_run) or '—'}",
        "",
        "Problemas graves:",
    ]
    for fd in gravis[:_MAX_ITEMS]:
        cnt = f" (×{fd.count})" if fd.count > 1 else ""
        lines.append(f"{_SEV_EMOJI[fd.severity]} {_redact(fd.message)}{cnt}")
        if fd.source:
            lines.append(f"   fonte: {_redact(fd.source)}")
        if fd.evidence:
            lines.append(f"   evidência: {_redact(fd.evidence[:200])}")
        if fd.suggestion:
            lines.append(f"   → {_redact(fd.suggestion)}")
    if len(gravis) > _MAX_ITEMS:
        lines.append(f"… e mais {len(gravis) - _MAX_ITEMS}.")
    lines += ["", "Relatório completo: python3 auditoria.py"]
    return "\n".join(lines)


def build_user_message(report: AuditoriaReport) -> str | None:
    """Frase simples e acionável para o usuário da conversa — ou None.

    O usuário NÃO recebe linguagem de governança: nem severidade, nem nomes
    de verificadores, nem caminhos internos. Só o que ele PODE resolver,
    no máximo uma frase. Achado que não é da conta dele (log, replay,
    estado interno…) não gera mensagem nenhuma.

    Ex.: "Não consegui ler o arquivo extrato.csv — pode reenviar?"
    """
    # achados que o usuário consegue resolver: os que apontam arquivos dele
    acionaveis = [f for f in report.findings
                  if f.severity in _USER_SEVERITIES and f.files]
    if not acionaveis:
        return None
    files: list[str] = []
    for fd in acionaveis:
        for name in fd.files:
            if name not in files:
                files.append(name)
    shown = files[:_MAX_USER_FILES]
    lista = ", ".join(shown)
    if len(files) == 1:
        frase = f"Não consegui ler o arquivo {lista}. Pode reenviar?"
    else:
        frase = (f"Não consegui ler {len(files)} arquivos que você enviou "
                 f"({lista}{'…' if len(files) > _MAX_USER_FILES else ''}). "
                 f"Pode reenviar?")
    return _redact(frase)


def send_alert(report: AuditoriaReport, cfg: dict | None = None,
               title: str = "SUPERVISOR DO BOT FISCAL",
               chat_id: str | None = None) -> bool:
    """Envia o alerta para o chat de administração. True = enviado.

    Não faz nada (False) quando não há achado grave ou quando o Telegram
    não está configurado (token/chat_id ausentes).
    """
    alert = build_alert(report, title=title)
    if not alert:
        return False
    from ..notify.telegram import TelegramNotifier

    tg = (cfg or {}).get("telegram") or {}
    notifier = TelegramNotifier(tg.get("token", ""), chat_id or tg.get("chat_id", ""))
    if not notifier.enabled:
        return False
    return notifier.send_text(alert) is not None
