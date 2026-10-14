"""Alíquota interestadual de ICMS — fonte única da regra.

Resolução do Senado Federal 22/1989 (interestadual) e 13/2012 (importados):

  * 4%  — bens importados (conteúdo de importação > 40%), qualquer origem
          e destino;
  * 7%  — origem no Sul ou Sudeste (PR, SC, RS, SP, RJ, MG — exceto ES) com
          destino no Norte, Nordeste, Centro-Oeste ou ES;
  * 12% — todos os demais casos (origem no Norte, Nordeste, Centro-Oeste ou
          ES, e operações entre estados do Sul/Sudeste);
  * mesma UF -> não há alíquota interestadual nem DIFAL.

DIFAL = alíquota interna do destino − alíquota interestadual.
"""
from __future__ import annotations

from decimal import Decimal

# Grupo Sul/Sudeste (exceto ES): origem aqui + destino fora do grupo = 7%
UF_SUL_SUDESTE = frozenset({"PR", "SC", "RS", "SP", "RJ", "MG"})

# Norte, Nordeste, Centro-Oeste e ES
UF_RESTANTES = frozenset({
    "AC", "AL", "AM", "AP", "BA", "CE", "DF", "ES", "GO", "MA", "MT", "MS",
    "PA", "PB", "PE", "PI", "RO", "RR", "SE", "TO",
})

TODAS_AS_UFS = UF_SUL_SUDESTE | UF_RESTANTES

# Alíquota interna padrão de referência (%) por UF — apenas estados com
# padrão conhecido (PA fica de fora: alíquota varia por produto).
INTERNAL_RATES: dict[str, Decimal] = {
    "SP": Decimal("18"), "MG": Decimal("18"), "RJ": Decimal("22"),
    "PR": Decimal("18"), "SC": Decimal("17"), "RS": Decimal("17"),
    "BA": Decimal("18"), "PE": Decimal("18"), "CE": Decimal("18"),
    "DF": Decimal("18"), "GO": Decimal("17"), "ES": Decimal("18"),
    "MS": Decimal("17"), "MT": Decimal("17"), "MA": Decimal("21"),
    "PI": Decimal("20"), "AM": Decimal("20"), "AP": Decimal("20"),
    "RR": Decimal("19"), "AC": Decimal("19"), "AL": Decimal("17"),
    "PB": Decimal("19"), "RN": Decimal("18"), "SE": Decimal("18"),
    "TO": Decimal("17"), "RO": Decimal("19"),
}


def aliquota_interestadual(
    uf_origem: str, uf_destino: str, importado: bool = False
) -> Decimal | None:
    """Alíquota interestadual de ICMS (Res. SF 22/1989 e 13/2012).

    Retorna ``None`` quando origem e destino são a MESMA UF (operação
    interna: não existe alíquota interestadual nem DIFAL).
    """
    o = (uf_origem or "").strip().upper()
    d = (uf_destino or "").strip().upper()
    if o not in TODAS_AS_UFS or d not in TODAS_AS_UFS:
        raise ValueError(f"UF inválida: {uf_origem!r} -> {uf_destino!r}")
    if o == d:
        return None
    if importado:
        return Decimal("4")
    if o in UF_SUL_SUDESTE and d in UF_RESTANTES:
        return Decimal("7")
    return Decimal("12")


def calcular_difal(
    uf_origem: str, uf_destino: str, interna_destino: Decimal | str | float,
    importado: bool = False,
) -> Decimal | None:
    """DIFAL (%) = alíquota interna do destino − alíquota interestadual.

    ``None`` quando é operação interna (mesma UF).
    """
    inter = aliquota_interestadual(uf_origem, uf_destino, importado=importado)
    if inter is None:
        return None
    return Decimal(str(interna_destino)) - inter


__all__ = [
    "INTERNAL_RATES", "TODAS_AS_UFS", "UF_RESTANTES", "UF_SUL_SUDESTE",
    "aliquota_interestadual", "calcular_difal",
]
