"""Tabelas de alíquotas por estado/cidade (ISSQN, LC 116/2003)."""
from .sp import EXCECOES_CIDADES_SP, TABELA_PADRAO_SP, calcular_issqn, get_aliquota

__all__ = ["TABELA_PADRAO_SP", "EXCECOES_CIDADES_SP", "get_aliquota", "calcular_issqn"]
