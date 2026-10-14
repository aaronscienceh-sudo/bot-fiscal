"""Interface da camada de IA.

Qualquer implementação (Ollama, fallback, outro endpoint) precisa expor:
  * available        -> bool
  * classify_invoice -> categoria da fatura
  * propose_match    -> sugestão de match fuzzy (fatura, confiança, motivo)
  * explain          -> explicação legível de uma inconsistência

O restante do pipeline não sabe qual implementação está em uso.
"""
from __future__ import annotations

from typing import Protocol

from ..domain.models import Inconsistency, Invoice, Transaction

# Categorias-alvo da classificação
CATEGORIES = [
    "Energia Elétrica",
    "Água / Saneamento",
    "Gás",
    "Internet / Telecom",
    "Aluguel / Locação",
    "Software / SaaS",
    "Tributos / Impostos",
    "Folha / Pessoal",
    "Fornecedores / Insumos",
    "Serviços Profissionais",
    "Marketing / Vendas",
    "Manutenção",
    "Outros",
]


class LLMClient(Protocol):
    available: bool

    def classify_invoice(self, invoice: Invoice) -> str: ...

    def propose_match(self, txn: Transaction, candidates: list[Invoice]) -> tuple[Invoice | None, float, str]: ...

    def explain(self, inc: Inconsistency) -> str: ...

    def extract_invoice_from_text(self, text: str) -> dict | None: ...
