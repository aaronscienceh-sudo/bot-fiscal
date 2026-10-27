"""Verificador de consistência da base de conhecimento e tabelas fiscais.

Valida a estrutura das regras (fiscal_kb), os números da tabela de alíquota
interestadual (icms_rates) e a tabela de ISSQN de SP — a matemática que
sustenta os pareceres do bot.
"""
from __future__ import annotations

from decimal import Decimal

from ...domain import fiscal_kb
from ...domain.icms_rates import (
    INTERNAL_RATES,
    TODAS_AS_UFS,
    aliquota_interestadual,
    calcular_difal,
)
from ...parsing.aliquotas.sp import (
    EXCECOES_CIDADES_SP,
    TABELA_PADRAO_SP,
    calcular_issqn,
    get_aliquota,
)
from ..models import CheckContext, Finding, Severity


def _f(id_: str, severity: Severity, message: str, evidence: str,
       suggestion: str, count: int = 1, source: str = "") -> Finding:
    return Finding(id=id_, check="kb_consistency", severity=severity,
                   message=message, evidence=evidence, suggestion=suggestion,
                   count=count, source=source)


def check_rules(rules: list) -> list[str]:
    """Problemas estruturais na lista de regras da KB."""
    errs: list[str] = []
    seen: dict[str, int] = {}
    for i, r in enumerate(rules):
        key = (r.title or "").strip().lower()
        seen[key] = seen.get(key, 0) + 1
        if not (r.keywords or ()):
            errs.append(f"regra '{r.title or i}' sem palavras-chave")
        if not (r.text or "").strip():
            errs.append(f"regra '{r.title or i}' sem texto de resposta")
        if r.topic is not None and r.topic not in (
                "difal", "icms", "iss", "pis_cofins", "ipi", "retencao",
                "aliquota", "regime", "reducao"):
            errs.append(f"regra '{r.title}' com topic desconhecido: {r.topic!r}")
    for title, n in seen.items():
        if n > 1:
            errs.append(f"título duplicado na KB: '{title}' ({n}×)")
    return errs


def check_icms() -> list[str]:
    """Sanidade da tabela de alíquota interestadual (Res. SF 22/1989 e 13/2012)."""
    errs: list[str] = []
    casos = [
        (("SP", "RJ"), Decimal("12")), (("RJ", "SP"), Decimal("12")),
        (("SP", "BA"), Decimal("7")), (("BA", "SP"), Decimal("12")),
        (("SP", "PA"), Decimal("7")), (("PA", "SP"), Decimal("12")),
        (("AM", "PA"), Decimal("12")), (("ES", "SP"), Decimal("12")),
        (("MG", "GO"), Decimal("7")), (("SC", "RS"), Decimal("12")),
    ]
    for (o, d), esperado in casos:
        got = aliquota_interestadual(o, d)
        if got != esperado:
            errs.append(f"aliquota_interestadual({o},{d}) = {got}, esperado {esperado}")
    for o, d in (("SP", "SP"), ("AM", "AM"), ("PA", "PA")):
        if aliquota_interestadual(o, d) is not None:
            errs.append(f"aliquota_interestadual({o},{d}) deveria ser None (interna)")
    for o, d in (("SP", "PA"), ("PA", "SP"), ("RJ", "AM")):
        got = aliquota_interestadual(o, d, importado=True)
        if got != Decimal("4"):
            errs.append(f"importado {o}->{d} = {got}, esperado 4")
    if aliquota_interestadual("SP", "SP", importado=True) is not None:
        errs.append("importado na mesma UF deveria ser None")
    if calcular_difal("MG", "PR", 18) != Decimal("6"):
        errs.append("calcular_difal(MG->PR, 18) deveria ser 6")
    if calcular_difal("SP", "AM", 20) != Decimal("13"):
        errs.append("calcular_difal(SP->AM, 20) deveria ser 13")
    if calcular_difal("SP", "SP", 18) is not None:
        errs.append("calcular_difal interna deveria ser None")
    for uf, rate in INTERNAL_RATES.items():
        if uf not in TODAS_AS_UFS:
            errs.append(f"INTERNAL_RATES com UF inválida: {uf}")
        if not (Decimal("0") < Decimal(str(rate)) <= Decimal("25")):
            errs.append(f"alíquota interna fora da faixa plausível: {uf}={rate}")
    return errs


def check_iss_table() -> list[str]:
    """Sanidade da tabela de ISSQN de SP (LC 116: mín 2% / máx 5%)."""
    errs: list[str] = []
    for tabela, nome in ((TABELA_PADRAO_SP, "TABELA_PADRAO_SP"),
                         ):
        for cod, rate in tabela.items():
            if not (0 < float(rate) <= 0.05):
                errs.append(f"{nome}[{cod}] = {rate} fora de (0, 0.05]")
    for cidade, tabela in EXCECOES_CIDADES_SP.items():
        for cod, rate in tabela.items():
            if not (0 < float(rate) <= 0.05):
                errs.append(f"exceção {cidade}[{cod}] = {rate} fora de (0, 0.05]")
    if get_aliquota("São Paulo", "14.01") != 0.05:
        errs.append("ISS São Paulo 14.01 deveria ser 5%")
    if get_aliquota("Campinas", "17.19") != 0.02:
        errs.append("ISS Campinas 17.19 deveria ser 2%")
    if get_aliquota("Cidade Inexistente", "01.01") != TABELA_PADRAO_SP["DEFAULT"]:
        errs.append("fallback de cidade desconhecida quebrou")
    r = calcular_issqn(1000.0, "São Paulo", "17.20")
    if abs(r["issqn"] - 30.0) > 0.001:
        errs.append(f"calcular_issqn(1000, SP, 17.20) = {r['issqn']}, esperado 30.0")
    return errs


def run(ctx: CheckContext) -> tuple[list[Finding], dict[str, int]]:
    findings: list[Finding] = []
    stats = {"kb_rules": len(fiscal_kb.RULES), "rule_errors": 0,
             "icms_errors": 0, "iss_errors": 0, "match_smoke_ok": 0}

    rule_errs = check_rules(fiscal_kb.RULES)
    stats["rule_errors"] = len(rule_errs)
    if rule_errs:
        findings.append(_f(
            "kb_consistency.rules", Severity.MEDIUM,
            "Regras da base de conhecimento com problema estrutural",
            " | ".join(rule_errs[:5])
            + (f" (+{len(rule_errs) - 5} mais)" if len(rule_errs) > 5 else ""),
            "Corrija as regras apontadas em src/domain/fiscal_kb.py.",
            count=len(rule_errs),
        ))

    smoke = [("qual a alíquota de ISS em São Paulo?", "iss"),
             ("o que é DIFAL?", "difal"),
             ("quanto pago de ICMS interestadual?", "icms")]
    try:
        for text, esperado in smoke:
            rule = fiscal_kb.match_rule(text)
            if rule is not None:
                stats["match_smoke_ok"] += 1
            elif esperado in ("iss", "difal"):
                findings.append(_f(
                    "kb_consistency.match_smoke", Severity.MEDIUM,
                    f"match_rule não achou regra para pergunta de rotina: {text!r}",
                    "A pergunta deveria casar com alguma regra da KB.",
                    "Confira as palavras-chave da regra correspondente em "
                    "src/domain/fiscal_kb.py.",
                ))
                break
    except Exception as e:  # noqa: BLE001 - KB não pode derrubar o bot
        findings.append(_f(
            "kb_consistency.match_crash", Severity.HIGH,
            "match_rule lançou exceção (derrubaria o atendimento de dúvidas)",
            f"{type(e).__name__}: {e}",
            "Corrija a regra problemática em src/domain/fiscal_kb.py.",
        ))

    icms_errs = check_icms()
    stats["icms_errors"] = len(icms_errs)
    if icms_errs:
        findings.append(_f(
            "kb_consistency.icms", Severity.HIGH,
            "Tabela de alíquota interestadual/DIFAL com erro",
            " | ".join(icms_errs[:5])
            + (f" (+{len(icms_errs) - 5} mais)" if len(icms_errs) > 5 else ""),
            "Os números da Res. SF 22/1989/13/2012 não batem — corrija "
            "src/domain/icms_rates.py antes que o bot responda errado.",
            count=len(icms_errs),
        ))

    iss_errs = check_iss_table()
    stats["iss_errors"] = len(iss_errs)
    if iss_errs:
        findings.append(_f(
            "kb_consistency.iss", Severity.HIGH,
            "Tabela de ISSQN de SP com alíquota fora da faixa legal (2%–5%)",
            " | ".join(iss_errs[:5])
            + (f" (+{len(iss_errs) - 5} mais)" if len(iss_errs) > 5 else ""),
            "Confira src/parsing/aliquotas/sp.py — ISS fora de 2%–5% gera "
            "parecer fiscal errado.",
            count=len(iss_errs),
        ))

    return findings, stats
