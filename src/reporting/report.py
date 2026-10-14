"""Geração de relatório: CSV (detalhado) + Markdown (legível)."""
from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

from ..domain.models import ReconciliationReport, Severity
from ..domain.tax import InvoiceAudit

_SEV_EMOJI = {Severity.HIGH: "🔴", Severity.MEDIUM: "🟠", Severity.LOW: "🟡"}


def _fmt(v) -> str:
    return "" if v is None else str(v)


def write_csv(report: ReconciliationReport, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = [
        "status", "method", "confidence",
        "txn_id", "txn_date", "txn_amount", "txn_description", "txn_reference", "txn_counterparty",
        "inv_id", "inv_number", "inv_issuer", "inv_category", "inv_amount", "inv_due_date",
        "inv_nf_type", "inv_series", "inv_access_key", "inv_issuer_doc",
        "inv_customer", "inv_customer_doc", "inv_competency",
        "explanation",
    ]
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for m in report.matches:
            t, i = m.transaction, m.invoice
            w.writerow([
                m.status.value, m.method, f"{m.confidence:.2f}",
                _fmt(t.id if t else None), _fmt(t.date if t else None),
                _fmt(t.amount if t else None), _fmt(t.description if t else None),
                _fmt(t.reference if t else None), _fmt(t.counterparty if t else None),
                _fmt(i.id if i else None), _fmt(i.number if i else None),
                _fmt(i.issuer if i else None), _fmt(i.category if i else None),
                _fmt(i.amount if i else None), _fmt(i.due_date if i else None),
                _fmt(i.nf_type if i else None), _fmt(i.series if i else None),
                _fmt(i.access_key if i else None), _fmt(i.issuer_doc if i else None),
                _fmt(i.customer if i else None), _fmt(i.customer_doc if i else None),
                _fmt(i.competency if i else None),
                _fmt(m.explanation),
            ])
    return path


def _tax_section(audits: list[InvoiceAudit] | None) -> list[str]:
    if not audits:
        return []
    lines = ["## 🧾 Auditoria fiscal das faturas e notas", ""]
    for a in audits:
        probs = a.problems
        head = (f"### {a.nf_type or 'Fatura'} {a.number} — {a.issuer}"
                + (f" (R$ {a.amount})" if a.amount is not None else ""))
        lines.append(head)
        meta = []
        if a.series:
            meta.append(f"série {a.series}")
        if a.access_key:
            meta.append(f"chave/código {a.access_key}")
        if a.competency:
            meta.append(f"competência {a.competency}")
        if a.issue_date:
            meta.append(f"emitida em {a.issue_date:%d/%m/%Y}")
        if meta:
            lines.append(f"- {_fmt(', '.join(meta))}")
        if a.customer:
            doc = f" ({a.customer_doc})" if a.customer_doc else ""
            lines.append(f"- Tomador/destinatário: {a.customer}{doc}")
        if a.issuer_doc:
            lines.append(f"- CNPJ/CPF emitente: {a.issuer_doc}")
        if a.tax_lines:
            lines.append("")
            lines.append("| Imposto | Base | Alíquota | Valor |")
            lines.append("|---|---|---|---|")
            for tl in a.tax_lines:
                rate = f"{tl.rate:.2%}" if tl.rate is not None else "—"
                amount = _fmt(tl.amount)
                if tl.estimated:
                    rate = f"{rate} (tabela)" if tl.rate is not None else "tabela"
                    amount = f"{amount} (tabela)"
                lines.append(f"| {tl.name} | {_fmt(tl.base)} | {rate} | {amount} |")
        lines.append("")
        if a.has_retentions:
            lines.append(f"- ✂️ Retenções na fonte: R$ {a.total_retentions}")
        unverified = any(tl.base is None or tl.rate is None for tl in a.tax_lines)
        estimated = [tl.name for tl in a.tax_lines if tl.estimated]
        if not a.tax_lines:
            lines.append("- Sem detalhes de impostos identificados.")
        elif not probs and not unverified:
            lines.append("- ✅ Cálculo de impostos conferido.")
        else:
            if unverified:
                lines.append("- 🟡 Impostos identificados pelo valor; base/alíquota não "
                             "estavam legíveis, então o cálculo não pôde ser conferido.")
            for c in probs:
                emoji = _SEV_EMOJI.get(c.severity, "•")
                lines.append(f"- {emoji} {c.message}")
        if estimated:
            lines.append("- 🟡 " + ", ".join(estimated) +
                         " mascarado(s) na nota — valor(es) calculado(s) pela tabela de alíquotas.")
        lines.append("")
    return lines


def write_markdown(report: ReconciliationReport, path: str | Path,
                   audits: list[InvoiceAudit] | None = None) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    sev = report.by_severity
    lines: list[str] = []
    lines.append("# Relatório de Conciliação de Contas e Faturas")
    lines.append("")
    lines.append(f"- **Gerado em:** {report.generated_at:%d/%m/%Y %H:%M}")
    if report.period_start and report.period_end:
        lines.append(f"- **Período:** {report.period_start} a {report.period_end}")
    lines.append(f"- **LLM utilizado:** {'sim' if report.llm_used else 'não (modo determinístico)'}")
    lines.append("")
    lines.append("## Resumo")
    lines.append("")
    lines.append(f"| Métrica | Valor |")
    lines.append(f"|---|---|")
    lines.append(f"| Transações processadas | {report.total_transactions} |")
    lines.append(f"| Faturas processadas | {report.total_invoices} |")
    lines.append(f"| Conciliadas | {len(report.matched)} |")
    lines.append(f"| Inconsistências | {len(report.inconsistencies)} "
                 f"(🔴 {sev['high']} · 🟠 {sev['medium']} · 🟡 {sev['low']}) |")
    lines.append("")

    if report.inconsistencies:
        lines.append("## ⚠️ Inconsistências detectadas")
        lines.append("")
        for n, inc in enumerate(report.inconsistencies, 1):
            emoji = _SEV_EMOJI.get(inc.severity, "•")
            lines.append(f"### {n}. {emoji} [{inc.severity.value.upper()}] {inc.type.value}")
            lines.append(f"- {inc.message}")
            if inc.detail:
                lines.append(f"- Detalhe: {inc.detail}")
            lines.append("")
    else:
        lines.append("## ✅ Nenhuma inconsistência detectada")
        lines.append("")

    lines.extend(_tax_section(audits))

    lines.append("## Detalhes da conciliação")
    lines.append("")
    lines.append("| Status | Método | Conf. | Transação | Data | Valor | Fatura | Emissor | Categoria |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for m in report.matches:
        t, i = m.transaction, m.invoice
        lines.append(
            f"| {m.status.value} | {m.method} | {m.confidence:.0%} "
            f"| {_fmt(t.id if t else '—')} | {_fmt(t.date if t else '—')} "
            f"| {_fmt(abs(t.amount) if t else '—')} | {_fmt(i.number if i else '—')} "
            f"| {_fmt(i.issuer if i else '—')} | {_fmt(i.category if i else '—')} |"
        )
    lines.append("")

    # classificação por categoria
    cats: dict[str, int] = {}
    for m in report.matches:
        if m.invoice and m.invoice.category:
            cats[m.invoice.category] = cats.get(m.invoice.category, 0) + 1
    if cats:
        lines.append("## Faturas por categoria")
        lines.append("")
        for c, n in sorted(cats.items(), key=lambda x: -x[1]):
            lines.append(f"- **{c}:** {n}")
        lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def write_report(report: ReconciliationReport, out_dir: str | Path,
                 stamp: str | None = None,
                 audits: list[InvoiceAudit] | None = None) -> tuple[Path, Path]:
    """Escreve CSV + Markdown em out_dir. Retorna (caminho_csv, caminho_md)."""
    out_dir = Path(out_dir)
    stamp = stamp or datetime.now().strftime("%Y%m%d_%H%M%S")
    return write_csv(report, out_dir / f"conciliacao_{stamp}.csv"), \
        write_markdown(report, out_dir / f"conciliacao_{stamp}.md", audits=audits)
