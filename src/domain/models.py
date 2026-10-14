"""Modelos de domínio da conciliação.

Transaction  -> linha de extrato bancário (movimento)
Invoice      -> fatura / nota fiscal a pagar
MatchResult  -> resultado do pareamento entre uma transação e uma fatura
Inconsistency-> alerta de inconsistência detectada
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class MatchStatus(str, Enum):
    MATCHED = "matched"
    AMOUNT_MISMATCH = "amount_mismatch"
    UNMATCHED_TRANSACTION = "unmatched_transaction"  # pagamento sem fatura
    UNMATCHED_INVOICE = "unmatched_invoice"          # fatura sem pagamento
    DUPLICATE = "duplicate"                          # pagamento duplicado


class Severity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class InconsistencyType(str, Enum):
    AMOUNT_MISMATCH = "amount_mismatch"
    MISSING_PAYMENT = "missing_payment"      # fatura sem pagamento
    MISSING_INVOICE = "missing_invoice"      # pagamento sem fatura
    DUPLICATE_PAYMENT = "duplicate_payment"
    LATE_PAYMENT = "late_payment"
    LOW_CONFIDENCE = "low_confidence"


class TaxLine(BaseModel):
    """Um imposto/contribuição listado na nota (ICMS, IPI, PIS, COFINS, ISSQN…)."""
    name: str
    base: Optional[Decimal] = Field(default=None, description="Base de cálculo")
    rate: Optional[Decimal] = Field(default=None, description="Alíquota como fração (0.18 = 18%)")
    amount: Optional[Decimal] = Field(default=None, description="Valor do imposto")
    estimated: bool = Field(default=False, description="True se o valor foi calculado pela tabela de alíquotas (mascarado na nota)")


class Transaction(BaseModel):
    """Um movimento do extrato bancário."""
    id: str
    date: date
    amount: Decimal = Field(description="Sinal negativo = débito/saída, positivo = crédito/entrada")
    description: str = ""
    reference: Optional[str] = Field(default=None, description="Nº de fatura/nota/protocolo, se houver")
    counterparty: Optional[str] = Field(default=None, description="Nome de quem enviou/recebeu")
    account: Optional[str] = None
    source_file: str = ""


class Invoice(BaseModel):
    """Uma fatura ou nota fiscal a pagar.

    Além dos campos clássicos de fatura, guarda os campos específicos de
    Nota Fiscal eletrônica (NFe/NFS-e/NFCe): tipo, série, chave/código de
    verificação, documentos do emitente e do tomador/destinatário,
    competência e o detalhamento dos impostos (tax_lines).
    """
    id: str
    number: str
    issuer: str = Field(description="Empresa emissora")
    issue_date: Optional[date] = None
    due_date: Optional[date] = Field(default=None, description="Vencimento")
    amount: Decimal = Field(description="Valor da fatura (sempre positivo)")
    description: Optional[str] = None
    category: Optional[str] = Field(default=None, description="Preenchido pela classificação (LLM)")
    source_file: str = ""

    # --- campos de Nota Fiscal (NFe / NFS-e / NFCe) ----------------------- #
    nf_type: Optional[str] = Field(default=None, description="NFe | NFS-e | NFCe | CF-e | Fatura")
    series: Optional[str] = None
    access_key: Optional[str] = Field(default=None, description="Chave de acesso (NFe) ou código de verificação (NFS-e)")
    issuer_doc: Optional[str] = Field(default=None, description="CNPJ/CPF do emitente/prestador")
    customer: Optional[str] = Field(default=None, description="Nome do tomador/destinatário")
    customer_doc: Optional[str] = Field(default=None, description="CNPJ/CPF do tomador/destinatário")
    competency: Optional[str] = Field(default=None, description="Competência (ex.: 11/2025)")
    tax_lines: list[TaxLine] = Field(default_factory=list, description="Detalhamento dos impostos")


class MatchResult(BaseModel):
    """Um pareamento. Em UNMATCHED_*, um dos lados é None."""
    transaction: Optional[Transaction] = None
    invoice: Optional[Invoice] = None
    status: MatchStatus
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    method: str = Field(default="none", description="exact_ref | name_amount | amount_date | name_amount_fuzzy | fuzzy_llm | none")
    explanation: Optional[str] = None


class Inconsistency(BaseModel):
    type: InconsistencyType
    severity: Severity
    message: str
    transaction: Optional[Transaction] = None
    invoice: Optional[Invoice] = None
    detail: Optional[str] = None


class ReconciliationReport(BaseModel):
    generated_at: datetime = Field(default_factory=datetime.now)
    period_start: Optional[date] = None
    period_end: Optional[date] = None
    total_transactions: int = 0
    total_invoices: int = 0
    matches: list[MatchResult] = Field(default_factory=list)
    inconsistencies: list[Inconsistency] = Field(default_factory=list)
    llm_used: bool = False

    # --- agregados convenientes -------------------------------------------
    @property
    def matched(self) -> list[MatchResult]:
        return [m for m in self.matches if m.status == MatchStatus.MATCHED]

    @property
    def by_severity(self) -> dict[str, int]:
        out = {s.value: 0 for s in Severity}
        for i in self.inconsistencies:
            out[i.severity.value] += 1
        return out

    @property
    def by_type(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for i in self.inconsistencies:
            out[i.type.value] = out.get(i.type.value, 0) + 1
        return out
