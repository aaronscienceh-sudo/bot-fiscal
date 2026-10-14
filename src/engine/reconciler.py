"""Engine de conciliação.

Fluxo:
  1. Classifica as faturas (LLM ou fallback)
  2. Matching determinístico (referência, nome+valor, valor+data)
  3. Matching fuzzy via LLM para o que sobrou
  4. Detecta inconsistências
  5. (Opcional) gera explicação em linguagem natural via LLM
  6. Monta o relatório
"""
from __future__ import annotations

from decimal import Decimal

from ..domain.models import (
    Inconsistency,
    Invoice,
    MatchResult,
    MatchStatus,
    ReconciliationReport,
    Transaction,
)
from ..domain.rules import DeterministicMatcher, detect_inconsistencies
from ..llm.base import LLMClient


class Reconciler:
    def __init__(
        self,
        llm: LLMClient,
        matcher: DeterministicMatcher | None = None,
        *,
        amount_tolerance: Decimal = Decimal("0.01"),
        date_window_days: int = 15,
        name_threshold: float = 0.82,
        fuzzy_confidence_threshold: float = 0.55,
        late_tolerance_days: int = 0,
        low_confidence_threshold: float = 0.5,
    ):
        self.llm = llm
        self.matcher = matcher or DeterministicMatcher(
            amount_tolerance=amount_tolerance,
            date_window_days=date_window_days,
            name_threshold=name_threshold,
        )
        self.amount_tolerance = amount_tolerance
        self.fuzzy_threshold = fuzzy_confidence_threshold
        self.late_tolerance_days = late_tolerance_days
        self.low_confidence_threshold = low_confidence_threshold

    # ------------------------------------------------------------------ #
    def reconcile(self, transactions: list[Transaction],
                  invoices: list[Invoice]) -> ReconciliationReport:
        # 1) classificação
        for inv in invoices:
            inv.category = self.llm.classify_invoice(inv)

        matches: list[MatchResult] = []
        used: set[str] = set()
        pending_txns: list[Transaction] = []

        # 2) matching determinístico
        for txn in transactions:
            available = [i for i in invoices if i.id not in used]
            inv, method, conf = self.matcher.match(txn, available)
            if inv is not None:
                status = (MatchStatus.MATCHED
                          if abs(abs(txn.amount) - inv.amount) <= self.amount_tolerance
                          else MatchStatus.AMOUNT_MISMATCH)
                matches.append(MatchResult(
                    transaction=txn, invoice=inv, status=status,
                    confidence=conf, method=method,
                    explanation=f"Match por {method} (confiança {conf:.0%}).",
                ))
                used.add(inv.id)
                continue

            # não casou com fatura disponível: checa duplicidade de uma já conciliada
            used_invs = [i for i in invoices if i.id in used]
            dup, dup_method, dup_conf = self.matcher.match(txn, used_invs)
            if dup is not None and dup_conf >= 0.8:
                matches.append(MatchResult(
                    transaction=txn, invoice=dup, status=MatchStatus.DUPLICATE,
                    confidence=dup_conf, method=dup_method,
                    explanation=(f"Possível duplicidade: mesmo destino de um pagamento "
                                 f"já conciliado ({dup_method})."),
                ))
            else:
                pending_txns.append(txn)

        # 3) matching fuzzy via LLM para o que sobrou
        remaining = [i for i in invoices if i.id not in used]
        for txn in pending_txns:
            if not remaining:
                matches.append(MatchResult(
                    transaction=txn, invoice=None,
                    status=MatchStatus.UNMATCHED_TRANSACTION,
                    confidence=0.0, method="none",
                ))
                continue
            inv, conf, reason = self.llm.propose_match(txn, remaining)
            if inv is not None and conf >= self.fuzzy_threshold:
                status = (MatchStatus.MATCHED
                          if abs(abs(txn.amount) - inv.amount) <= self.amount_tolerance
                          else MatchStatus.AMOUNT_MISMATCH)
                matches.append(MatchResult(
                    transaction=txn, invoice=inv, status=status,
                    confidence=conf, method="fuzzy_llm",
                    explanation=reason or "Match sugerido pelo modelo.",
                ))
                used.add(inv.id)
                remaining.remove(inv)
            else:
                matches.append(MatchResult(
                    transaction=txn, invoice=None,
                    status=MatchStatus.UNMATCHED_TRANSACTION,
                    confidence=conf, method="none",
                ))

        # 4) faturas sem pagamento
        for inv in invoices:
            if inv.id not in used:
                matches.append(MatchResult(
                    transaction=None, invoice=inv,
                    status=MatchStatus.UNMATCHED_INVOICE,
                    confidence=0.0, method="none",
                ))

        # 5) inconsistências
        incs = detect_inconsistencies(
            matches,
            late_tolerance_days=self.late_tolerance_days,
            low_confidence_threshold=self.low_confidence_threshold,
        )

        # 6) explicação em linguagem natural (só se LLM disponível)
        if self.llm.available:
            for inc in incs:
                inc.detail = self.llm.explain(inc)

        # 7) relatório
        dates = [t.date for t in transactions if t.date]
        report = ReconciliationReport(
            period_start=min(dates) if dates else None,
            period_end=max(dates) if dates else None,
            total_transactions=len(transactions),
            total_invoices=len(invoices),
            matches=matches,
            inconsistencies=incs,
            llm_used=self.llm.available,
        )
        return report
