"""Registro dos verificadores do supervisor.

Cada verificador é um módulo com `run(ctx: CheckContext) -> (findings, stats)`.
Adicione novos checks aqui para que o runner os execute.
"""
from __future__ import annotations

from . import kb_consistency, log_health, parsing_quality, replay, session_sanity, tax_recalc

CHECKS = {
    "log_health": log_health.run,
    "parsing_quality": parsing_quality.run,
    "tax_recalc": tax_recalc.run,
    "replay": replay.run,
    "session_sanity": session_sanity.run,
    "kb_consistency": kb_consistency.run,
}

__all__ = ["CHECKS"]
