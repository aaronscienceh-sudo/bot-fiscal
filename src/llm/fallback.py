"""Fallback determinístico — funciona SEM LLM.

Usado quando o Ollama não está disponível, ou como rede de segurança.
Classificação por palavras-chave, match fuzzy por similaridade de nome e
extração de fatura por heurísticas de regex (melhor esforço).
"""
from __future__ import annotations

import re
from decimal import Decimal

from ..domain.models import Inconsistency, Invoice, Transaction
from ..domain.rules import normalize_name, parse_amount, parse_date, similarity

CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "Energia Elétrica": ["energia", "luz", "enel", "light", "cpfl", "cemig",
                         "eletropaulo", "neoen", "equatorial", "celpe"],
    "Água / Saneamento": ["agua", "sabesp", "saneamento", "esgoto", "water", "cedae", "copel"],
    "Gás": ["gas", "comgas", "gasnorte", "gás natural"],
    "Internet / Telecom": ["internet", "telecom", "vivo", "claro", "tim", "fibra",
                           "telefone", "celular", "mobile", "oi "],
    "Aluguel / Locação": ["aluguel", "locacao", "renda", "imovel", "condominio",
                          "imobiliaria", "lote"],
    "Software / SaaS": ["software", "saas", "licenca", "assinatura", "cloud", "aws",
                        "azure", "google", "microsoft", "hosting", "dominio"],
    "Tributos / Impostos": ["imposto", "tributo", "darf", "das ", "iss", "icms", "irpj",
                            "contribuicao", "municipal", "federal", "guia", "receita"],
    "Folha / Pessoal": ["salario", "folha", "funcionario", "pro-labore", "prolabore",
                        "pagamento funcionario", "rh "],
    "Fornecedores / Insumos": ["fornecedor", "insumo", "mercadoria", "materia prima",
                               "compra", "distribuidora", "atacado"],
    "Serviços Profissionais": ["servico", "consultoria", "advocacia", "contabil",
                               "honorarios", "freela", "assessoria"],
    "Marketing / Vendas": ["marketing", "publicidade", "anuncio", "ads", "meta",
                           "vendas", "propaganda"],
    "Manutenção": ["manutencao", "reparo", "servico tecnico", "assistencia", "oficina"],
}


class FallbackClient:
    available = False

    def classify_invoice(self, invoice: Invoice) -> str:
        text = normalize_name(f"{invoice.issuer} {invoice.description or ''}")
        best_cat, best_hits = "Outros", 0
        for cat, kws in CATEGORY_KEYWORDS.items():
            hits = sum(1 for kw in kws if kw in text)
            if hits > best_hits:
                best_cat, best_hits = cat, hits
        return best_cat

    def propose_match(self, txn: Transaction, candidates: list[Invoice]) -> tuple[Invoice | None, float, str]:
        if not candidates or not txn.counterparty:
            return None, 0.0, ""
        best, best_sim = None, 0.0
        for c in candidates:
            sim = similarity(txn.counterparty, c.issuer)
            if sim > best_sim:
                best, best_sim = c, sim
        if best and best_sim >= 0.85:
            return best, round(0.5 + best_sim / 3, 2), f"similaridade de nome {best_sim:.0%}"
        return None, 0.0, ""

    def explain(self, inc: Inconsistency) -> str:
        return inc.message

    # ------------------------------------------------------------------ #
    def extract_invoice_from_text(self, text: str) -> dict | None:
        """Heurística de regex (melhor esforço) p/ extrair fatura do texto OCR.

        Sem LLM não dá para ler base/alíquota de forma confiável; extraímos o
        que for robusto (emissor, número, datas, total) e, quando possível,
        o valor do ICMS. A auditoria fiscal completa exige o LLM.
        """
        if not (text or "").strip():
            return None
        t = text

        def _search(pattern: str, flags: int = re.IGNORECASE) -> str | None:
            m = re.search(pattern, t, flags)
            return m.group(1).strip() if m else None

        def _iso(value: str | None) -> str | None:
            d = parse_date(value) if value else None
            return d.isoformat() if d else None

        number = (_search(r"(?:n[úu]mero|n[ºo°]|nota\s*fiscal|nfs-?e|nf-?e?|fatura)\s*[:#]?\s*(\d{3,})")
                  or _search(r"\b(\d{6,})\b"))
        issuer = (_search(r"(?:emitente|emissor|fornecedor|raz[oaõ]o\s*social)\s*[:\-]?\s*([A-Za-z0-9 .&/-]{4,60})")
                  or _search(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,4}(?:\s+LTDA|S\.A\.|SA|EPP|ME)?\b)", 0))
        due = _search(r"(?:vencimento|venc\.?|due)\s*[:\-]?\s*(\d{2}/\d{2}/\d{2,4})")
        issue = _search(r"(?:emiss[aoã]o|data\s+de\s+emiss[aoã]o)\s*[:\-]?\s*(\d{2}/\d{2}/\d{2,4})")
        amount_total = _search(
            r"(?<!sub)\btotal\b\s*(?:a\s*pagar|geral|do\s*documento)?\s*[:=]?\s*r?\$?\s*([\d.,]+)")
        amount_any = _search(r"r\$\s*([\d]{1,3}(?:\.\d{3})*,\d{2})")
        subtotal = _search(r"(?<!\w)subtotal\s*[:=]?\s*r?\$?\s*([\d.,]+)")
        service_code = _search(
            r"(?:c[óo]digo\s+(?:do\s+)?servi[çc]o|item\s+(?:da\s+)?lista|servi[çc]o)\s*[:#]?\s*"
            r"(\d{2}[.\-]\d{2})(?!\d)")
        city = (_search(r"munic[íi]pio\s+da\s+incid[êe]ncia\s+do\s+issqn\s*\n\s*([A-Za-zÀ-ÿ' .-]{3,40})")
                or _search(r"issqn\s*[-–:]\s*([A-Za-zÀ-ÿ' .-]{3,40})")
                or _search(r"(?:munic[íi]pio|prefeitura)\s*[:\-]?\s*([A-Za-zÀ-ÿ' .-]{3,40})"))

        # retenções na fonte: linhas "IR RETIDO: R$ 15,00" (viram linhas RETIDO)
        retention_lines: list[dict] = []
        for tax in ("IR", "CSLL", "PIS", "COFINS", "ISS"):
            m = re.search(rf"^{tax}\b[^\n]*\bretid[oa][^\n]*$", t, re.IGNORECASE | re.MULTILINE)
            if not m:
                continue
            am = re.search(r"([\d]{1,3}(?:\.\d{3})*,\d{2})", m.group(0))
            if am:
                retention_lines.append({"name": f"{tax} RETIDO",
                                        "amount": parse_amount(am.group(1))})
        tax_lines = list(retention_lines)
        # ISSQN (NFS-e): "Valor ISSQN: X", "Base Cálculo ISSQN: X", "Alíquota: X"
        issqn = None
        iss_amt = _search(r"valor\s+issqn\s*[:=]?\s*r?\$?\s*([\d.,]+)")
        iss_base = _search(r"base\s+c[áa]lculo\s+issqn\s*[:=]?\s*r?\$?\s*([\d.,]+)")
        iss_rate = _search(r"al[íi]quota\s*[:=]?\s*([\d.,]+)")
        if iss_amt or iss_base or iss_rate:
            issqn = {"name": "ISSQN",
                     "base": parse_amount(iss_base) if iss_base else None,
                     "rate": parse_amount(iss_rate) if iss_rate else None,
                     "amount": parse_amount(iss_amt) if iss_amt else None}
            tax_lines.append(issqn)
        for tax in ("ICMS", "IPI", "PIS", "COFINS", "ISS"):
            if tax == "ISS" and issqn is not None:
                continue  # ISSQN já capturado acima
            m = re.search(rf"^{tax}\b[^\n]*$", t, re.IGNORECASE | re.MULTILINE)
            segment = m.group(0) if m else ""
            if not segment:
                m2 = re.search(rf"{tax}\b[^\n]{{0,60}}", t, re.IGNORECASE)
                segment = m2.group(0) if m2 else ""
            if not segment:
                continue
            if re.search(r"retid", segment, re.IGNORECASE):
                continue  # já capturada como retenção

            value_m = re.search(
                r"valor\s*[:=]?\s*r?\$?\s*([\d]{1,3}(?:\.\d{3})*,\d{2})",
                segment, re.IGNORECASE)
            if value_m:
                amount_str = value_m.group(1)
            else:
                amounts = re.findall(r"([\d]{1,3}(?:\.\d{3})*,\d{2})", segment)
                amount_str = amounts[-1] if amounts else None
            if not amount_str:
                continue

            base_m = re.search(
                r"(?:base|bc)\s*[:=]?\s*r?\$?\s*([\d]{1,3}(?:\.\d{3})*,\d{2})",
                segment, re.IGNORECASE)
            rate_m = re.search(
                r"(?:al[íi]quota|taxa|rate)\s*[:=]?\s*([\d.,]+)\s*%?",
                segment, re.IGNORECASE)
            if not rate_m:
                rate_m = re.search(rf"{tax}\b[^0-9%]{{0,20}}?([\d.,]+)\s*%", segment, re.IGNORECASE)

            tax_lines.append({
                "name": tax.upper(),
                "base": parse_amount(base_m.group(1)) if base_m else None,
                "rate": parse_amount(rate_m.group(1)) if rate_m else None,
                "amount": parse_amount(amount_str),
            })

        # tipo do documento
        nf_type = None
        if re.search(r"nfs-?e\b|nota\s+fiscal\s+de\s+servi[çc]o", t, re.IGNORECASE):
            nf_type = "NFS-e"
        elif re.search(r"nfce\b|nota\s+fiscal\s+de\s+consumidor", t, re.IGNORECASE):
            nf_type = "NFCe"
        elif re.search(r"\bnfe\b|nf-?e\b|nota\s+fiscal\s+eletr[ôo]nica|danfe|chave\s+de\s+acesso",
                       t, re.IGNORECASE):
            nf_type = "NFe"

        # NFS-e: valor do serviço, líquido e total de retenções
        service_value = None
        m = re.search(r"(?:valor\s+do\s+servi[çc]o|servi[çc]o)\s*[:=]?\s*r?\$?\s*"
                      r"([\d]{1,3}(?:\.\d{3})*,\d{2})", t, re.IGNORECASE)
        if m:
            service_value = parse_amount(m.group(1))
        net_value = None
        m = re.search(r"(?:valor\s+l[íi]quido|l[íi]quido)\s*[:=]?\s*r?\$?\s*"
                      r"([\d]{1,3}(?:\.\d{3})*,\d{2})", t, re.IGNORECASE)
        if m:
            net_value = parse_amount(m.group(1))
        retentions = None
        m = re.search(r"(?:total\s+de\s+)?reten[çc][õo]es?\s*[:=]?\s*r?\$?\s*"
                      r"([\d]{1,3}(?:\.\d{3})*,\d{2})", t, re.IGNORECASE)
        if m:
            retentions = parse_amount(m.group(1))
        elif retention_lines:
            retentions = sum((r["amount"] or Decimal(0)) for r in retention_lines)

        # valor a pagar: NFS-e usa o líquido (após retenções); sem total explícito,
        # líquido > serviço > primeiro R$ encontrado
        if nf_type == "NFS-e":
            final_amount = (parse_amount(amount_total) if amount_total else None) \
                or net_value or service_value \
                or (parse_amount(amount_any) if amount_any else None)
        else:
            final_amount = (parse_amount(amount_total) if amount_total else None) \
                or (parse_amount(amount_any) if amount_any else None)

        regime = re.search(r"(simples nacional|lucro presumido|lucro real|\bMEI\b)", t, re.IGNORECASE)
        notes = (regime.group(1) + " — " if regime else "") + \
            "extração por heurística (sem LLM) — conferência limitada"
        if issuer and re.search(r"(simples nacional|lucro presumido|lucro real|\bMEI\b)",
                                issuer, re.IGNORECASE):
            issuer = None  # pegou o regime como nome do emitente

        if not (number or final_amount or issuer):
            return None
        return {
            "issuer": issuer,
            "number": number,
            "issue_date": _iso(issue),
            "due_date": _iso(due),
            "amount": final_amount,
            "subtotal": parse_amount(subtotal) if subtotal else None,
            "nf_type": nf_type,
            "tax_lines": tax_lines,
            "city": city,
            "service_code": service_code,
            "service_value": service_value,
            "retentions": retentions,
            "net_value": net_value,
            "notes": notes,
        }
