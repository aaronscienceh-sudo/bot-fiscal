"""Assistente de Conciliação de Contas e Faturas.

Ponto de entrada: lê arquivos de data/inbox, concilia, classifica,
sinaliza inconsistências, gera relatório (CSV+MD) e envia alertas no Telegram.

Uso:
    python main.py                     # usa config.yaml padrão
    python main.py --inbox data/inbox  # diretório customizado
    python main.py --no-telegram       # só gera relatório local
"""
from __future__ import annotations

import argparse
import sys
from decimal import Decimal
from pathlib import Path

from src.config import load_config
from src.domain.rules import DeterministicMatcher
from src.engine import Reconciler
from src.llm import make_llm
from src.notify import TelegramNotifier
from src.parsing import parse_directory
from src.reporting import write_report


def run(inbox: str | None = None, reports: str | None = None,
        config_path: str | None = None, no_telegram: bool = False):
    cfg = load_config(config_path)

    # 1) parsing -----------------------------------------------------------
    inbox_dir = Path(inbox or cfg["paths"]["inbox"])
    if not inbox_dir.exists():
        print(f"❌ Diretório de entrada não existe: {inbox_dir}")
        sys.exit(1)
    print(f"📂 Processando arquivos em: {inbox_dir}")
    results = parse_directory(inbox_dir)

    transactions, invoices = [], []
    for r in results:
        transactions.extend(r.transactions)
        invoices.extend(r.invoices)
        tag = "✅" if (r.transactions or r.invoices) else "⚠️"
        print(f"  {tag} {Path(r.path).name}: "
              f"{len(r.transactions)} transações, {len(r.invoices)} faturas"
              + (f"  [erros: {'; '.join(r.errors)}]" if r.errors else ""))

    if not transactions and not invoices:
        print("❌ Nenhum dado reconhecido. Verifique os arquivos e cabeçalhos.")
        sys.exit(1)

    # 2) camada de IA ------------------------------------------------------
    llm = make_llm(cfg["llm"])
    llm_name = (f"Ollama · {cfg['llm']['model']}" if llm.available
                else "fallback determinístico (sem LLM)")
    print(f"🧠 Modelo de IA: {llm_name}")

    # 3) conciliação -------------------------------------------------------
    m = cfg["matching"]
    matcher = DeterministicMatcher(
        amount_tolerance=Decimal(m["amount_tolerance"]),
        date_window_days=int(m["date_window_days"]),
        name_threshold=float(m["name_threshold"]),
    )
    reconciler = Reconciler(
        llm, matcher,
        amount_tolerance=Decimal(m["amount_tolerance"]),
        fuzzy_confidence_threshold=float(m["fuzzy_confidence_threshold"]),
        late_tolerance_days=int(m["late_tolerance_days"]),
        low_confidence_threshold=float(m["low_confidence_threshold"]),
    )
    report = reconciler.reconcile(transactions, invoices)

    # 4) relatório ---------------------------------------------------------
    reports_dir = Path(reports or cfg["paths"]["reports"])
    csv_path, md_path = write_report(report, reports_dir)
    print(f"📄 Relatório CSV: {csv_path}")
    print(f"📝 Relatório MD:  {md_path}")

    # 5) Telegram ----------------------------------------------------------
    if no_telegram:
        print("🔕 Telegram desativado nesta execução.")
    else:
        tg = cfg["telegram"]
        notifier = TelegramNotifier(tg["token"], tg["chat_id"], enabled=tg["enabled"])
        if notifier.enabled:
            notifier.send_report(report, csv_path, md_path)
            print("📨 Alertas enviados no Telegram.")
        else:
            print("🔕 Telegram não configurado (token/chat_id ausentes).")

    # 6) resumo no terminal ------------------------------------------------
    _print_summary(report)
    return report


def _print_summary(report):
    sev = report.by_severity
    print("\n" + "=" * 52)
    print("RESUMO DA CONCILIAÇÃO")
    print("=" * 52)
    print(f"  Transações : {report.total_transactions}")
    print(f"  Faturas    : {report.total_invoices}")
    print(f"  Conciliadas: {len(report.matched)}")
    print(f"  Inconsistências: {len(report.inconsistencies)} "
          f"(alta {sev['high']} · média {sev['medium']} · baixa {sev['low']})")
    if report.inconsistencies:
        print("\n  Alertas:")
        for inc in report.inconsistencies:
            icon = {"high": "🔴", "medium": "🟠", "low": "🟡"}[inc.severity.value]
            print(f"   {icon} {inc.message}")
    else:
        print("\n  ✅ Tudo conciliado, sem inconsistências.")
    print("=" * 52)


def main():
    parser = argparse.ArgumentParser(description="Assistente de Conciliação de Contas e Faturas")
    parser.add_argument("--inbox", help="Diretório com os arquivos de entrada")
    parser.add_argument("--reports", help="Diretório de saída dos relatórios")
    parser.add_argument("--config", help="Caminho do config.yaml")
    parser.add_argument("--no-telegram", action="store_true", help="Não enviar alertas no Telegram")
    args = parser.parse_args()
    run(args.inbox, args.reports, args.config, args.no_telegram)


if __name__ == "__main__":
    main()
