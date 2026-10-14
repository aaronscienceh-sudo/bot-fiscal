"""Pergunta fiscal genérica/hipotética com notas na conversa: a resposta é
ancorada nas notas REAIS da conversa (análise do tópico nas notas) + a
explicação da regra — nunca só a regra genérica.

Exceção: pergunta que descreve um CASO de DIFAL com origem/destino
("se eu comprar de SP pra MG, tem DIFAL?") recebe resposta direta sobre o
caso (difal_case) — não é ancorada nas notas da conversa.

Sem notas na conversa, a resposta continua sendo a regra genérica.
"""
from decimal import Decimal

from src.domain.models import TaxLine
from src.domain.tax import InvoiceAudit
from src.llm.tax_advisor import TaxAdvisor


def _nfe_difal() -> InvoiceAudit:
    return InvoiceAudit(
        source="nfe1.pdf", issuer="Fornecedor A", number="1234",
        nf_type="NFe", amount=Decimal("1180.00"), subtotal=Decimal("1000.00"),
        tax_lines=[
            TaxLine(name="ICMS", base=Decimal("1000.00"),
                    rate=Decimal("0.04"), amount=Decimal("40.00")),
            TaxLine(name="ICMS DIFAL", base=Decimal("1000.00"),
                    rate=Decimal("0.14"), amount=Decimal("140.00")),
        ],
    )


def _nfe_interna() -> InvoiceAudit:
    return InvoiceAudit(
        source="nfe2.pdf", issuer="Fornecedor B", number="5678",
        nf_type="NFe", amount=Decimal("1180.00"), subtotal=Decimal("1000.00"),
        tax_lines=[
            TaxLine(name="ICMS", base=Decimal("1000.00"),
                    rate=Decimal("0.18"), amount=Decimal("180.00")),
        ],
    )


def _nfse() -> InvoiceAudit:
    return InvoiceAudit(
        source="nfse3.pdf", issuer="Fornecedor C", number="90",
        nf_type="NFS-E", amount=Decimal("1030.00"), service_value=Decimal("1000.00"),
        city="São Paulo", service_code="17.20",
        tax_lines=[
            TaxLine(name="ISSQN", base=Decimal("1000.00"),
                    rate=Decimal("0.03"), amount=Decimal("30.00")),
        ],
    )


def test_difal_caso_hipotetico_responde_o_caso():
    # pergunta que descreve um CASO (origem/destino + natureza): resposta
    # direta sobre o caso — mesmo com notas na conversa, não é sequestrada
    # por elas. Sem saber se é revenda ou consumidor final, o bot PERGUNTA
    # (não responde "sim" direto)
    adv = TaxAdvisor()
    audits = [_nfe_difal(), _nfe_interna(), _nfse()]
    ans = adv.answer("se eu comprar de SP pra MG, tem DIFAL? me explica", audits)
    assert "Sim" not in ans
    assert "consumidor final" in ans.lower()
    assert "NFe 1234" not in ans
    # completa o caso (junção que o bot faz na conversa): SP -> MG 12%,
    # interna MG 18% -> DIFAL 6%
    ans2 = adv.answer(
        "se eu comprar de SP pra MG, tem DIFAL? me explica "
        "será para um consumidor final", audits)
    assert "6 pontos percentuais" in ans2
    assert "NFe 1234" not in ans2


def test_difal_generica_com_notas_ancorada():
    adv = TaxAdvisor()
    ans = adv.answer("o que é DIFAL?", [_nfe_difal(), _nfse()])
    assert "desta conversa" in ans
    assert "R$ 140,00" in ans
    assert "EC 87/2015" in ans


def test_difal_sem_notas_fica_generica():
    adv = TaxAdvisor()
    ans = adv.answer("o que é DIFAL?", [])
    assert "EC 87/2015" in ans
    # sem notas: não inventa análise de conversa
    assert "desta conversa" not in ans


def test_icms_hipotetico_ancorado():
    adv = TaxAdvisor()
    ans = adv.answer("como funciona o ICMS interestadual?", [_nfe_difal(), _nfse()])
    assert "NFe 1234" in ans
    assert "ICMS" in ans


def test_iss_hipotetico_ancorado():
    adv = TaxAdvisor()
    ans = adv.answer("me explica o ISS", [_nfse(), _nfe_difal()])
    assert "NFS-E 90" in ans
    assert "ISS" in ans


def test_retencao_hipotetica_ancorada():
    adv = TaxAdvisor()
    audits = [InvoiceAudit(
        source="nfse.pdf", issuer="F", number="77", nf_type="NFS-E",
        amount=Decimal("970.00"), service_value=Decimal("1000.00"),
        tax_lines=[
            TaxLine(name="ISSQN", base=Decimal("1000.00"),
                    rate=Decimal("0.03"), amount=Decimal("30.00")),
            TaxLine(name="IR RETIDO", amount=Decimal("30.00")),
        ],
    )]
    ans = adv.answer("o que é retenção na fonte?", audits + [_nfse()])
    assert "NFS-E 77" in ans
    assert "IR RETIDO" in ans
    assert "IN RFB 1.234/2012" in ans


def test_conceitual_sem_notas_ancoradas():
    # pergunta puramente conceitual (MEI) não é ancorada nas notas
    adv = TaxAdvisor()
    ans = adv.answer("o que é MEI?", [_nfe_difal()])
    assert "MEI (LC 128/2008)" in ans
    assert "desta conversa" not in ans


def test_nota_especifica_ainda_responde_da_nota():
    adv = TaxAdvisor()
    audits = [_nfe_difal(), _nfe_interna()]
    ans = adv.answer("o DIFAL da nota 1234?", audits)
    assert "NFe 1234" in ans
    assert "R$ 140,00" in ans
