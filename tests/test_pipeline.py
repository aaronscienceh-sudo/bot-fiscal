"""Testes do pipeline de conciliação.

Cobre: parsing (CSV/OFX/PDF), regras (valores, datas, referência, matching),
detecção de inconsistências, classificação (fallback) e o fluxo ponta-a-ponta.
"""
from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.domain.models import (  # noqa: E402
    InconsistencyType,
    Invoice,
    MatchResult,
    MatchStatus,
    Severity,
    Transaction,
)
from src.domain.rules import (  # noqa: E402
    DeterministicMatcher,
    detect_inconsistencies,
    extract_reference,
    normalize_name,
    parse_amount,
    parse_date,
    similarity,
)
from src.engine import Reconciler  # noqa: E402
from src.llm import FallbackClient  # noqa: E402
from src.parsing import parse_file  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
# Regras: valores, datas, normalização, referência
# --------------------------------------------------------------------------- #
def test_parse_amount_br():
    assert parse_amount("1.234,56") == Decimal("1234.56")
    assert parse_amount("150,00") == Decimal("150.00")
    assert parse_amount("-150,00") == Decimal("-150.00")
    assert parse_amount("(100,00)") == Decimal("-100.00")
    assert parse_amount("R$ 850,00") == Decimal("850.00")


def test_parse_amount_us():
    assert parse_amount("1,234.56") == Decimal("1234.56")
    assert parse_amount(850) == Decimal("850")


def test_parse_date():
    assert parse_date("10/08/2026") == date(2026, 8, 10)
    assert parse_date("2026-08-10") == date(2026, 8, 10)
    assert parse_date("10/08/26") == date(2026, 8, 10)
    assert parse_date("invalido") is None


def test_normalize_name():
    assert normalize_name("Enel Energia") == "enel energia"
    assert normalize_name("  IMOBILIÁRIA   SILVA ") == "imobiliaria silva"


def test_similarity():
    assert similarity("Enel Energia", "ENEL ENERGIA") == 1.0
    assert similarity("Claro", "Claro Net") > 0.8
    assert similarity("Enel", "Sabesp") < 0.5


def test_extract_reference():
    assert extract_reference("PAG BOLETO ENEL ENERGIA NF 1001") == "1001"
    assert extract_reference("FATURA 4521") == "4521"
    assert extract_reference("PIX SEM REFERENCIA") is None


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def test_parse_csv_transactions():
    r = parse_file(ROOT / "data" / "inbox" / "extrato_banco.csv")
    assert len(r.transactions) == 8
    assert len(r.invoices) == 0
    assert not r.errors
    # primeira transação
    t = r.transactions[0]
    assert t.date == date(2026, 8, 10)
    assert t.amount == Decimal("-150.00")
    assert t.reference == "1001"


def test_parse_csv_invoices():
    r = parse_file(ROOT / "data" / "inbox" / "faturas.csv")
    assert len(r.invoices) == 7
    assert len(r.transactions) == 0
    inv = next(i for i in r.invoices if i.number == "1005")
    assert inv.issuer == "AWS"
    assert inv.amount == Decimal("850.00")
    assert inv.due_date == date(2026, 8, 25)


def test_parse_ofx():
    r = parse_file(ROOT / "data" / "examples" / "ofx" / "extrato.ofx")
    assert len(r.transactions) == 7
    assert not r.errors
    t = r.transactions[0]
    assert t.date == date(2026, 8, 10)
    assert t.amount == Decimal("-150.00")
    assert t.reference == "1001"
    assert t.counterparty == "ENEL ENERGIA"


def test_parse_pdf():
    r = parse_file(ROOT / "data" / "examples" / "pdf" / "faturas.pdf")
    assert len(r.invoices) == 3
    nums = {i.number for i in r.invoices}
    assert nums == {"2001", "2002", "2003"}
    inv = next(i for i in r.invoices if i.number == "2001")
    assert inv.issuer == "Enel Energia"
    assert inv.amount == Decimal("150.00")


# --------------------------------------------------------------------------- #
# Matching determinístico
# --------------------------------------------------------------------------- #
def _inv(num, issuer, amount, due=None):
    return Invoice(id=f"INV-{num}", number=num, issuer=issuer,
                   amount=Decimal(str(amount)), due_date=due)


def _txn(desc, amount, cp=None, ref=None, d=date(2026, 8, 10)):
    return Transaction(id="TXN-1", date=d, amount=Decimal(str(amount)),
                       description=desc, counterparty=cp, reference=ref)


def test_match_exact_ref():
    m = DeterministicMatcher()
    inv = _inv("1001", "Enel", 150)
    txn = _txn("PAG BOLETO NF 1001", -150, cp="Enel", ref="1001")
    got, method, conf = m.match(txn, [inv])
    assert got is inv and method == "exact_ref" and conf > 0.9


def test_match_name_amount():
    m = DeterministicMatcher()
    inv = _inv("1002", "Sabesp", 220.50)
    txn = _txn("TRANSF AGUA", -220.50, cp="Sabesp")
    got, method, conf = m.match(txn, [inv])
    assert got is inv and method == "name_amount"


def test_match_amount_date():
    m = DeterministicMatcher()
    inv = _inv("1003", "Claro Net", 199.90, due=date(2026, 8, 20))
    txn = _txn("DEB AUT", -199.90, cp="Outra Empresa", d=date(2026, 8, 22))
    got, method, conf = m.match(txn, [inv])
    assert got is inv and method == "amount_date"


def test_no_match():
    m = DeterministicMatcher()
    inv = _inv("1001", "Enel", 150)
    txn = _txn("PIX DESCONHECIDO", -500, cp="Loja XYZ")
    got, method, conf = m.match(txn, [inv])
    assert got is None and method == "none"


# --------------------------------------------------------------------------- #
# Detecção de inconsistências
# --------------------------------------------------------------------------- #
def test_detect_amount_mismatch():
    inv = _inv("1005", "AWS", 850)
    txn = _txn("PAG NF 1005", -800, ref="1005")
    matches = [MatchResult(
        transaction=txn, invoice=inv, status=MatchStatus.AMOUNT_MISMATCH,
        confidence=0.99, method="exact_ref",
    )]
    incs = detect_inconsistencies(matches)
    assert any(i.type == InconsistencyType.AMOUNT_MISMATCH for i in incs)
    assert any(i.severity == Severity.HIGH for i in incs)


def test_detect_missing_payment():
    inv = _inv("1007", "Vivo", 120)
    matches = [MatchResult(transaction=None, invoice=inv,
                           status=MatchStatus.UNMATCHED_INVOICE)]
    incs = detect_inconsistencies(matches)
    assert any(i.type == InconsistencyType.MISSING_PAYMENT for i in incs)


def test_detect_missing_invoice():
    txn = _txn("PIX LOJA", -500, cp="Loja XYZ")
    matches = [MatchResult(transaction=txn, invoice=None,
                           status=MatchStatus.UNMATCHED_TRANSACTION)]
    incs = detect_inconsistencies(matches)
    assert any(i.type == InconsistencyType.MISSING_INVOICE for i in incs)


def test_detect_late_payment():
    inv = _inv("1006", "Contabil", 1200, due=date(2026, 8, 30))
    txn = _txn("TRANSF NF 1006", -1200, ref="1006", d=date(2026, 9, 2))
    matches = [MatchResult(transaction=txn, invoice=inv,
                           status=MatchStatus.MATCHED, confidence=0.99,
                           method="exact_ref")]
    incs = detect_inconsistencies(matches)
    assert any(i.type == InconsistencyType.LATE_PAYMENT for i in incs)


def test_detect_duplicate():
    inv = _inv("1001", "Enel", 150)
    t1 = _txn("PAG NF 1001", -150, ref="1001", d=date(2026, 8, 10))
    t2 = _txn("PAG NF 1001", -150, ref="1001", d=date(2026, 8, 26))
    matches = [
        MatchResult(transaction=t1, invoice=inv, status=MatchStatus.MATCHED,
                    confidence=0.99, method="exact_ref"),
        MatchResult(transaction=t2, invoice=inv, status=MatchStatus.DUPLICATE,
                    confidence=0.99, method="exact_ref"),
    ]
    incs = detect_inconsistencies(matches)
    assert any(i.type == InconsistencyType.DUPLICATE_PAYMENT for i in incs)


# --------------------------------------------------------------------------- #
# Classificação (fallback)
# --------------------------------------------------------------------------- #
def test_fallback_classification():
    fb = FallbackClient()
    assert fb.classify_invoice(_inv("1", "Enel Energia", 100)) == "Energia Elétrica"
    assert fb.classify_invoice(_inv("2", "Sabesp", 100)) == "Água / Saneamento"
    assert fb.classify_invoice(_inv("3", "Imobiliária Silva", 100)) == "Aluguel / Locação"
    assert fb.classify_invoice(_inv("4", "AWS", 100)) == "Software / SaaS"


# --------------------------------------------------------------------------- #
# Pipeline ponta-a-ponta
# --------------------------------------------------------------------------- #
def _load(inbox: Path):
    from src.parsing import parse_directory
    txns, invs = [], []
    for r in parse_directory(inbox):
        txns.extend(r.transactions)
        invs.extend(r.invoices)
    return txns, invs


def test_end_to_end_main_inbox():
    txns, invs = _load(ROOT / "data" / "inbox")
    assert len(txns) == 8 and len(invs) == 7
    report = Reconciler(FallbackClient()).reconcile(txns, invs)

    types = {i.type for i in report.inconsistencies}
    assert InconsistencyType.AMOUNT_MISMATCH in types   # AWS 800 vs 850
    assert InconsistencyType.DUPLICATE_PAYMENT in types  # Enel 2x
    assert InconsistencyType.MISSING_PAYMENT in types    # Vivo sem pagamento
    assert InconsistencyType.MISSING_INVOICE in types    # PIX Loja XYZ
    assert InconsistencyType.LATE_PAYMENT in types       # Contabil 3 dias
    assert len(report.matched) == 5


def test_end_to_end_ofx_clean():
    txns, invs = _load(ROOT / "data" / "examples" / "ofx")
    assert len(txns) == 7 and len(invs) == 7
    report = Reconciler(FallbackClient()).reconcile(txns, invs)
    # tudo conciliado; só resta o atraso da 1006
    assert len(report.matched) == 7
    assert len(report.inconsistencies) == 1
    assert report.inconsistencies[0].type == InconsistencyType.LATE_PAYMENT
