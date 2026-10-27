"""Verificador de saúde dos logs do bot.

Detecta nos arquivos de log: token vazado, instância duplicada (409),
tracebacks, chats bloqueados (403), instabilidade de rede (DNS/timeout/502),
respostas vazias, falhas de envio, falhas de parse de anexos e falta de
timestamp (auditabilidade).
"""
from __future__ import annotations

import re
from pathlib import Path

from ..models import CheckContext, Finding, Severity

_TOKEN_RE = re.compile(r"\d{8,10}:[A-Za-z0-9_-]{35}")
_TIMESTAMP_RE = re.compile(
    r"\d{2}/\d{2}/\d{4}[ T]\d{2}:\d{2}|\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}"
)

# trecho esperado na linha -> chave da métrica
_GETUPDATES_ERRORS = [
    ("409 Conflict", "getupdates_409"),
    ("Temporary failure in name resolution", "dns_failure"),
    ("The read operation timed out", "timeout"),
    ("403 Forbidden", "getupdates_403"),
    ("502 Bad Gateway", "server_502"),
    ("Server disconnected", "server_disconnect"),
]

_STATS_KEYS = [
    "lines", "timestamped_lines", "bot_starts",
    "getupdates_errors", "getupdates_409", "getupdates_403",
    "dns_failure", "timeout", "server_502", "server_disconnect",
    "max_getupdates_streak",
    "process_errors", "blocked_chats_403", "tracebacks",
    "send_failures", "empty_responses", "parse_failures", "token_leaks",
]


def _f(id_: str, severity: Severity, message: str, evidence: str,
       suggestion: str, count: int = 1, source: str = "") -> Finding:
    return Finding(id=id_, check="log_health", severity=severity,
                   message=message, evidence=evidence, suggestion=suggestion,
                   count=count, source=source)


def run(ctx: CheckContext) -> tuple[list[Finding], dict[str, int]]:
    stats = {k: 0 for k in _STATS_KEYS}
    findings: list[Finding] = []
    files: list[str] = []
    token_files: list[str] = []

    existing = [p for p in ctx.log_paths if p.exists()]
    if not existing:
        findings.append(_f(
            "log_health.no_logs", Severity.LOW,
            "Nenhum arquivo de log encontrado",
            f"Procurado: {', '.join(str(p) for p in ctx.log_paths) or '(nenhum caminho)'}",
            "Gere logs do bot (ex.: python3 bot.py 2>&1 | tee bot.log) para que a "
            "auditoria tenha o que analisar.",
        ))
        return findings, stats

    for path in existing:
        try:
            label = str(path.resolve().relative_to(Path.cwd().resolve()))
        except ValueError:
            label = str(path)
        files.append(label)
        text = path.read_text(encoding="utf-8", errors="replace")
        streak = 0
        for line in text.splitlines():
            stats["lines"] += 1
            if _TIMESTAMP_RE.search(line):
                stats["timestamped_lines"] += 1
            if _TOKEN_RE.search(line):
                stats["token_leaks"] += 1
                if label not in token_files:
                    token_files.append(label)
            if "🤖 Bot @" in line and "online" in line:
                stats["bot_starts"] += 1
            if "Traceback (most recent call last):" in line:
                stats["tracebacks"] += 1
            if "❌ falha ao enviar" in line:
                stats["send_failures"] += 1
            if "resposta VAZIA" in line:
                stats["empty_responses"] += 1
            if "Não consegui extrair dados" in line:
                stats["parse_failures"] += 1
            if "[bot] erro ao processar:" in line:
                stats["process_errors"] += 1
                if "403 Forbidden" in line:
                    stats["blocked_chats_403"] += 1
            if "[bot] getUpdates:" in line:
                stats["getupdates_errors"] += 1
                streak += 1
                stats["max_getupdates_streak"] = max(stats["max_getupdates_streak"], streak)
                for needle, key in _GETUPDATES_ERRORS:
                    if needle in line:
                        stats[key] += 1
                        break
            elif not line.startswith(("   ", "\t")) and "Traceback" not in line:
                # linha de topo nova quebra a rajada; indentadas/traceback são
                # continuação do erro atual
                streak = 0

    src = ", ".join(files)

    if stats["token_leaks"]:
        findings.append(_f(
            "log_health.token_leak", Severity.CRITICAL,
            "Token do bot vazado em texto puro nos logs",
            f"{stats['token_leaks']} ocorrência(s) em {', '.join(token_files)}",
            "Apague/rote os logs afetados e rotacione o token em @BotFather. O bot "
            "já usa redact() no loop — logs antigos podem ter sido gerados antes.",
            count=stats["token_leaks"], source=", ".join(token_files),
        ))
    if stats["getupdates_409"]:
        findings.append(_f(
            "log_health.duplicate_instance", Severity.HIGH,
            "Conflito 409: mais de uma instância do bot usa o mesmo token",
            f"{stats['getupdates_409']}× '409 Conflict' em {src}",
            "Encerre a instância duplicada (ps aux | grep -E 'bot.py|src.bot') — "
            "só pode haver UM processo com o mesmo token disputando getUpdates.",
            count=stats["getupdates_409"], source=src,
        ))
    if stats["tracebacks"]:
        findings.append(_f(
            "log_health.traceback", Severity.HIGH,
            "Tracebacks de exceção no processamento de mensagens",
            f"{stats['tracebacks']} traceback(s) em {src}",
            "Abra o log, localize o último 'Traceback' e corrija a causa raiz; o "
            "usuário recebeu só o aviso genéric de erro ao processar.",
            count=stats["tracebacks"], source=src,
        ))
    if stats["blocked_chats_403"]:
        findings.append(_f(
            "log_health.blocked_chat", Severity.MEDIUM,
            "Chats com acesso negado (403) ao responder",
            f"{stats['blocked_chats_403']} erro(s) 403 em 'erro ao processar' em {src}",
            "Provável usuário que bloqueou o bot. Remova o chat de "
            "allowed_chat_ids ou ignore — o fluxo já não derruba o loop.",
            count=stats["blocked_chats_403"], source=src,
        ))
    if stats["send_failures"]:
        findings.append(_f(
            "log_health.send_failure", Severity.MEDIUM,
            "Falhas ao enviar mensagens/relatórios ao Telegram",
            f"{stats['send_failures']} falha(s) de envio em {src}",
            "Confira parse_mode (Markdown malformado quebra o envio) e a "
            "conectividade; cada falha já é isolada por item no bot.",
            count=stats["send_failures"], source=src,
        ))
    if stats["empty_responses"]:
        findings.append(_f(
            "log_health.empty_response", Severity.MEDIUM,
            "Respostas vazias geradas pelo bot",
            f"{stats['empty_responses']} resposta(s) VAZIA(s) em {src}",
            "Algum handler devolveu BotResponse sem texto nem relatório — "
            "revise o fluxo que atende esse tipo de mensagem.",
            count=stats["empty_responses"], source=src,
        ))
    timeouts_sev = Severity.MEDIUM if stats["timeout"] >= 50 else Severity.LOW
    if stats["timeout"]:
        findings.append(_f(
            "log_health.timeout", timeouts_sev,
            "Timeouts no long polling (getUpdates)",
            f"{stats['timeout']} timeout(s) em {src}",
            "Rede instável ou timeout do httpx menor que o polling. Considere "
            "aumentar o timeout HTTP ou checar a conexão da máquina.",
            count=stats["timeout"], source=src,
        ))
    dns_sev = Severity.MEDIUM if stats["dns_failure"] >= 50 else Severity.LOW
    if stats["dns_failure"]:
        findings.append(_f(
            "log_health.dns_failure", dns_sev,
            "Falhas de resolução DNS (máquina sem rede/DNS)",
            f"{stats['dns_failure']} falha(s) de DNS em {src}",
            "A máquina perdeu internet/DNS durante o funcionamento; o bot "
            "recupera sozinho em 5s, mas a conversa fica indisponível.",
            count=stats["dns_failure"], source=src,
        ))
    if stats["getupdates_403"]:
        findings.append(_f(
            "log_health.getupdates_403", Severity.MEDIUM,
            "getUpdates negado (403): token inválido ou bot removido",
            f"{stats['getupdates_403']}× 403 no getUpdates em {src}",
            "Confira se o token em TELEGRAM_BOT_TOKEN ainda é válido "
            "(@BotFather → /mybots).",
            count=stats["getupdates_403"], source=src,
        ))
    telegram_errors = stats["server_502"] + stats["server_disconnect"]
    if telegram_errors:
        findings.append(_f(
            "log_health.telegram_instability", Severity.LOW,
            "Instabilidade do endpoint do Telegram (502/disconnect)",
            f"{telegram_errors} ocorrência(s) em {src}",
            "Falha do lado do api.telegram.org — o bot já se recupera sozinho; "
            "só investigue se persistir por horas.",
            count=telegram_errors, source=src,
        ))
    if stats["max_getupdates_streak"] >= 20:
        findings.append(_f(
            "log_health.error_streak", Severity.MEDIUM,
            "Rajada longa de erros consecutivos no getUpdates",
            f"Máximo de {stats['max_getupdates_streak']} erros seguidos em {src}",
            "Verifique conectividade/VPN/proxy da máquina — o bot ficou um "
            "trecho inteiro sem conseguir receber mensagens.",
            count=stats["max_getupdates_streak"], source=src,
        ))
    if stats["parse_failures"]:
        findings.append(_f(
            "log_health.parse_failure", Severity.LOW,
            "Anexos recebidos sem dados extraídos",
            f"{stats['parse_failures']} anexo(s) sem dados extraídos em {src}",
            "Formato não suportado ou ilegível. Confira o anexo no e-mail ou "
            "peça o PDF original; o parsing_quality audita os arquivos das "
            "sessões com mais detalhe.",
            count=stats["parse_failures"], source=src,
        ))
    if stats["bot_starts"] >= 3:
        findings.append(_f(
            "log_health.many_restarts", Severity.LOW,
            "Bot reiniciou várias vezes durante o período dos logs",
            f"{stats['bot_starts']} linhas de inicialização em {src}",
            "Reinícios frequentes podem perder contexto; se não foi manual, "
            "procure o traceback anterior no log.",
            count=stats["bot_starts"], source=src,
        ))
    if stats["lines"] and not stats["timestamped_lines"]:
        findings.append(_f(
            "log_health.no_timestamps", Severity.LOW,
            "Logs sem timestamp — difícil correlacionar incidentes",
            f"0 de {stats['lines']} linhas com data/hora em {src}",
            "Passe o bot por um logger com timestamp (ex.: logging com "
            "format '%(asctime)s') para a auditoria poder ordenar eventos.",
            source=src,
        ))

    return findings, stats
