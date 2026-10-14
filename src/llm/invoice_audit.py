"""Auditoria de fatura a partir de texto (OCR).

Fluxo:  texto (OCR)  ->  LLM/fallback extrai dados  ->  InvoiceAudit
        ->  verificação determinística do cálculo de impostos.

É a ponte entre "o cliente mandou uma foto" e "o parecer fiscal + a fatura
pronta para conciliar".
"""
from __future__ import annotations

from decimal import Decimal

from ..domain.tax import (
    InvoiceAudit,
    TaxLine,
    _dec,
    audit_to_invoice,
    detect_regime,
    verify_invoice_taxes,
)
from .base import LLMClient


def build_audit(llm: LLMClient, text: str, source: str = "",
                tolerance: Decimal = Decimal("0.05")) -> InvoiceAudit | None:
    """Extrai a fatura do texto e verifica o cálculo dos impostos.

    Retorna None se nada foi reconhecido (texto vazio, LLM sem saída útil).
    """
    data = llm.extract_invoice_from_text(text)
    if not data:
        return None

    tax_lines: list[TaxLine] = []
    for tl in data.get("tax_lines") or []:
        if not isinstance(tl, dict):
            continue
        name = str(tl.get("name") or "").strip()
        if not name:
            continue
        tax_lines.append(TaxLine(
            name=name,
            base=_dec(tl.get("base")),
            rate=_dec(tl.get("rate")),
            amount=_dec(tl.get("amount")),
        ))

    audit = InvoiceAudit(
        source=source,
        issuer=str(data.get("issuer") or "").strip(),
        number=str(data.get("number") or "").strip(),
        issue_date=_parse_iso(data.get("issue_date")),
        due_date=_parse_iso(data.get("due_date")),
        amount=_dec(data.get("amount")),
        subtotal=_dec(data.get("subtotal")),
        tax_lines=tax_lines,
        city=_clean_str(data.get("city")),
        service_code=_clean_str(data.get("service_code")),
        notes=_clean_str(data.get("notes")),
        nf_type=_norm_nf_type(data.get("nf_type")),
        series=_clean_str(data.get("series")),
        access_key=_clean_str(data.get("access_key")),
        issuer_doc=_clean_str(data.get("issuer_doc")),
        customer=_clean_str(data.get("customer")),
        customer_doc=_clean_str(data.get("customer_doc")),
        competency=_clean_str(data.get("competency")),
        service_value=_dec(data.get("service_value")),
        retentions=_dec(data.get("retentions")),
        net_value=_dec(data.get("net_value")),
        freight=_dec(data.get("freight")),
        insurance=_dec(data.get("insurance")),
        other_expenses=_dec(data.get("other_expenses")),
        st=_dec(data.get("st")),
        discounts=_dec(data.get("discounts")),
    )
    # regime da emissora: marcado no texto da nota (campo/rodapé/menção)
    audit.regime, audit.regime_evidence = detect_regime(text)
    verify_invoice_taxes(audit, tolerance=tolerance)
    return audit


def _norm_nf_type(v) -> str | None:
    s = _clean_str(v)
    if not s:
        return None
    u = s.upper().replace(" ", "")
    if u in ("NFE",):
        return "NFe"
    if u in ("NFSE", "NFS"):
        return "NFS-e"
    if u in ("NFCE",):
        return "NFCe"
    return s


def _clean_str(v) -> str | None:
    s = str(v or "").strip()
    return s or None


def _parse_iso(v):
    if not v:
        return None
    from datetime import date
    s = str(v).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y"):
        try:
            from datetime import datetime
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


__all__ = ["build_audit", "audit_to_invoice"]
