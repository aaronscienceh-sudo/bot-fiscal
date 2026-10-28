"""Modelos do supervisor (IA de governança que audita o bot).

Finding  -> um problema detectado, com evidência e sugestão de correção
AuditoriaReport -> resultado de uma rodada completa de auditoria
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field


class Severity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Finding(BaseModel):
    """Um problema encontrado pelo supervisor."""
    id: str = Field(description="Chave estável do finding (dedup entre rodadas)")
    check: str = Field(description="Nome do verificador que gerou o finding")
    severity: Severity
    message: str
    evidence: str = ""
    suggestion: str = ""
    count: int = Field(default=1, ge=1)
    source: str = Field(default="", description="Arquivo de origem (log, sessão…)")
    files: list[str] = Field(
        default_factory=list,
        description="Arquivos do usuário ligados ao achado (para mensagem simples no chat)")


class AuditoriaReport(BaseModel):
    generated_at: datetime = Field(default_factory=datetime.now)
    findings: list[Finding] = Field(default_factory=list)
    checks_run: list[str] = Field(default_factory=list)
    stats: dict[str, int] = Field(default_factory=dict)

    @property
    def by_severity(self) -> dict[str, int]:
        out = {s.value: 0 for s in Severity}
        for f in self.findings:
            out[f.severity.value] += 1
        return out

    @property
    def max_severity(self) -> Optional[Severity]:
        for s in (Severity.CRITICAL, Severity.HIGH, Severity.MEDIUM, Severity.LOW):
            if self.by_severity[s.value]:
                return s
        return None


@dataclass
class CheckContext:
    """Contexto passado a cada verificador."""
    cfg: dict = field(default_factory=dict)
    log_paths: list[Path] = field(default_factory=list)
    sessions_dir: Optional[Path] = None
    reports_dir: Optional[Path] = None
    sessions_cache: Optional[list] = field(default=None, repr=False)
    # restringe os checks de sessão a UM chat (auditoria ao vivo no bot)
    only_chat: Optional[str] = field(default=None)
