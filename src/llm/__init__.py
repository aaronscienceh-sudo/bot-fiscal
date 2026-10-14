"""Fábrica do cliente de IA.

Tenta o Ollama (se configurado e disponível); senão, usa o fallback
determinístico. Assim o pipeline sempre funciona.
"""
from __future__ import annotations

from .base import CATEGORIES, LLMClient
from .fallback import FallbackClient
from .invoice_audit import audit_to_invoice, build_audit
from .ollama_client import OllamaClient
from .tax_advisor import TaxAdvisor


def make_llm(llm_cfg: dict) -> LLMClient:
    """llm_cfg: {'enabled': bool, 'base_url': str, 'model': str, 'timeout': float}"""
    if llm_cfg.get("enabled", True):
        client = OllamaClient(
            base_url=llm_cfg.get("base_url", "http://localhost:11434"),
            model=llm_cfg.get("model", "llama3.2"),
            timeout=float(llm_cfg.get("timeout", 180)),
            num_ctx=int(llm_cfg.get("num_ctx", 2048)),
        )
        if client.available:
            return client
    return FallbackClient()


__all__ = ["CATEGORIES", "LLMClient", "OllamaClient", "FallbackClient",
           "make_llm", "build_audit", "audit_to_invoice", "TaxAdvisor"]
