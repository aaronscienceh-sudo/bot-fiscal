"""Auditoria fiscal de faturas: modelos + verificação determinística.

O LLM (ou o fallback) extrai de uma fatura — lida por OCR a partir de uma
foto, ou de um PDF — os campos de cada imposto (base, alíquota, valor) e os
totais. Esta camada RE-VERIFICA a aritmética de forma determinística, sem
confiar no modelo:

  * por imposto:  base × alíquota  ≈  valor informado
                  (ISS: alíquota calculada DIRETAMENTE pela tabela, cidade + cód. LC 116/2003)
  * total:        subtotal + Σ impostos  ≈  total da fatura (heurística)

Assim o cliente recebe um parecer objetivo: "o cálculo de impostos está
correto" ou "há N divergências", com os valores esperados vs. informados.

Para NFS-e com campos mascarados com asterisco (*), a classe
CalculadoraImpostosNFS NÃO tenta ler base/alíquota da nota: ela as deriva
sozinha — base = valor do serviço (raciocínio de base) e alíquota = tabela
de alíquotas (cidade + código LC 116/2003) — e calcula o valor do imposto
(base × alíquota), registrando divergências em observacao.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field

from .models import Invoice, Severity, TaxLine


def _dec(v) -> Optional[Decimal]:
    """Converte número/string em Decimal (ou None). Aceita '1.234,56' e '1234.56'."""
    if v is None:
        return None
    if isinstance(v, Decimal):
        return v
    s = str(v).strip().replace("R$", "").replace(" ", "")
    if not s or s.lower() in ("null", "none", "n/d", "-"):
        return None
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return Decimal(s)
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Regime tributário da emissora (identificação a partir da nota)
# ---------------------------------------------------------------------------
# Marcadores explícitos, em ordem de prioridade: (regex, regime, evidência).
# A evidência é o "porquê" mostrado ao cliente.
_REGIME_MARKERS: tuple[tuple[re.Pattern, str, str], ...] = (
    (re.compile(r"documento\s+emitido\s+por\s+me\s+ou\s+epp\s+optante\s+pel[oa]\s+simples", re.I),
     "Simples Nacional",
     'a nota traz o rodapé "DOCUMENTO EMITIDO POR ME OU EPP OPTANTE PELO SIMPLES NACIONAL"'),
    (re.compile(r"optante\s+pel[oa]\s+simples\s+nacional|optante\s+simples\s+nacional", re.I),
     "Simples Nacional",
     'a nota informa "OPTANTE" no campo "Situação do prestador perante o Simples Nacional"'),
    # DANFSe de sistema próprio (ex.: Sorocaba): "ME EPP - Simples Nacional" no
    # regime especial do ISSQN / "Simples Nacional na Data de Competência: Optante"
    (re.compile(r"me\s*epp\s*-\s*simples\s+nacional"
                r"|simples\s+nacional\s+na\s+data\s+de\s+compet[êe]ncia\s*\n\s*optante", re.I),
     "Simples Nacional",
     'a nota traz "Simples Nacional" no campo "Regime Especial de Tributação do ISSQN" '
     '("ME EPP - Simples Nacional")'),
    (re.compile(r"\bMEI\b|microempreendedor\s+individual", re.I),
     "MEI", 'a nota menciona "MEI" (Microempreendedor Individual)'),
    (re.compile(r"lucro\s+presumido", re.I),
     "Lucro Presumido", 'a nota menciona "Lucro Presumido"'),
    (re.compile(r"lucro\s+real", re.I),
     "Lucro Real", 'a nota menciona "Lucro Real"'),
)
_NAO_OPTANTE_RE = re.compile(r"\bn[ãa]o\s+optante\b", re.I)


def detect_regime(text: str) -> tuple[Optional[str], Optional[str]]:
    """Detecta o regime tributário da emissora no texto de uma nota.

    Retorna ``(regime, evidência)``:
      * regime explícito (Simples/MEI/Presumido/Real) + onde foi visto; ou
      * ``(None, evidência)`` quando a nota informa "NÃO OPTANTE" pelo Simples
        (regime Presumido/Real, não especificado); ou
      * ``(None, None)`` quando não há marcador.
    """
    if not (text or "").strip():
        return None, None
    for rx, regime, ev in _REGIME_MARKERS:
        if rx.search(text):
            return regime, ev
    if _NAO_OPTANTE_RE.search(text):
        return None, ('a nota informa "NÃO OPTANTE" no campo "Situação do prestador '
                      'perante o Simples Nacional"')
    return None, None


class TaxCheck(BaseModel):
    """Resultado de uma verificação de cálculo."""
    kind: str = Field(description="tax_math | rate | estimate | total | field")
    ok: bool
    expected: Optional[Decimal] = None
    actual: Optional[Decimal] = None
    diff: Optional[Decimal] = None
    message: str = ""
    severity: Severity = Severity.LOW


class TaxEstimate(BaseModel):
    """Estimativa de imposto mascarado, calculada por tabela de alíquotas."""
    name: str
    city: str
    service_code: str
    rate: Decimal
    expected: Decimal
    source: str = "TABELA"


class InvoiceAudit(BaseModel):
    """Parecer fiscal de uma fatura."""
    source: str = ""
    issuer: str = ""
    number: str = ""
    issue_date: Optional[date] = None
    due_date: Optional[date] = None
    amount: Optional[Decimal] = Field(default=None, description="Total a pagar da fatura")
    subtotal: Optional[Decimal] = Field(default=None, description="Subtotal antes dos impostos")
    tax_lines: list[TaxLine] = Field(default_factory=list)
    checks: list[TaxCheck] = Field(default_factory=list)

    # --- identificação da nota (NFe / NFS-e / NFCe) ----------------------- #
    nf_type: Optional[str] = Field(default=None, description="NFe | NFS-e | NFCe | Fatura")
    series: Optional[str] = None
    access_key: Optional[str] = Field(default=None, description="Chave de acesso / código de verificação")
    issuer_doc: Optional[str] = Field(default=None, description="CNPJ/CPF do emitente")
    customer: Optional[str] = Field(default=None, description="Nome do tomador/destinatário")
    customer_doc: Optional[str] = Field(default=None, description="CNPJ/CPF do tomador/destinatário")
    competency: Optional[str] = Field(default=None, description="Competência (ex.: 11/2025)")

    # --- composição do total (por tipo de documento) ----------------------- #
    service_value: Optional[Decimal] = Field(default=None, description="NFS-e: valor do serviço (bruto)")
    retentions: Optional[Decimal] = Field(default=None, description="NFS-e: retenções na fonte")
    net_value: Optional[Decimal] = Field(default=None, description="NFS-e: valor líquido")
    freight: Optional[Decimal] = Field(default=None, description="NFe: frete")
    insurance: Optional[Decimal] = Field(default=None, description="NFe: seguro")
    other_expenses: Optional[Decimal] = Field(default=None, description="NFe: outras despesas acessórias")
    st: Optional[Decimal] = Field(default=None, description="NFe: ICMS substituição tributária")
    discounts: Optional[Decimal] = Field(default=None, description="Descontos")

    # --- dados p/ estimar imposto mascarado (tabela de alíquotas) ----------- #
    city: Optional[str] = Field(default=None, description="Município da incidência do ISSQN")
    service_code: Optional[str] = Field(default=None, description="Código do serviço (tabela LC 116/2003)")
    notes: Optional[str] = Field(default=None, description="Observações da nota (ex.: regime Simples Nacional)")
    estimates: list[TaxEstimate] = Field(default_factory=list)

    # --- regime tributário da emissora -------------------------------------- #
    regime: Optional[str] = Field(
        default=None,
        description="Regime da emissora: Simples Nacional | MEI | Lucro Presumido | Lucro Real")
    regime_evidence: Optional[str] = Field(
        default=None,
        description="Como o regime foi identificado na nota (campo, rodapé, XML ou 'NÃO OPTANTE')")

    @property
    def problems(self) -> list[TaxCheck]:
        return [c for c in self.checks if not c.ok]

    @property
    def ok(self) -> bool:
        return not self.problems

    # --- retenções na fonte ------------------------------------------------ #
    @property
    def retention_lines(self) -> list[TaxLine]:
        """Linhas de retenção na fonte (ex.: 'IR RETIDO', 'PIS RETIDO', 'IRRF')."""
        pats = ("RET", "RETIDO", "RETEN", "IRRF")
        return [tl for tl in self.tax_lines
                if any(p in (tl.name or "").upper() for p in pats)]

    @property
    def has_retentions(self) -> bool:
        return bool(self.retention_lines) or (self.retentions is not None
                                              and self.retentions > 0)

    @property
    def total_retentions(self) -> Optional[Decimal]:
        """Total retido: soma das linhas de retenção (se houver), senão o campo
        agregado ``retentions`` (bloco de retenções da NFS-e)."""
        if self.retention_lines:
            total = sum((tl.amount or Decimal(0)) for tl in self.retention_lines)
            return total if total > 0 else None
        if self.retentions is not None and self.retentions > 0:
            return self.retentions
        return None


def _q(d: Decimal) -> Decimal:
    return d.quantize(Decimal("0.01"))


def _norm_rate(rate: Decimal) -> Decimal:
    """Se a alíquota veio como '18' (percentual), converte para 0.18."""
    return rate / Decimal(100) if rate > 1 else rate


def _is_iss_line(tl: TaxLine) -> bool:
    """Linha de ISSQN de fato (exclui retenções: 'ISS RETIDO' não é o ISS da operação)."""
    n = (tl.name or "").upper()
    return "ISS" in n and not any(p in n for p in ("RET", "IRRF"))


def _table_rate(audit: InvoiceAudit, tl: TaxLine) -> Decimal | None:
    """Alíquota ISSQN calculada diretamente pela tabela (cidade + código LC 116/2003)."""
    if not _is_iss_line(tl):
        return None
    if not (audit.city and audit.service_code):
        return None
    try:
        from ..parsing.aliquotas import get_aliquota
    except ImportError:
        return None
    return Decimal(str(get_aliquota(audit.city, audit.service_code)))


def _iss_base(audit: InvoiceAudit, tl: TaxLine) -> Optional[Decimal]:
    """Base do ISS (LC 116/2003 art. 2): a da linha, senão o valor do serviço
    (mesmo com a base mascarada com * na nota), senão o subtotal."""
    if tl.base is not None:
        return tl.base
    if audit.service_value is not None:
        return audit.service_value
    return audit.subtotal


def _estimate_masked(audit: InvoiceAudit, tl: TaxLine) -> TaxEstimate | None:
    """Estima imposto mascarado via tabela de alíquotas (cidade + código do serviço).

    A base pode vir mascarada na nota: nesse caso usa o valor do serviço
    (LC 116 art. 2) — o cálculo continua saindo da tabela.
    """
    if not _is_iss_line(tl):
        return None
    if not (audit.city and audit.service_code):
        return None
    base = _iss_base(audit, tl)
    if base is None:
        return None
    try:
        from ..parsing.aliquotas import calcular_issqn
    except ImportError:
        return None
    res = calcular_issqn(float(base), audit.city, audit.service_code)
    rate = Decimal(str(res["aliquota"]))
    return TaxEstimate(
        name=tl.name,
        city=audit.city,
        service_code=audit.service_code,
        rate=rate,
        expected=_q(base * rate),
        source=res["fonte"],
    )


def verify_invoice_taxes(audit: InvoiceAudit,
                         tolerance: Decimal = Decimal("0.05")) -> list[TaxCheck]:
    """Re-verifica a aritmética dos impostos e preenche audit.checks."""
    checks: list[TaxCheck] = []

    # 1) por imposto: base × alíquota ≈ valor
    #    Para ISS, a alíquota é calculada DIRETAMENTE pela tabela (cidade +
    #    código LC 116/2003); a alíquota impressa na nota só é comparada à tabela.
    for tl in audit.tax_lines:
        table_rate = _table_rate(audit, tl)
        # ISS: alíquota DIRETAMENTE pela tabela; base = valor do serviço (LC 116 art. 2)
        # mesmo quando a base vem mascarada com * na nota
        base = _iss_base(audit, tl) if table_rate is not None else tl.base
        if (base is not None and tl.amount is not None
                and (tl.rate is not None or table_rate is not None)):
            rate = table_rate if table_rate is not None else _norm_rate(tl.rate)
            origem = "tabela" if table_rate is not None else "nota"
            expected = _q(base * rate)
            diff = expected - tl.amount
            ok = abs(diff) <= tolerance
            checks.append(TaxCheck(
                kind="tax_math", ok=ok, expected=expected, actual=tl.amount,
                diff=Decimal("0.00") if ok else diff,
                message=(f"{tl.name}: base {base} × {rate:.2%} ({origem}) = {expected}, mas a fatura "
                         f"informa {tl.amount} (diferença {diff:+.2f})."
                         if not ok
                         else f"{tl.name}: cálculo conferido ({base} × {rate:.2%} ({origem}) = {expected})."),
                severity=Severity.HIGH if not ok else Severity.LOW,
            ))
            if table_rate is not None and tl.rate is not None:
                nota_rate = _norm_rate(tl.rate)
                if abs(nota_rate - table_rate) > Decimal("0.0001"):
                    checks.append(TaxCheck(
                        kind="rate", ok=False, expected=table_rate, actual=nota_rate,
                        diff=nota_rate - table_rate,
                        message=(f"{tl.name}: alíquota da nota ({nota_rate:.2%}) difere da tabela "
                                 f"({table_rate:.2%} — {audit.city}, cód. {audit.service_code})."),
                        severity=Severity.MEDIUM,
                    ))
        elif tl.amount is None:
            est = _estimate_masked(audit, tl)
            if est is not None:
                audit.estimates.append(est)
                # valor mascarado na nota -> preenche com o valor calculado pela tabela
                tl.amount = est.expected
                if tl.rate is None:
                    tl.rate = est.rate
                tl.estimated = True
                base = _iss_base(audit, tl)
                checks.append(TaxCheck(
                    kind="estimate", ok=True,
                    expected=est.expected,
                    message=(f"{tl.name}: valor mascarado na nota — calculado pela tabela de "
                             f"alíquotas ({est.city}, cód. {est.service_code}, {est.rate:.2%}) "
                             f"sobre base {base}: {est.expected}."),
                    severity=Severity.LOW,
                ))
            elif tl.base is not None and tl.rate is not None:
                # reconstrução genérica (IPI, ICMS, ICMS-ST, PIS, COFINS...):
                # valor ausente/mascarado, mas base e alíquota estão na nota
                rate = _norm_rate(tl.rate)
                valor = _q(tl.base * rate)
                tl.amount = valor
                tl.estimated = True
                checks.append(TaxCheck(
                    kind="estimate", ok=True,
                    expected=valor,
                    message=(f"{tl.name}: valor não identificado na nota — calculado pelos "
                             f"dados disponíveis (base {tl.base} × {rate:.2%} = {valor})."),
                    severity=Severity.LOW,
                ))
            else:
                checks.append(TaxCheck(
                    kind="field", ok=True,
                    message=(f"{tl.name}: valor não identificado no documento "
                             f"(ausente ou mascarado) — cálculo não pôde ser conferido."),
                    severity=Severity.LOW,
                ))
        else:
            checks.append(TaxCheck(
                kind="field", ok=True,
                message=(f"{tl.name}: valor {tl.amount} presente, mas base/alíquota não "
                         f"identificadas — cálculo não pôde ser conferido."),
                severity=Severity.LOW,
            ))

    # 2) total — a regra depende do tipo de documento
    if audit.amount is not None:
        nft = (audit.nf_type or "").upper()
        if nft in ("NFE", "NFCE"):
            # vNF = vProd + frete + seguro + outras + ST + IPI − desconto
            # (ICMS/PIS/COFINS são destacados e NÃO somam no total)
            extras = [v for v in (audit.freight, audit.insurance,
                                  audit.other_expenses, audit.st) if v is not None]
            if audit.subtotal is not None and extras:
                ipi = sum((tl.amount or Decimal(0)) for tl in audit.tax_lines
                          if (tl.name or "").upper() == "IPI")
                desc = audit.discounts or Decimal(0)
                expected_total = _q(audit.subtotal + sum(extras, Decimal(0)) + ipi - desc)
                diff = expected_total - audit.amount
                ok = abs(diff) <= tolerance
                checks.append(TaxCheck(
                    kind="total", ok=ok, expected=expected_total, actual=audit.amount,
                    diff=Decimal("0.00") if ok else diff,
                    message=(f"Total esperado {expected_total} (produtos {audit.subtotal} + "
                             f"frete/seguro/outras/ST/IPI − descontos), mas a nota informa "
                             f"{audit.amount} (diferença {diff:+.2f})."
                             if not ok
                             else f"Total consistente: produtos {audit.subtotal} + despesas = {audit.amount}."),
                    severity=Severity.MEDIUM if not ok else Severity.LOW,
                ))
        elif nft == "NFS-E":
            # líquido = serviço − retenções − descontos
            if audit.service_value is not None and audit.net_value is not None:
                desc = audit.discounts or Decimal(0)
                ret = audit.retentions or Decimal(0)
                expected_net = _q(audit.service_value - ret - desc)
                diff = expected_net - audit.net_value
                ok = abs(diff) <= tolerance
                checks.append(TaxCheck(
                    kind="total", ok=ok, expected=expected_net, actual=audit.net_value,
                    diff=Decimal("0.00") if ok else diff,
                    message=(f"Valor líquido esperado {expected_net} (serviço {audit.service_value} "
                             f"− retenções {ret} − descontos {desc}), mas a nota informa "
                             f"{audit.net_value} (diferença {diff:+.2f})."
                             if not ok
                             else f"Valor líquido consistente: {audit.service_value} − retenções/descontos = {audit.net_value}."),
                    severity=Severity.MEDIUM if not ok else Severity.LOW,
                ))
        elif audit.subtotal is not None and audit.tax_lines:
            # heurística genérica p/ faturas/OCR: subtotal + impostos ≈ total
            taxes_sum = sum((tl.amount or Decimal(0)) for tl in audit.tax_lines)
            expected_total = _q(audit.subtotal + taxes_sum)
            diff = expected_total - audit.amount
            ok = abs(diff) <= tolerance
            checks.append(TaxCheck(
                kind="total", ok=ok, expected=expected_total, actual=audit.amount,
                diff=Decimal("0.00") if ok else diff,
                message=(f"Total esperado {expected_total} (subtotal {audit.subtotal} + impostos "
                         f"{taxes_sum:.2f}), mas a fatura informa {audit.amount} (diferença {diff:+.2f})."
                         if not ok
                         else f"Total consistente: subtotal {audit.subtotal} + impostos = {audit.amount}."),
                severity=Severity.MEDIUM if not ok else Severity.LOW,
            ))

    order = {Severity.HIGH: 0, Severity.MEDIUM: 1, Severity.LOW: 2}
    checks.sort(key=lambda c: (0 if not c.ok else 1, order[c.severity]))
    audit.checks = checks
    return checks


# ---------------------------------------------------------------------------
# Formatação do parecer (usada pelo bot e pelo conselheiro fiscal)
# ---------------------------------------------------------------------------
def _brl(v: Decimal) -> str:
    s = f"{v:,.2f}"
    return "R$ " + s.replace(",", "\x00").replace(".", ",").replace("\x00", ".")


def _rate_pct(rate: Decimal) -> str:
    pct = rate / Decimal(100) if rate > 1 else rate
    return f"{pct * 100:.2f}".replace(".", ",") + "%"


def format_tax_lines(audit: InvoiceAudit) -> list[str]:
    """Uma linha por imposto, com base × alíquota = valor quando disponíveis.

    Para ISS, o valor é calculado DIRETAMENTE pela tabela de alíquotas
    (cidade + código LC 116/2003) — não pelo valor impresso na nota.
    """
    ok_math = {(c.expected, c.actual) for c in audit.checks
               if c.kind == "tax_math" and c.ok}
    out: list[str] = []
    for tl in audit.tax_lines:
        table_rate = _table_rate(audit, tl)
        if tl.amount is None:
            if tl.base is not None and tl.rate is not None:
                # valor mascarado, mas base e alíquota permitem reconstruí-lo
                rate = tl.rate / Decimal(100) if tl.rate > 1 else tl.rate
                valor = (tl.base * rate).quantize(Decimal("0.01"))
                out.append(f"• {tl.name}: base {_brl(tl.base)} × {_rate_pct(tl.rate)} "
                           f"= {_brl(valor)} (reconstruido) ✅")
            else:
                base = f"base {_brl(tl.base)}" if tl.base is not None else "base não identificada"
                out.append(f"• {tl.name}: {base} — valor mascarado/ausente no documento (*****)")
        elif tl.estimated:
            if table_rate is not None:
                base = _iss_base(audit, tl)
                base_txt = f"base {_brl(base)}" if base is not None else "base não identificada"
                out.append(f"• {tl.name}: {base_txt} × {_rate_pct(table_rate)} "
                           f"(tabela {audit.city}, cód. {audit.service_code}) = "
                           f"{_brl(tl.amount)} — mascarado na nota, valor calculado pela "
                           f"tabela de alíquotas")
            else:
                base = f"base {_brl(tl.base)}" if tl.base is not None else "base não identificada"
                rate = f" × {_rate_pct(tl.rate)}" if tl.rate is not None else ""
                out.append(f"• {tl.name}: {base}{rate} = {_brl(tl.amount)} (tabela) — "
                           f"mascarado na nota, valor calculado pela tabela de alíquotas")
        elif table_rate is not None:
            # ISS: valor calculado DIRETAMENTE pela tabela (cidade + cód. LC 116/2003)
            base = _iss_base(audit, tl)
            if base is None:
                out.append(f"• {tl.name}: {_brl(tl.amount)} — base não identificada, "
                           f"cálculo não conferido pela tabela")
            else:
                valor = _q(base * table_rate)
                mark = " ✅" if (valor, tl.amount) in ok_math else " ❌"
                line = (f"• {tl.name}: base {_brl(base)} × {_rate_pct(table_rate)} "
                        f"(tabela {audit.city}, cód. {audit.service_code}) = {_brl(valor)}{mark}")
                if abs(tl.amount - valor) > Decimal("0.05"):
                    line += f" — a nota informa {_brl(tl.amount)} (diferença {tl.amount - valor:+.2f})"
                out.append(line)
        elif tl.base is not None and tl.rate is not None:
            rate = tl.rate / Decimal(100) if tl.rate > 1 else tl.rate
            exp = (tl.base * rate).quantize(Decimal("0.01"))
            mark = " ✅" if (exp, tl.amount) in ok_math else " ❌"
            out.append(f"• {tl.name}: base {_brl(tl.base)} × {_rate_pct(tl.rate)} "
                       f"= {_brl(tl.amount)}{mark}")
        else:
            out.append(f"• {tl.name}: {_brl(tl.amount)}")
    return out


def audit_to_invoice(audit: InvoiceAudit, source_file: str) -> Invoice | None:
    """Converte o parecer em um Invoice p/ participar da conciliação."""
    amount = audit.amount
    if amount is None and audit.subtotal is not None:
        amount = audit.subtotal + sum((tl.amount or Decimal(0)) for tl in audit.tax_lines)
    if amount is None:
        return None
    h = hashlib.md5(f"{source_file}|{audit.number}|{audit.issuer}".encode()).hexdigest()[:8]
    return Invoice(
        id=f"INV-{h}",
        number=audit.number or "FOTO",
        issuer=audit.issuer or "Desconhecido",
        issue_date=audit.issue_date,
        due_date=audit.due_date,
        amount=amount,
        description=audit.issuer or None,
        source_file=source_file,
    )


# ---------------------------------------------------------------------------
# NFS-e — resolução das bases de cálculo por imposto (mesmo mascaradas com *)
# ---------------------------------------------------------------------------

@dataclass
class ItemImposto:
    """Linha de imposto de uma NFS-e. ``aliquota`` em percentual (2.0 = 2%)."""
    cod: str
    base: Optional[float] = None
    valor: Optional[float] = None
    aliquota: Optional[float] = None
    status: str = ""
    observacao: str = ""


@dataclass
class DadosNota:
    """Dados de uma NFS-e para resolução das bases de cálculo.

    ``base_servico`` e os valores de ``bases``/``impostos`` vêm como None
    quando o campo aparece mascarado com asterisco (*) na nota.
    """
    base_servico: Optional[float] = None
    base_servico_mascarada: bool = False
    # bases impressas na nota, por imposto (None = mascarada com *)
    bases: dict[str, Optional[float]] = field(default_factory=dict)
    # itens de imposto extraídos da nota (valor/alíquota impressos, se houver)
    impostos: dict[str, ItemImposto] = field(default_factory=dict)
    natureza_servico: str = "outros"
    regime_prestador: str = "desconhecido"
    municipio: Optional[str] = None
    codigo_servico_lc116: Optional[str] = None


class CalculadoraImpostosNFS:
    """Resolve as bases de cálculo e os valores dos impostos de uma NFS-e,
    mesmo mascarados com *."""

    # tolerâncias de divergência entre a base do imposto e a base do serviço:
    # impressa (evidência direta) vs. reconstruída (erro de arredondamento
    # do valor destacado é ampliado pela divisão pela alíquota)
    TOLERANCIA_IMPRESSA = 0.05
    TOLERANCIA_RECONSTRUIDA = 2.00

    # ------------------------------------------------------------------ #
    # Alíquota: da nota (se impressa) senão da TABELA (cidade + serviço) #
    # ------------------------------------------------------------------ #
    def aliquota_tabela(self, nota: DadosNota) -> Optional[float]:
        """Alíquota ISSQN pela TABELA (cidade + código LC 116/2003), em fração."""
        if not (nota.municipio and nota.codigo_servico_lc116):
            return None
        try:
            from ..parsing.aliquotas import get_aliquota
        except ImportError:
            return None
        return get_aliquota(nota.municipio, nota.codigo_servico_lc116)

    def _aliquota(self, item: Optional[ItemImposto], nota: DadosNota) -> Optional[float]:
        """Alíquota em fração (0.03 = 3%): da nota se impressa, senão da tabela."""
        if item is not None and item.aliquota is not None and item.aliquota > 0:
            return item.aliquota / 100.0  # na nota vem em percentual (2.0 = 2%)
        return self.aliquota_tabela(nota)  # tabela já vem em fração

    def resolver_base_imposto(self, cod_imposto: str, nota: DadosNota) -> tuple[float | None, str]:
        """
        Resolve a base de cálculo de UM imposto, mesmo mascarada com *.

        Raciocínio de base: a base do ISSQN é o VALOR DO SERVIÇO. Quando a
        base vem mascarada com * na nota, não se lê dela — usa-se o valor do
        serviço.

        Ordem:
          a) base impressa do próprio imposto -> "impressa_imposto"
          b) valor do serviço                 -> "base_servico"
          c) falha                            -> (None, "revisao_manual")
        """
        # a) base impressa do próprio imposto (quando não mascarada)
        base_impressa = nota.bases.get(cod_imposto)
        if base_impressa is not None:
            return base_impressa, "impressa_imposto"

        # b) raciocínio de base: base do ISSQN = valor do serviço
        if nota.base_servico is not None:
            return nota.base_servico, "base_servico"

        # c) sem valor do serviço
        return None, "revisao_manual"

    def resolver_valor_imposto(self, cod_imposto: str, nota: DadosNota) -> tuple[float | None, str]:
        """
        Resolve o valor de UM imposto, mesmo mascarado com *.

        Quando o valor vem mascarado com *, não se lê da nota: calcula
        base × alíquota, onde a base é o valor do serviço e a alíquota vem da
        TABELA (cidade + código LC 116/2003).

        Ordem:
          a) valor impresso na nota -> "impresso"
          b) base × alíquota        -> "reconstruido"
          c) falha                  -> (None, "revisao_manual")
        """
        item = nota.impostos.get(cod_imposto)
        # a) valor impresso na nota (quando não mascarado)
        if item is not None and item.valor is not None:
            return item.valor, "impresso"

        # b) base (valor do serviço) × alíquota (tabela quando mascarada)
        base, _origem = self.resolver_base_imposto(cod_imposto, nota)
        if base is not None:
            aliquota = self._aliquota(item, nota)
            if aliquota is not None and aliquota > 0:
                return round(base * aliquota, 2), "reconstruido"

        # c) sem dados suficientes
        return None, "revisao_manual"

    def reconstruir_impostos(self, nota: DadosNota) -> tuple[Optional[float], str]:
        """
        Reconstrói a BASE e o VALOR de cada imposto, mesmo mascarados com *,
        usando resolver_base_imposto() e resolver_valor_imposto(), e devolve a
        base do serviço.

        Para cada imposto (base = valor do serviço; alíquota = TABELA quando
        mascarada na nota):
          - se base is None e valor e alíquota existem: base = valor / alíquota
          - se valor is None e base e alíquota existem: valor = base × alíquota
        Escreve de volta: imposto.base = base_resolvida; imposto.valor = valor_resolvido,
        e atualiza item.status/item.observacao.

        A base do serviço é a informada (status "calculada") ou a média das
        bases resolvidas (status "reconstruida"). Registra divergência em
        observacao quando |base_imposta - base_servico| excede a tolerância:
        > 0.05 para base impressa, > 2.00 para base reconstruída.
        """
        # Resolve base e valor de cada imposto
        bases = {cod: self.resolver_base_imposto(cod, nota) for cod in nota.impostos}
        valores = {cod: self.resolver_valor_imposto(cod, nota) for cod in nota.impostos}

        # Preenchimento cruzado: base a partir de valor/alíquota e vice-versa
        # (alíquota da TABELA quando mascarada na nota)
        for cod, item in nota.impostos.items():
            base, origem_base = bases[cod]
            valor, origem_valor = valores[cod]
            aliquota = self._aliquota(item, nota)
            if (base is None and valor is not None
                    and aliquota is not None and aliquota > 0):
                base = round(valor / aliquota, 2)
                origem_base = "reconstruida_sozinha"
            if (valor is None and base is not None
                    and aliquota is not None and aliquota > 0):
                valor = round(base * aliquota, 2)
                origem_valor = "reconstruido"
            bases[cod] = (base, origem_base)
            valores[cod] = (valor, origem_valor)

        # Base do serviço: a informada ou a média das bases resolvidas
        if nota.base_servico is not None:
            base_servico: Optional[float] = nota.base_servico
            status = "calculada"
        else:
            candidatas = [b for b, _origem in bases.values() if b is not None]
            base_servico = round(sum(candidatas) / len(candidatas), 2) if candidatas else None
            status = "reconstruida" if candidatas else "revisao_manual"

        # Escreve de volta e registra divergências
        for cod, item in nota.impostos.items():
            base, origem_base = bases[cod]
            valor, origem_valor = valores[cod]
            item.base = base
            item.valor = valor
            # alíquota: da nota (se impressa) senão a derivada da TABELA (em %)
            aliquota_resolvida = self._aliquota(item, nota)
            if item.aliquota is None and aliquota_resolvida is not None:
                item.aliquota = round(aliquota_resolvida * 100, 4)
            # status: a pior parte prevalece
            if base is None and valor is None:
                item.status = "revisao_manual"
            elif "reconstru" in origem_base or "reconstru" in origem_valor:
                item.status = "reconstruido"
            else:
                item.status = "impresso"
            if base is None:
                if not item.observacao:
                    item.observacao = (f"Base de {cod} mascarada e sem valor do serviço "
                                       f"para derivar.")
                continue
            if valor is None:
                if not item.observacao:
                    item.observacao = (f"Valor de {cod} mascarado e sem alíquota na tabela "
                                       f"({nota.municipio or 'cidade'}, "
                                       f"{nota.codigo_servico_lc116 or 'cód. serviço'}).")
                continue
            if base_servico is None:
                continue
            tolerancia = (self.TOLERANCIA_IMPRESSA if origem_base == "impressa_imposto"
                          else self.TOLERANCIA_RECONSTRUIDA)
            if abs(base - base_servico) > tolerancia:
                item.observacao = (f"Base de {cod} ({base:.2f}) difere da base do "
                                   f"serviço ({base_servico:.2f}).")

        return base_servico, status
