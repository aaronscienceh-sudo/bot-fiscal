"""Testes da auditoria fiscal (cálculo de impostos) e do OCR de fotos.

Cobre: verificação determinística (base × alíquota, total), conversão em
Invoice, extração por fallback (regex), build_audit e OCR via Tesseract
(este último é pulado se o binário não estiver instalado).
"""
from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.domain.tax import (  # noqa: E402
    InvoiceAudit,
    TaxLine,
    audit_to_invoice,
    verify_invoice_taxes,
)
from src.llm import FallbackClient, build_audit  # noqa: E402
from src.parsing import OCRError, ocr_image, tesseract_available  # noqa: E402


# --------------------------------------------------------------------------- #
# Verificação determinística do cálculo de impostos
# --------------------------------------------------------------------------- #
def _audit(tax_lines, subtotal=None, amount=None):
    return InvoiceAudit(issuer="Teste", number="1", tax_lines=tax_lines,
                        subtotal=subtotal, amount=amount)


def test_tax_math_ok():
    a = _audit([TaxLine(name="ICMS", base=Decimal("1000"), rate=Decimal("0.18"),
                        amount=Decimal("180.00"))])
    verify_invoice_taxes(a)
    assert a.ok
    assert all(c.ok for c in a.checks)


def test_tax_math_wrong():
    # 1000 × 18% = 180, mas a fatura informa 200 -> divergência alta
    a = _audit([TaxLine(name="ICMS", base=Decimal("1000"), rate=Decimal("0.18"),
                        amount=Decimal("200.00"))])
    verify_invoice_taxes(a)
    assert not a.ok
    bad = [c for c in a.checks if not c.ok]
    assert len(bad) == 1
    assert bad[0].kind == "tax_math"
    assert bad[0].expected == Decimal("180.00")
    assert bad[0].actual == Decimal("200.00")


def test_rate_as_percent_number():
    # alíquota veio como 18 (percentual) -> normaliza para 0.18
    a = _audit([TaxLine(name="ICMS", base=Decimal("1000"), rate=Decimal("18"),
                        amount=Decimal("180.00"))])
    verify_invoice_taxes(a)
    assert a.ok


def test_total_consistent():
    a = _audit([TaxLine(name="ICMS", base=Decimal("1000"), rate=Decimal("0.18"),
                        amount=Decimal("180.00"))],
               subtotal=Decimal("1000"), amount=Decimal("1180.00"))
    verify_invoice_taxes(a)
    assert a.ok


def test_total_divergent():
    # subtotal 1000 + ICMS 180 = 1180, mas total informado 1200
    a = _audit([TaxLine(name="ICMS", base=Decimal("1000"), rate=Decimal("0.18"),
                        amount=Decimal("180.00"))],
               subtotal=Decimal("1000"), amount=Decimal("1200.00"))
    verify_invoice_taxes(a)
    assert not a.ok
    assert any(c.kind == "total" and not c.ok for c in a.checks)


def test_tax_line_without_amount_informative():
    # valor ausente/mascarado no documento é informativo, não divergência
    a = _audit([TaxLine(name="IPI")])
    verify_invoice_taxes(a)
    assert a.ok
    assert any(c.kind == "field" and c.ok for c in a.checks)


def test_nfe_total_ignores_highlights():
    # NFe: vNF = vProd + frete (ICMS/PIS/COFINS são destacados e NÃO somam)
    a = InvoiceAudit(issuer="T", number="1", nf_type="NFe",
                     subtotal=Decimal("25.56"), amount=Decimal("32.02"),
                     freight=Decimal("6.46"),
                     tax_lines=[TaxLine(name="ICMS", amount=Decimal("3.84")),
                                TaxLine(name="PIS", amount=Decimal("0.21")),
                                TaxLine(name="COFINS", amount=Decimal("0.96"))])
    verify_invoice_taxes(a)
    assert a.ok


def test_nfs_e_total_check():
    a = InvoiceAudit(issuer="T", number="1", nf_type="NFS-e",
                     amount=Decimal("30.90"), service_value=Decimal("30.90"),
                     net_value=Decimal("30.90"), retentions=Decimal("0.00"),
                     discounts=Decimal("0.00"),
                     tax_lines=[TaxLine(name="ISSQN", base=Decimal("30.90"),
                                        rate=Decimal("5"), amount=Decimal("1.54"))])
    verify_invoice_taxes(a)
    assert a.ok


def test_nfs_e_total_divergent():
    a = InvoiceAudit(issuer="T", number="1", nf_type="NFS-e",
                     amount=Decimal("30.90"), service_value=Decimal("30.90"),
                     net_value=Decimal("25.00"))
    verify_invoice_taxes(a)
    assert any(c.kind == "total" and not c.ok for c in a.checks)


def test_tolerance_respected():
    # diferença de 0.03 <= tolerância 0.05 -> ok
    a = _audit([TaxLine(name="ICMS", base=Decimal("1000"), rate=Decimal("0.18"),
                        amount=Decimal("180.03"))])
    verify_invoice_taxes(a, tolerance=Decimal("0.05"))
    assert a.ok


# --------------------------------------------------------------------------- #
# Conversão em Invoice
# --------------------------------------------------------------------------- #
def test_audit_to_invoice():
    a = _audit([TaxLine(name="ICMS", base=Decimal("1000"), rate=Decimal("0.18"),
                        amount=Decimal("180.00"))],
               subtotal=Decimal("1000"), amount=Decimal("1180.00"))
    a.issuer = "Enel Energia"
    a.number = "4521"
    inv = audit_to_invoice(a, "foto.jpg")
    assert inv is not None
    assert inv.number == "4521"
    assert inv.issuer == "Enel Energia"
    assert inv.amount == Decimal("1180.00")


def test_audit_to_invoice_without_amount_uses_subtotal_plus_taxes():
    a = _audit([TaxLine(name="ICMS", amount=Decimal("180.00"))],
               subtotal=Decimal("1000"), amount=None)
    inv = audit_to_invoice(a, "foto.jpg")
    assert inv is not None
    assert inv.amount == Decimal("1180.00")


# --------------------------------------------------------------------------- #
# Extração por fallback (regex) + build_audit
# --------------------------------------------------------------------------- #
SAMPLE_TEXT = """
FATURA / NOTA FISCAL
Emitente: ENEL DISTRIBUICAO DE ENERGIA SA
Numero: 4521
Emissao: 01/08/2026
Vencimento: 15/08/2026

Subtotal: R$ 1000,00
ICMS (18%): R$ 180,00
Total: R$ 1180,00
"""


def test_fallback_extract():
    fb = FallbackClient()
    data = fb.extract_invoice_from_text(SAMPLE_TEXT)
    assert data is not None
    assert data["number"] == "4521"
    assert "ENEL" in (data["issuer"] or "").upper()
    assert data["amount"] == Decimal("1180.00")


def test_fallback_extract_empty():
    fb = FallbackClient()
    assert fb.extract_invoice_from_text("   ") is None
    assert fb.extract_invoice_from_text("sem dados relevantes aqui") is None


def test_build_audit_with_fallback():
    audit = build_audit(FallbackClient(), SAMPLE_TEXT, source="foto.jpg")
    assert audit is not None
    assert audit.number == "4521"
    assert audit.amount == Decimal("1180.00")
    # ICMS extraído só com valor (sem base/alíquota) -> aviso, não erro alto
    assert any(tl.name == "ICMS" for tl in audit.tax_lines)


# --------------------------------------------------------------------------- #
# OCR (pulado se o tesseract não estiver instalado)
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not tesseract_available(),
                    reason="tesseract não instalado")
def test_ocr_roundtrip(tmp_path):
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (900, 400), "white")
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 28)
    except OSError:
        font = ImageFont.load_default()
    lines = [
        "FATURA 4521",
        "Emitente: ENEL ENERGIA SA",
        "Vencimento: 15/08/2026",
        "Subtotal: R$ 1000,00",
        "ICMS: R$ 180,00",
        "Total: R$ 1180,00",
    ]
    y = 40
    for ln in lines:
        d.text((40, y), ln, fill="black", font=font)
        y += 50
    p = tmp_path / "fatura.png"
    img.save(p)

    text = ocr_image(p)
    assert "4521" in text
    assert "ENEL" in text.upper()
    assert "1180" in text.replace(",", "")


@pytest.mark.skipif(not tesseract_available(),
                    reason="tesseract não instalado")
def test_ocr_missing_binary_raises():
    from PIL import Image
    import tempfile, os
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "x.png"
        Image.new("RGB", (10, 10), "white").save(p)
        with pytest.raises(OCRError):
            ocr_image(p, tesseract_bin="/caminho/que/nao/existe")
