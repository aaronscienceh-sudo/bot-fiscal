"""Regras determinísticas: normalização, parsing de valores/datas,
matching e detecção de inconsistências.

Tudo aqui roda SEM LLM — é o coração confiável da conciliação.
O LLM só entra como reforço (classificação + matching fuzzy).
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher

from .models import (
    Inconsistency,
    InconsistencyType,
    Invoice,
    MatchResult,
    MatchStatus,
    Severity,
    Transaction,
)

# --------------------------------------------------------------------------- #
# Normalização de texto
# --------------------------------------------------------------------------- #
def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def normalize_name(s: str) -> str:
    """Minúsculas, sem acentos, sem pontuação, espaços colapsados."""
    s = strip_accents(s or "").lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def similarity(a: str, b: str) -> float:
    """Similaridade 0..1 entre dois nomes normalizados."""
    a, b = normalize_name(a), normalize_name(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    # substrings comuns (ex.: "enel" dentro de "enel energia")
    if a in b or b in a:
        return 0.9
    return SequenceMatcher(None, a, b).ratio()


# --------------------------------------------------------------------------- #
# Parsing de valores e datas (formato brasileiro + internacional)
# --------------------------------------------------------------------------- #
def parse_amount(value) -> Decimal:
    """Aceita Decimal, número, ou string '1.234,56' / '1,234.56' / '(100,00)'."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    s = str(value).strip().replace("R$", "").replace(" ", "")
    negative = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):      # 1.234.567,89  (BR)
            s = s.replace(".", "").replace(",", ".")
        else:                                 # 1,234,567.89  (US)
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        d = Decimal(s)
    except InvalidOperation:
        return Decimal("0")
    return -d if negative else d


def parse_date(value) -> date | None:
    if isinstance(value, date):
        return value
    s = str(value).strip()
    for fmt in ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d", "%d-%m-%Y", "%m/%d/%Y"):
        try:
            from datetime import datetime
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


# --------------------------------------------------------------------------- #
# Extração de referência (nº de fatura/nota) da descrição do movimento
# --------------------------------------------------------------------------- #
_REF_PATTERNS = [
    r"(?:nf|nfe|nota\s*fiscal|nota|fatura|fat|doc|documento|protocolo|ref|referencia|boleto)\s*[:#]?\s*(\d{3,})",
    r"#(\d{3,})",
    r"\b(\d{6,})\b",  # último recurso: número longo isolado
]


def extract_reference(description: str) -> str | None:
    text = (description or "").upper().replace("N F", "NF")
    for pat in _REF_PATTERNS:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return m.group(1)
    return None


# --------------------------------------------------------------------------- #
# Matching determinístico
# --------------------------------------------------------------------------- #
class DeterministicMatcher:
    """Emparelha transações com faturas por regras, em ordem de confiança."""

    def __init__(self, amount_tolerance: Decimal = Decimal("0.01"),
                 date_window_days: int = 15, name_threshold: float = 0.82):
        self.amount_tolerance = amount_tolerance
        self.date_window = timedelta(days=date_window_days)
        self.name_threshold = name_threshold

    def _amounts_equal(self, txn: Transaction, inv: Invoice) -> bool:
        return abs(abs(txn.amount) - inv.amount) <= self.amount_tolerance

    def match(self, txn: Transaction, invoices: list[Invoice]) -> tuple[Invoice | None, str, float]:
        """Retorna (fatura, método, confiança) ou (None, 'none', 0)."""

        # 1) referência exata
        if txn.reference:
            for inv in invoices:
                if inv.number == txn.reference:
                    return inv, "exact_ref", 0.99

        # 2) nome + valor exato
        for inv in invoices:
            if txn.counterparty and self._amounts_equal(txn, inv):
                sim = similarity(txn.counterparty, inv.issuer)
                if sim >= self.name_threshold:
                    return inv, "name_amount", min(0.98, 0.85 + sim / 10)

        # 3) valor exato + proximidade de data (vencimento)
        if txn.date:
            for inv in invoices:
                if self._amounts_equal(txn, inv) and inv.due_date:
                    if abs((txn.date - inv.due_date).days) <= self.date_window.days:
                        return inv, "amount_date", 0.80

        # 4) nome forte + valor próximo (tolerância maior p/ juros/multa)
        if txn.counterparty:
            best, best_sim = None, 0.0
            for inv in invoices:
                sim = similarity(txn.counterparty, inv.issuer)
                if sim > best_sim and abs(abs(txn.amount) - inv.amount) <= inv.amount * Decimal("0.10"):
                    best, best_sim = inv, sim
            if best and best_sim >= self.name_threshold:
                return best, "name_amount_fuzzy", round(0.6 + best_sim / 4, 2)

        return None, "none", 0.0


# --------------------------------------------------------------------------- #
# Detecção de inconsistências
# --------------------------------------------------------------------------- #
def detect_inconsistencies(
    matches: list[MatchResult],
    *,
    late_tolerance_days: int = 0,
    low_confidence_threshold: float = 0.5,
) -> list[Inconsistency]:
    incs: list[Inconsistency] = []

    for m in matches:
        txn, inv = m.transaction, m.invoice

        if m.status == MatchStatus.MATCHED:
            if txn is None or inv is None:
                continue
            # pagamento atrasado
            if inv.due_date and txn.date and (txn.date - inv.due_date).days > late_tolerance_days:
                dias = (txn.date - inv.due_date).days
                incs.append(Inconsistency(
                    type=InconsistencyType.LATE_PAYMENT,
                    severity=Severity.LOW if dias <= 3 else Severity.MEDIUM,
                    message=f"Fatura {inv.number} ({inv.issuer}) paga {dias} dia(s) após o vencimento.",
                    transaction=txn, invoice=inv,
                ))
            # confiança baixa (match feito por LLM)
            if m.confidence < low_confidence_threshold:
                incs.append(Inconsistency(
                    type=InconsistencyType.LOW_CONFIDENCE,
                    severity=Severity.MEDIUM,
                    message=f"Match de baixa confiança ({m.confidence:.0%}) entre {txn.id} e fatura {inv.number}.",
                    transaction=txn, invoice=inv,
                ))

        elif m.status == MatchStatus.DUPLICATE:
            if txn is None or inv is None:
                continue
            incs.append(Inconsistency(
                type=InconsistencyType.DUPLICATE_PAYMENT,
                severity=Severity.HIGH,
                message=f"Fatura {inv.number} ({inv.issuer}) de {inv.amount} paga mais de uma vez.",
                transaction=txn, invoice=inv,
                detail=m.explanation or "Pagamento duplicado detectado.",
            ))

        elif m.status == MatchStatus.AMOUNT_MISMATCH:
            if txn is None or inv is None:
                continue
            diff = abs(txn.amount) - inv.amount
            pct = (diff / inv.amount * 100) if inv.amount else Decimal(0)
            incs.append(Inconsistency(
                type=InconsistencyType.AMOUNT_MISMATCH,
                severity=Severity.HIGH if abs(pct) > 5 else Severity.MEDIUM,
                message=(f"Valor divergente: pago {abs(txn.amount)} vs fatura {inv.amount} "
                         f"(diferença {diff:+})."),
                transaction=txn, invoice=inv,
                detail=f"Método do match: {m.method}",
            ))

        elif m.status == MatchStatus.UNMATCHED_TRANSACTION:
            if txn is None:
                continue
            incs.append(Inconsistency(
                type=InconsistencyType.MISSING_INVOICE,
                severity=Severity.MEDIUM,
                message=f"Pagamento {txn.id} em {txn.date} de {abs(txn.amount)} sem fatura correspondente.",
                transaction=txn,
                detail=f"Descrição: {txn.description}",
            ))

        elif m.status == MatchStatus.UNMATCHED_INVOICE:
            if inv is None:
                continue
            incs.append(Inconsistency(
                type=InconsistencyType.MISSING_PAYMENT,
                severity=Severity.HIGH,
                message=f"Fatura {inv.number} ({inv.issuer}) de {inv.amount} sem pagamento identificado.",
                invoice=inv,
                detail=f"Vencimento: {inv.due_date}",
            ))

    # ordena por severidade (high primeiro)
    order = {Severity.HIGH: 0, Severity.MEDIUM: 1, Severity.LOW: 2}
    incs.sort(key=lambda i: (order[i.severity], i.type.value))
    return incs
