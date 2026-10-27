"""Supervisor do Bot Fiscal — IA de governança que audita o bot e relata erros.

Detecta problemas nos logs (e, nas próximas fases, nas sessões e relatórios)
do bot de conciliação e gera um relatório (Markdown + CSV) com evidência e
sugestão de correção. Não altera nada — só detecta e relata.

Uso:
    python3 auditoria.py                     # todas as verificações
    python3 auditoria.py --only log_health   # só um verificador
    python3 auditoria.py --logs bot.log logs/bot.log
    python3 auditoria.py --out data/auditoria
    python3 auditoria.py --no-report         # só resumo no console
    python3 auditoria.py --chat 111111   # só UMA sessão (mesmos checks
                                             #  que o bot roda ao vivo)
    python3 auditoria.py --notify            # achado crítico/alto vai para o
                                             #  chat de admin (TELEGRAM_CHAT_ID)

Código de saída: 1 se houver achado crítico, 0 caso contrário.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from src.config import load_config
from src.supervisor import (
    build_alert,
    discover_log_paths,
    run_audit,
    run_session_audit,
    send_alert,
    write_audit_report,
)
from src.supervisor.models import Severity

_SEV_EMOJI = {
    Severity.CRITICAL: "🚨",
    Severity.HIGH: "🔴",
    Severity.MEDIUM: "🟠",
    Severity.LOW: "🟡",
}


def run(logs: list[str] | None = None, sessions: str | None = None,
        out: str | None = None, only: list[str] | None = None,
        config_path: str | None = None, no_report: bool = False,
        chat: str | None = None, notify_flag: bool = False):
    cfg = load_config(config_path)
    if chat:
        # uma única sessão: os mesmos 4 checks que o bot roda ao vivo
        root = Path(sessions or (cfg.get("bot", {}) or {}).get("sessions_dir",
                                                               "data/sessions"))
        report = run_session_audit(cfg, root / chat, only=only)
        log_paths: list[Path] = []
    else:
        log_paths = [Path(p) for p in logs] if logs else discover_log_paths()
        report = run_audit(cfg=cfg, log_paths=log_paths, sessions_dir=sessions,
                           only=only)

    print(f"🔍 Supervisor: verificadores {', '.join(report.checks_run) or '—'}")
    for p in log_paths:
        print(f"   📄 {p}")
    if chat:
        print(f"   🗂️ sessão: {chat}")

    if not no_report:
        out_dir = Path(out or "data/auditoria")
        csv_path, md_path = write_audit_report(report, out_dir)
        print(f"📄 Relatório CSV: {csv_path}")
        print(f"📝 Relatório MD:  {md_path}")

    _print_summary(report)

    if notify_flag:
        title = f"SUPERVISOR · sessão {chat}" if chat \
            else "SUPERVISOR · auditoria completa"
        if build_alert(report, title=title) is None:
            print("📨 Sem achado crítico/alto — nada a alertar.")
        elif send_alert(report, cfg, title=title):
            print("📨 Alerta enviado para o chat de administração "
                  "(TELEGRAM_CHAT_ID).")
        else:
            print("📨 Alerta NÃO enviado: Telegram sem token/chat_id "
                  "(TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID).")
    return report


def _print_summary(report):
    sev = report.by_severity
    print("\n" + "=" * 56)
    print("RESUMO DA AUDITORIA (SUPERVISOR)")
    print("=" * 56)
    print(f"  Achados: {len(report.findings)} "
          f"(🚨 {sev['critical']} · 🔴 {sev['high']} · "
          f"🟠 {sev['medium']} · 🟡 {sev['low']})")
    if report.findings:
        print("\n  Problemas:")
        for fd in report.findings:
            icon = _SEV_EMOJI[fd.severity]
            cnt = f"  (×{fd.count})" if fd.count > 1 else ""
            print(f"   {icon} [{fd.severity.value.upper()}] {fd.message}{cnt}")
            if fd.suggestion:
                print(f"      → {fd.suggestion}")
    else:
        print("\n  ✅ Nenhum problema encontrado.")
    print("=" * 56)


def main():
    parser = argparse.ArgumentParser(
        description="Supervisor do Bot Fiscal: audita logs/sessões e relata erros")
    parser.add_argument("--logs", nargs="+",
                        help="Arquivos de log (padrão: bot.log e logs/*.log)")
    parser.add_argument("--sessions", help="Diretório de sessões do bot")
    parser.add_argument("--chat", help="Audita só UMA sessão (id do chat)")
    parser.add_argument("--out", help="Diretório de saída dos relatórios")
    parser.add_argument("--only", nargs="+",
                        help="Só estes verificadores (ex.: log_health)")
    parser.add_argument("--config", help="Caminho do config.yaml")
    parser.add_argument("--no-report", action="store_true",
                        help="Não escrever relatório em disco")
    parser.add_argument("--notify", action="store_true",
                        help="Envia achado crítico/alto para TELEGRAM_CHAT_ID")
    args = parser.parse_args()
    report = run(args.logs, args.sessions, args.out, args.only,
                 args.config, args.no_report, args.chat, args.notify)
    if report.by_severity[Severity.CRITICAL.value]:
        sys.exit(1)


if __name__ == "__main__":
    main()
