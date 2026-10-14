"""Conselheiro fiscal do bot: responde perguntas sobre tributos e notas.

Camadas, em ordem:
   0. Pergunta sobre o CASO de DIFAL do cliente ("empresa em BH prestando
      serviço para POA, vou pagar DIFAL?") -> resposta direta sobre o caso
      (difal_case), antes da busca de nota.
   0.5. Dúvida genérica anunciada ("tenho dúvida de DIFAL") sem nota citada e
      sem pergunta específica -> o bot não calcula sobre nota aleatória:
      pede para o cliente mandar a nota ou perguntar o ponto específico.
  1. Pergunta sobre uma NOTA da conversa -> resposta com os dados da
     auditoria fiscal (InvoiceAudit) + tabela LC 116/2003. Se o número
     citado não está na conversa, o bot diz que não a tem (e lista as que tem).
  2. Pergunta sobre o CONJUNTO das notas/extrato ("qual nota tem retenção?",
     "resumo dos impostos") -> resposta agregada com os dados da sessão.
  3. Pergunta que casa com a base de conhecimento fiscal -> resposta
     determinística pronta (funciona sem LLM). Se a conversa já tem notas e
     o tópico aparece nelas (DIFAL, ICMS, ISS, PIS/COFINS, IPI, retenções,
     alíquotas, regime, redução de base), a resposta é ancorada nas notas
     REAIS: análise do tópico nas notas da conversa + explicação da regra —
     a dúvida é tirada sobre o caso do cliente, nunca só com a teoria.
  4. LLM disponível (Ollama) -> resposta livre com as regras como contexto.
  5. Nada casou -> mensagem com os tópicos que o bot domina.
"""
from __future__ import annotations

import re
from decimal import Decimal

from ..domain.difal_case import analyze_difal_case, answer_difal_case
from ..domain.fiscal_kb import FiscalRule, is_tax_topic, match_rule
from ..domain.rules import normalize_name
from ..domain.tax import InvoiceAudit, TaxLine, _brl, _q, _rate_pct, format_tax_lines
from .base import LLMClient

_SEV_EMOJI = {"high": "🔴", "medium": "🟠", "low": "🟡"}


def _is_iss_name(name: str | None) -> bool:
    """Nome de linha de ISSQN de fato (exclui retenções: 'ISS RETIDO')."""
    n = (name or "").upper()
    return "ISS" in n and not any(p in n for p in ("RET", "IRRF"))


class TaxAdvisor:
    """Responde perguntas fiscais usando as regras (LC 116 etc.) e a sessão."""

    def __init__(self, llm: LLMClient | None = None):
        self.llm = llm

    # ------------------------------------------------------------------ #
    # Detecção
    # ------------------------------------------------------------------ #
    def is_tax_question(self, text: str, audits: list[InvoiceAudit]) -> bool:
        if self._find_audit(text, audits) is not None:
            return True
        # cita uma nota por número ("qual o valor da nota 999?") -> responde
        # mesmo sem palavra fiscal: ou a nota está na conversa ou eu digo que não a tenho
        if self._referenced_number(text) is not None:
            return True
        # aponta para UMA nota da conversa sem número ("a nota anterior tem
        # ISS?", "a última tem retenção?") -> é sobre aquela nota
        if self._anaphoric_audit(text, audits) is not None \
                and self._ATTR_RE.search(normalize_name(text)):
            return True
        return is_tax_topic(text)

    # ------------------------------------------------------------------ #
    # Resposta
    # ------------------------------------------------------------------ #
    CLOSING = "\n\nPosso ajudar com mais alguma coisa ou você tem mais alguma dúvida?"

    _TOL = Decimal("0.05")

    @staticmethod
    def _pct(v: Decimal) -> str:
        return f"{v:.2%}".replace(".", ",")

    def _iss_tabela(self, a: InvoiceAudit, tl: TaxLine | None = None):
        """ISS calculado DIRETAMENTE pela tabela de alíquotas (cidade + cód. LC 116/2003).

        Retorna ``(alíquota, base, valor, cidade, código)`` ou None sem
        cidade/código. Base: a do imposto, senão o valor do serviço
        (LC 116 art. 2 — vale mesmo com a base mascarada na nota), senão o subtotal.
        """
        if not (a.city and a.service_code):
            return None
        try:
            from ..parsing.aliquotas import get_aliquota
        except ImportError:
            return None
        rate = Decimal(str(get_aliquota(a.city, a.service_code)))
        base = tl.base if tl is not None and tl.base is not None else None
        if base is None:
            for x in a.tax_lines:
                n = (x.name or "").upper()
                if "ISS" in n and not any(p in n for p in ("RET", "IRRF")) \
                        and x.base is not None:
                    base = x.base
                    break
        if base is None:
            base = a.service_value
        if base is None:
            base = a.subtotal
        if base is None:
            return None  # sem base não há cálculo pela tabela
        valor = (base * rate).quantize(Decimal("0.01"))
        return rate, base, valor, a.city, a.service_code

    def answer(self, text: str, audits: list[InvoiceAudit],
               transactions: list | None = None,
               last_discussed: InvoiceAudit | None = None) -> str:
        return self.answer_with_target(text, audits, transactions,
                                       last_discussed)[0]

    def answer_with_target(self, text: str, audits: list[InvoiceAudit],
                           transactions: list | None = None,
                           last_discussed: InvoiceAudit | None = None
                           ) -> tuple[str, InvoiceAudit | None]:
        """Resposta + a nota sobre a qual ela foi feita.

        ``target`` é a nota específica quando a pergunta aponta para UMA nota
        (número, pronome, nota em pauta); None quando a resposta é sobre o
        conjunto, uma regra geral ou o caso hipotético do cliente. O agente
        usa o target para lembrar qual nota está em pauta na conversa.
        """
        t = normalize_name(text)
        # aponta para UMA nota da conversa ("a nota anterior tem ISS?", "a última
        # tem retenção?", "a de antes tem DIFAL?"): resolvo AQUELA nota e respondo
        # só sobre ela — sem cair no resumo/ancoragem de TODAS as notas, que é o
        # que fazia a resposta fugir para as outras notas.
        anaphoric = self._anaphoric_audit(text, audits, last_discussed)
        if anaphoric is not None:
            return self._answer_invoice(text, anaphoric) + self.CLOSING, anaphoric
        # CASO de DIFAL descrito pelo cliente ("tenho empresa em BH e vou prestar
        # serviço para uma empresa de POA, vou pagar DIFAL?"): resposta direta
        # sobre o caso — ANTES da busca de nota, para a pergunta hipotética não
        # ser sequestrada por uma nota que esteja na conversa
        if "difal" in t and self._referenced_number(text) is None \
                and not self._PRON_RE.search(t) \
                and not (audits and self._NOTE_REF_RE.search(t)):
            case = analyze_difal_case(text)
            # "nota" genérico dentro do caso ("vou pagar DIFAL se emitir
            # nota contra ele?") não é referência a nota da conversa: com
            # origem E destino citados, a pergunta é um caso. Com 1 UF,
            # menção a documento prefere a interpretação de nota
            # ("tem DIFAL na nota de BH?")
            if case is not None and (not self._DOC_RE.search(t)
                                     or (case.origin and case.destination)):
                return answer_difal_case(case, text=text) + self.CLOSING, None
        # dúvida genérica anunciada ("tenho dúvida de DIFAL") com notas na
        # conversa: sem nota citada e sem pergunta específica, o bot raciocina
        # a intenção ANTES de calcular — não sequestra a dúvida com uma nota
        # aleatória; pede para o cliente mandar a nota ou perguntar o que quer
        vague = self._VAGUE_RE.search(t)
        if vague is not None and audits and self._referenced_number(text) is None \
                and not self._DOC_RE.search(t) and not self._PRON_RE.search(t) \
                and not self._DEFIN_RE.search(t) and not self._COLL_RE.search(t):
            return self._vague_topic_reply(vague.group(1)) + self.CLOSING, None
        # "uma nota minha" sem dizer qual, com várias notas na conversa:
        # NÃO supor qual nota o cliente quer — mostro o resumo dos impostos de
        # cada uma e peço o número (responder sobre a nota errada é pior)
        if audits and len(audits) > 1 and self._referenced_number(text) is None \
                and not self._COLL_RE.search(t) and self._AMBIG_NOTE_RE.search(t):
            return self._which_note_reply(audits), None
        audit = self._find_audit(text, audits, last_discussed)
        if audit is not None:
            return self._answer_invoice(text, audit) + self.CLOSING, audit
        num = self._referenced_number(text)
        if num is not None:
            return self._missing_invoice(num, audits) + self.CLOSING, None
        # SEM número de nota citado, a pergunta NÃO é sobre uma nota específica:
        # o bot responde objetivamente (conjunto das notas ou regra) e nunca
        # supõe/adivinha qual nota o cliente quer.
        # conjunto das notas / extrato ("qual nota tem retenção?", "resumo dos impostos")
        if self._COLL_RE.search(t):
            if "extrato" in t and transactions:
                return self._answer_extrato(transactions) + self.CLOSING, None
            if audits:
                # "quais..." sem citar nota ("Quais as penalidades do SPED?") é
                # pergunta GERAL: responde pela regra da base de conhecimento,
                # não sobre o conjunto das notas
                if not self._DOC_ANY_RE.search(t):
                    rule = match_rule(text)
                    if rule is not None:
                        grounded = self._grounded_rule(rule, audits)
                        if grounded is not None:
                            return grounded + self.CLOSING, None
                        return rule.text + self.CLOSING, None
                return self._answer_collection(t, audits) + self.CLOSING, None
        rule = match_rule(text)
        if rule is not None:
            # pergunta fiscal genérica/hipotética com notas na conversa
            # ("como funciona o DIFAL?", "me explica DIFAL"): não respondo só com a
            # regra genérica — analiso o tópico nas notas REAIS da conversa e
            # depois explico a regra, tirando a dúvida sobre o caso do cliente
            grounded = self._grounded_rule(rule, audits)
            if grounded is not None:
                return grounded + self.CLOSING, None
            return rule.text + self.CLOSING, None
        reply = self._llm_answer(text, audits)
        if reply:
            return reply + self.CLOSING, None
        return (
            "🤔 Não encontrei uma resposta pronta para essa pergunta. Se for sobre tributos, "
            "tente citar o nome do imposto (ISS, ICMS, DIFAL, PIS, COFINS, IPI, IRPJ, CSLL) "
            "ou o número da nota — assim eu localizo a resposta.\n\n"
            "O que eu domino:\n"
            "• ISS e a LC 116/2003 (lista de serviços, alíquotas, retenções, local de incidência)\n"
            "• ICMS, ICMS-ST, DIFAL, redução de base — e a diferença entre ICMS e ISS\n"
            "• PIS, COFINS, IPI, IRPJ, CSLL\n"
            "• Regimes: MEI, Simples Nacional, Lucro Presumido, Lucro Real "
            "(alíquota por faturamento, limites e o que acontece ao estourar)\n"
            "• IRPF: cálculo (carnê-leão), rescisão trabalhista, ganho de capital, DIRPF\n"
            "• MEI + CLT ao mesmo tempo, pensionista, empregado no MEI\n"
            "• Retenções na fonte (IR, CSLL, PIS, COFINS, ISS)\n"
            "• Multa/juros por atraso e parcelamento de débitos\n"
            "• Uma nota específica: 'qual o ISS da nota 123?', 'tem retenção na NF 456?'\n"
            "• O conjunto das notas/extrato: 'qual nota tem retenção?', 'resumo dos impostos'"
            + self.CLOSING
        ), None

    def answer_for_document(self, text: str, audits: list[InvoiceAudit]) -> str:
        """Pergunta na legenda de um documento/foto que o cliente acabou de
        enviar: a nota está identificada pelo CONTEXTO (é a que ele mandou),
        então vale responder sobre ela mesmo sem número. Com uma nota, responde
        sobre ela; com várias, sobre o conjunto. (Perguntas de chat sem número
        seguem para ``answer`` e respondem objetivamente, sem supor a nota.)"""
        if len(audits) == 1:
            return self._answer_invoice(text, audits[0]) + self.CLOSING
        if audits:
            return self._answer_collection(normalize_name(text), audits) + self.CLOSING
        return self.answer(text, audits)

    def _vague_topic_reply(self, topic: str) -> str:
        """Dúvida genérica anunciada: pedir a nota ou a pergunta específica
        (em vez de calcular sobre uma nota aleatória da conversa)."""
        name = self._VAGUE_DISPLAY.get(topic, topic.upper())
        return (
            f"Claro! Me diz o que exatamente você quer saber sobre {name}:\n"
            "• Se for sobre uma nota sua: envie a nota (foto ou arquivo) ou cite "
            "o número dela (ex.: \"qual o DIFAL da nota 123?\").\n"
            "• Se for uma dúvida em geral: pergunte o ponto específico "
            "(ex.: \"o que é DIFAL?\", \"quando incide DIFAL?\")."
        )

    # -- regra fiscal ancorada nas notas da conversa ------------------------ #
    # Tópico da regra -> método que analisa o tópico nas notas da conversa.
    # Pergunta genérica/hipotética sobre um desses tópicos vira: análise das
    # notas reais (valores, base, alíquota) + explicação da regra — a dúvida é
    # tirada sobre o caso do cliente, não só com a teoria.
    _TOPIC_METHODS = {
        "difal": "_collection_difal",
        "icms": "_collection_icms",
        "iss": "_collection_iss",
        "pis_cofins": "_collection_pis_cofins",
        "ipi": "_collection_ipi",
        "retencao": "_collection_retentions",
        "aliquota": "_collection_aliquotas",
        "regime": "_collection_regime",
        "reducao": "_collection_reducao",
    }

    def _grounded_rule(self, rule: FiscalRule, audits: list[InvoiceAudit]) -> str | None:
        """Regra fiscal ancorada nas notas da conversa (ou None se não dá para
        ancorar: sem notas na conversa ou tópico puramente conceitual)."""
        if not audits or rule.topic is None or rule.topic not in self._TOPIC_METHODS:
            return None
        method = getattr(self, self._TOPIC_METHODS[rule.topic])
        analysis = method(audits)
        return analysis + "\n\n" + rule.text

    @staticmethod
    def _tax_line_detail(tl: TaxLine) -> str:
        """Detalhe de uma linha de imposto: valor (+ base e alíquota, se houver)."""
        if tl.amount is not None:
            detail = f"{tl.name} {_brl(tl.amount)}"
        else:
            detail = f"{tl.name} (valor não identificado na nota)"
        parts = []
        if tl.base is not None:
            parts.append(f"base {_brl(tl.base)}")
        if tl.rate is not None:
            parts.append(f"alíquota {_rate_pct(tl.rate)}")
        if parts:
            detail += f" ({', '.join(parts)})"
        return detail

    # -- relatório completo da nota recebida (todos os impostos) ----------- #
    def full_report(self, a: InvoiceAudit) -> str:
        """Parecer completo de uma nota recebida.

        Mostra TODOS os impostos da nota (base × alíquota = valor, com
        conferência ✅/❌), o regime tributário da emissora com a explicação do
        porquê, e, em NFS-e, as retenções na fonte — destacadas na nota (com
        verificação) ou calculadas sobre o valor do serviço
        (valores esperados: IR, CSLL, PIS, COFINS, ISS).
        """
        head = f"🧾 {a.nf_type or 'Fatura'} {a.number}".strip()
        if a.issuer:
            head += f" — {a.issuer}"
        lines = [head]

        nft = (a.nf_type or "").upper()
        vals = []
        if a.amount is not None:
            vals.append(f"total {_brl(a.amount)}")
        if nft == "NFS-E" and a.service_value is not None:
            vals.append(f"serviço {_brl(a.service_value)}")
        elif a.subtotal is not None and a.subtotal != a.amount:
            vals.append(f"produtos {_brl(a.subtotal)}")
        if a.freight is not None and a.freight:
            vals.append(f"frete {_brl(a.freight)}")
        if vals:
            lines.append("💰 " + " | ".join(vals))

        lines.append("")
        lines.append("📊 Impostos da nota:")
        tax_lines = self._tax_lines_full(a)
        lines.extend(tax_lines or ["• sem detalhes de impostos destacados na nota."])

        lines.append("")
        lines.append("🧭 Regime da emissora:")
        lines.append(self._regime_da_emissora(a))

        if nft == "NFS-E":
            lines.append("")
            lines.append("✂️ Retenções na fonte:")
            lines.extend(self._retention_lines(a))

        lines.append("")
        lines.extend(self._veredicto_lines(a))
        return "\n".join(lines)

    def _tax_lines_full(self, a: InvoiceAudit) -> list[str]:
        """Todos os impostos da nota (retenções ficam na seção própria), com o
        cálculo base × alíquota = valor e, sem alíquota, a alíquota efetiva.
        Para ISS, o valor é calculado DIRETAMENTE pela tabela (cidade + cód. LC 116)."""
        ok_math = {(c.expected, c.actual) for c in a.checks
                   if c.kind == "tax_math" and c.ok}
        ret_ids = {id(tl) for tl in a.retention_lines}
        out: list[str] = []
        for tl in a.tax_lines:
            if id(tl) in ret_ids:
                continue
            iss_t = self._iss_tabela(a, tl) if _is_iss_name(tl.name) else None
            if tl.amount is None:
                if tl.base is not None and tl.rate is not None:
                    rate = tl.rate / Decimal(100) if tl.rate > 1 else tl.rate
                    valor = (tl.base * rate).quantize(Decimal("0.01"))
                    out.append(f"• {tl.name}: base {_brl(tl.base)} × {_rate_pct(tl.rate)} "
                               f"= {_brl(valor)} (reconstruído) ✅")
                else:
                    base = (f"base {_brl(tl.base)}" if tl.base is not None
                            else "base não identificada")
                    out.append(f"• {tl.name}: {base} — valor mascarado/ausente no documento (*****)")
            elif tl.estimated:
                if iss_t is not None:
                    rate, base, _valor, city, code = iss_t
                    base_txt = (f"base {_brl(base)}" if base is not None
                                else "base não identificada")
                    out.append(f"• {tl.name}: {base_txt} × {_rate_pct(rate)} "
                               f"(tabela {city}, cód. {code}) = {_brl(tl.amount)} — "
                               f"mascarado na nota, valor calculado pela tabela de alíquotas")
                else:
                    base = (f"base {_brl(tl.base)}" if tl.base is not None
                            else "base não identificada")
                    rate = f" × {_rate_pct(tl.rate)}" if tl.rate is not None else ""
                    out.append(f"• {tl.name}: {base}{rate} = {_brl(tl.amount)} (tabela) — "
                               f"mascarado na nota, valor calculado pela tabela de alíquotas")
            elif iss_t is not None:
                rate, base, valor, city, code = iss_t
                base_txt = f"base {_brl(base)}" if base is not None else "base não identificada"
                mark = " ✅" if valor is not None and (valor, tl.amount) in ok_math else " ❌"
                line = (f"• {tl.name}: {base_txt} × {_rate_pct(rate)} "
                        f"(tabela {city}, cód. {code}) = {_brl(valor)}{mark}")
                if valor is not None and abs(tl.amount - valor) > self._TOL:
                    line += f" — a nota informa {_brl(tl.amount)} (diferença {tl.amount - valor:+.2f})"
                out.append(line)
            elif tl.base is not None and tl.rate is not None:
                rate = tl.rate / Decimal(100) if tl.rate > 1 else tl.rate
                exp = (tl.base * rate).quantize(Decimal("0.01"))
                mark = " ✅" if (exp, tl.amount) in ok_math else " ❌"
                out.append(f"• {tl.name}: base {_brl(tl.base)} × {_rate_pct(tl.rate)} "
                           f"= {_brl(tl.amount)}{mark}")
            else:
                extra = ""
                if a.amount and a.amount > 0:
                    eff = f"{(tl.amount / a.amount) * 100:.2f}".replace(".", ",")
                    extra = f" (efetiva {eff}% do total)"
                out.append(f"• {tl.name}: {_brl(tl.amount)}{extra}")
        return out

    # -- nota específica -------------------------------------------------- #
    # "nota 123", "nf 123", "nf123", "NF-e 123", "nfs e 123", "nota nº 123", "nf número 123",
    # "nota no 123", "nota 1.234" (separador de milhar vira espaço na normalização)…
    # (o texto chega normalizado: sem acentos e sem pontuação)
    _REF_RE = re.compile(
        r"\b(?:nota|nf|nfe|nfs|nfse|fatura|documento|doc)\s*"
        r"(?:(?:e|n|no|num|numero)\s*)?(\d{1,}(?:\s\d{3})*)",
        re.IGNORECASE,
    )
    _LAST_RE = re.compile(r"\b(ultima|ultimo|recem)\b", re.IGNORECASE)
    _DOC_RE = re.compile(r"\b(nota|nf|nfe|nfs|nfse|fatura|documento)\b", re.IGNORECASE)
    # referência a um documento da conversa: preposição + documento ("na
    # nota", "no documento") ou "última/último/recém" + documento ("a última
    # nota"). "nota" genérico sem isso ("vou pagar DIFAL se emitir nota
    # contra ele?") faz parte do caso descrito, não referência a nota da
    # conversa
    _NOTE_REF_RE = re.compile(
        r"\b(?:na|no|nas|nos|da|do|das|dos|pela|pelo)\s+"
        r"(?:ultima\s+|ultimo\s+|recem\s+)?"
        r"(?:nota|nf|nfe|nfs|nfse|fatura|documento)\b"
        r"|\b(?:ultima|ultimo|recem)\s+(?:nota|nf|nfe|nfs|nfse|fatura|documento)\b",
        re.IGNORECASE)
    # pronomes que apontam para a nota já citada ("tem retenção nela?")
    _PRON_RE = re.compile(
        r"\b(nela|nelas|nele|neles|dela|dele|desta|dessa|deste|desse|nesta|nessa)\b",
        re.IGNORECASE,
    )
    # atributo da nota: "há retenção na NF?", "qual o ISS dela?", "o valor da fatura"
    _ATTR_RE = re.compile(
        r"(reten|retido|iss|icms|ipi|pis|cofins|aliquota|imposto|tributo|valor|base|"
        r"calcul|confer|corret|\bst\b|substitu|total|quanto|difal|reducao)",
        re.IGNORECASE,
    )
    # pergunta DEFINICIONAL ("o que é DIFAL?", "me explica o ISS"): responde com
    # a regra (ancorada no conjunto das notas, se houver), não sobre uma nota
    _DEFIN_RE = re.compile(
        r"(\bo que\b|\bo qual\b|\bexplica\b|\bexplicacao\b|\bcomo funciona\b|"
        r"\bpara que serve\b|\bqual a diferenca\b|\bme fala\b|\bme conta\b)",
        re.IGNORECASE,
    )
    # pergunta "como faz" (cálculo/pagamento/declaração): o cliente quer o
    # MÉTODO do tributo, não o dado de uma nota específica
    _HOWTO_RE = re.compile(
        r"\bcomo\s+(?:se\s+|foi\s+|e\s+)?"
        r"(?:calculo|calcula|calculam|calcular|calculad[oa]|"
        r"pago|paga|pagam|pagar)\b"
        r"|\bcomo\s+(?:se\s+)?(?:declar[oa]|declarar|confer[oa]|conferir)\b"
        r"|\bqual\s+(?:e\s+)?a\s+formula\b"
        r"|\bde\s+que\s+(?:forma|maneira)\b",
        re.IGNORECASE,
    )
    # nomes de tributo/conceito fiscal: distinguem pergunta sobre o CONCEITO
    # ("como calculo o IR?", "o que é DIFAL?") de pergunta sobre a NOTA
    # ("como calculo o valor da nota?") — só a primeira sai da nota e vai p/ regra
    _TAX_WORD_RE = re.compile(
        r"\b(iss|issqn|icms|difal|pis|cofins|ipi|ir|irrf|irpj|csll|inss|iptu|irpf|"
        r"aliquota|aliquotas|retencao|retencoes|regime|regimes|imposto|impostos|"
        r"tributo|tributos|reducao)\b",
        re.IGNORECASE,
    )

    def _concept_question(self, t: str) -> bool:
        """True se a pergunta é sobre o CONCEITO de um tributo (definicional ou
        'como faz') — responde pela regra (ancorada nas notas, quando dá),
        nunca sequestrada pelos dados de uma nota da conversa."""
        return bool((self._DEFIN_RE.search(t) or self._HOWTO_RE.search(t))
                    and self._TAX_WORD_RE.search(t))
    # pergunta sobre o CONJUNTO das notas/extrato (sem número específico).
    # Só plural/expressão explícita de conjunto — "a nota" no singular é
    # pergunta sobre UMA nota (a mais recente), não sobre o conjunto.
    _COLL_RE = re.compile(
        r"(\bnotas\b|\bfaturas\b|\bnfes\b|\bnfses\b|\bextratos?\b|\bdocumentos\b|"
        r"\btodas\b|\btodos\b|\bgeral\b|\bresumo\b|\bquantas\b|\bquais\b|"
        r"\blista\b|\blistar\b|\btotal\b|"
        r"\bqual(is)?\s+notas?\b|\bqual(is)?\s+faturas?\b|\bqual(is)?\s+nfes?\b|\bqual(is)?\s+nfses?\b)",
        re.IGNORECASE,
    )
    # referência a documento em qualquer número (singular/plural): distingue
    # pergunta sobre o CONJUNTO das notas ("quais notas têm DIFAL?") de pergunta
    # GERAL que usa "quais" sem citar nota ("Quais as penalidades do SPED?")
    _DOC_ANY_RE = re.compile(
        r"\b(notas?|nfes?|nfses?|nfse|faturas?|documentos?)\b",
        re.IGNORECASE,
    )
    # referência INDETERMINADA a nota ("uma nota minha", "alguma nota",
    # "qualquer nota", "minha nota") sem número: o cliente não disse qual.
    # Com várias notas na conversa, NÃO supor qual é — listar as notas e
    # perguntar o número (responder sobre a nota errada é pior que perguntar)
    _AMBIG_NOTE_RE = re.compile(
        r"\b(?:uma|um|alguma|qualquer)\s+(?:das\s+|de\s+)?(?:notas?|nfs?|nfes?|faturas?|documentos?)"
        r"|\bminha\s+(?:nota|nf|nfe|nfs|nfse|fatura|documento)\b",
        re.IGNORECASE,
    )
    # referência ANAFÓRICA a UMA nota já citada na conversa: "a nota anterior",
    # "a anterior", "a passada", "a de antes", "a nota que mandei antes",
    # "a última nota", "a última". Singular — plural ("as notas anteriores")
    # é pergunta sobre o CONJUNTO e continua no fluxo normal.
    _ANAPHOR_RE = re.compile(
        r"\b(?:"
        r"(?:nota|nf|nfe|nfs|nfse|fatura|documento)\s+"
        r"(?:anterior|anteriormente|passada|antiga|de\s+antes|"
        r"que\s+(?:eu\s+)?(?:falei|mandei|enviei))\b"
        r"|(?:a|o|aquela|aquele|essa|esse|minha|uma)\s+"
        r"(?:nota|nf|nfe|nfs|nfse|fatura|documento)\s+"
        r"(?:anterior|anteriormente|passada|antiga|de\s+antes|"
        r"que\s+(?:eu\s+)?(?:falei|mandei|enviei))\b"
        r"|(?:a|aquela|essa)\s+(?:anterior|passada|antiga|de\s+antes)\b"
        r"|(?:a|aquela|essa|minha)\s+(?:ultima|ultimo|recem(?:mente)?)\b"
        r"|(?:ultima|ultimo|recem(?:mente)?)\s+"
        r"(?:nota|nf|nfe|nfs|nfse|fatura|documento)\b"
        # demonstrativo apontando para a nota em pauta: "essa nota tem DIFAL?",
        # "esta fatura tem ISS?", "aquela NFS tem retenção?"
        r"|(?:essa|esta|aquela|a|o)\s+"
        r"(?:nota|nf|nfe|nfs|nfse|fatura|documento)\b"
        # pronome apontando para a mesma nota: "tem DIFAL nela?", "dela tem
        # ISS?". "ela/ele" só conta quando seguido de verbo ("ela tem ISS?") —
        # sozinho é pessoa ("vou pagar DIFAL se emitir nota contra ele?").
        r"|(?:nela|nelas|nele|neles|dela|dele)\b"
        r"|(?:ela|ele)\s+(?:tem|tinha|possui|vai|foi|e|esta|era)\b"
        r")",
        re.IGNORECASE,
    )
    _ANAPHOR_PRIOR = re.compile(
        r"anterior|passada|antiga|de\s+antes|(?:falei|mandei|enviei)\b",
        re.IGNORECASE,
    )
    # pergunta fiscal GERAL (não sobre uma nota específica): regimes, alíquotas
    # e comparações. Com notas na conversa, essas NÃO caem na branch da última
    # nota — vão para a regra correspondente (o cliente pergunta a regra, não a
    # nota). Ex.: "ISS no Simples ou no Presumido?", "qual a alíquota do ISS?"
    _GENERAL_TAX_RE = re.compile(
        r"(\bregime\b|\bsimples\b|\bpresumido\b|\blucro real\b|\bmei\b|"
        r"\baliquota\b|qual paga mais|pagar mais|paga mais|mais barato|"
        r"qual (?:e|é) melhor|vale a pena|mudar de regime|"
        r"\brescisao\b|\bganho de capital\b|\bimovel\b|\bsped\b|"
        r"\bdirpf\b|\bclt\b|\bautonomo\b|\bfgts\b|\bindenizacao\b)",
        re.IGNORECASE,
    )
    # dúvida genérica anunciada sobre um tópico fiscal ("tenho dúvida de
    # DIFAL", "quero saber sobre ISS", "me ajuda com retenção"): o cliente
    # está dizendo que tem uma dúvida, não perguntando nada específico ->
    # não calcular sobre nota aleatória; pedir a nota ou a pergunta concreta
    _VAGUE_RE = re.compile(
        r"\b(?:"
        r"duvidas?\s+(?:de|sobre|com|no|na|do|da)\s+"
        r"|quero\s+saber\s+(?:mais\s+)?sobre\s+"
        r"|(?:me\s+ajuda(?:r)?|me\s+ajude|pode\s+me\s+ajudar|preciso\s+de\s+ajuda)\s+(?:com|sobre)\s+"
        r")("
        r"difal|icms|iss|issqn|pis|cofins|ipi|irpj|csll|irrf|st|"
        r"retencao|retencoes|aliquota|aliquotas|regime|regimes|reducao|"
        r"imposto|impostos|tributo|tributos|simples|mei|inss|iptu|irpf"
        r")\b",
        re.IGNORECASE,
    )
    _VAGUE_DISPLAY = {
        "difal": "DIFAL", "icms": "ICMS", "iss": "ISS", "issqn": "ISSQN",
        "pis": "PIS", "cofins": "COFINS", "ipi": "IPI", "irpj": "IRPJ",
        "csll": "CSLL", "irrf": "IRRF", "st": "ICMS-ST",
        "retencao": "retenção", "retencoes": "retenções",
        "aliquota": "alíquota", "aliquotas": "alíquotas",
        "regime": "regime tributário", "regimes": "regimes tributários",
        "reducao": "redução de base", "imposto": "imposto",
        "impostos": "impostos", "tributo": "tributo", "tributos": "tributos",
        "simples": "Simples Nacional", "mei": "MEI", "inss": "INSS",
        "iptu": "IPTU", "irpf": "IRPF",
    }
    # movimentos de extrato que mencionam tributos na descrição
    _TAX_TXN_RE = re.compile(
        r"(iss|issqn|icms|ipi|pis|cofins|irrf|\bir\b|csll|difal|darf|gps|\bdas\b|"
        r"tribut|retido|retencao|tributo|imposto)",
        re.IGNORECASE,
    )

    def _referenced_number(self, text: str) -> str | None:
        """Número de nota citado no texto (ou None). Separa milhar: '1 234' -> '1234'."""
        m = self._REF_RE.search(normalize_name(text))
        return m.group(1).replace(" ", "") if m else None

    def _find_audit(self, text: str, audits: list[InvoiceAudit],
                    last_discussed: InvoiceAudit | None = None) -> InvoiceAudit | None:
        """Nota sobre a qual a pergunta é feita: SOMENTE quando o texto cita o
        número real da nota/NFS (ex.: 'nota 123', 'NFS 456'). Sem número, a
        pergunta NÃO é sobre uma nota específica — o bot responde objetivamente
        (regra/conjunto) e não supõe/adivinha qual nota o cliente quer."""
        if not audits:
            return None
        t = normalize_name(text)  # sem acentos: "última" -> "ultima"
        m = self._REF_RE.search(t)
        if not m:
            return None  # sem número citado -> não é sobre uma nota específica
        num = m.group(1).replace(" ", "")  # '1 234' (de 1.234) -> '1234'
        for a in audits:
            if a.number and (a.number == num
                             or (len(num) >= 3 and (num in a.number or a.number in num))):
                return a
        # rede de segurança: o número citado pode estar no nome do arquivo
        # (ex.: 'NFS-e_00001014.pdf') mesmo se a extração do número falhou
        num_c = num.lstrip("0")
        if len(num_c) >= 3:
            for a in audits:
                src = re.sub(r"\D", "", a.source or "")
                if src and src.lstrip("0") == num_c:
                    return a
        return None  # número citado mas não está na conversa (tratado em answer())

    def _anaphoric_audit(self, text: str, audits: list[InvoiceAudit],
                         last_discussed: InvoiceAudit | None = None
                         ) -> InvoiceAudit | None:
        """Nota apontada por referência na conversa, sem número: "a nota
        anterior tem ISS?", "a última tem retenção?", "a de antes?".

        Resolve para UMA nota real: a de trás da que está em pauta
        (``last_discussed``) para "anterior/passada/de antes", e a própria nota
        em pauta (ou a mais recente) para "última/recém". Sem isso a pergunta
        caía no resumo/ancoragem de TODAS as notas da conversa e a resposta
        falava das outras notas.

        Retorna None quando a pergunta não é sobre uma nota específica
        (plural/conjunto, número citado, referência ambígua ou sem notas).
        """
        if not audits:
            return None
        t = normalize_name(text)
        m = self._ANAPHOR_RE.search(t)
        if m is None:
            return None
        # número citado manda; plural/conjunto é pergunta sobre todas. Uma
        # referência como "uma nota anterior" JÁ aponta para uma nota (não é
        # mais "qualquer nota"), então não cai na lista pedindo o número.
        if self._REF_RE.search(t) or self._COLL_RE.search(t):
            return None
        idx = len(audits) - 1
        if last_discussed is not None:
            for i, a in enumerate(audits):
                if a is last_discussed:
                    idx = i
                    break
        if self._ANAPHOR_PRIOR.search(m.group(0)) and idx > 0:
            idx -= 1  # "anterior": a nota de trás da que está em pauta
        return audits[idx]

    def _answer_invoice(self, text: str, audit: InvoiceAudit) -> str:
        t = normalize_name(text)
        head = f"🧾 {audit.nf_type or 'Fatura'} {audit.number}".strip()
        if audit.issuer:
            head += f" — {audit.issuer}"
        if audit.amount is not None:
            head += f" — {_brl(audit.amount)}"

        if "regime" in t:
            body = self._regime_lines(audit)
        elif any(k in t for k in ("reten", "retido")):
            body = self._retention_lines(audit)
        elif "difal" in t:
            body = self._difal_lines(audit)
        elif any(k in t for k in ("reducao", "reduzid")):
            body = self._reducao_lines(audit)
        elif re.search(r"\b(?:ir|irrf)\b", t):
            # "qual o IR da nota 123?": o IR na nota é a retenção na fonte
            body = self._retention_lines(audit)
        elif any(k in t for k in ("aliquota", "por que", "porque")):
            body = self._aliquota_lines(audit, t)
        elif any(k in t for k in ("corret", "confer", "confere", "valid", "confianca")):
            body = self._conferencia_lines(audit)
        elif any(k in t for k in ("imposto", "tributo", "iss", "icms", "pis", "cofins",
                                  "ipi", "calcul", "base")):
            body = self._imposto_lines(audit, t)
        elif any(k in t for k in ("total", "valor", "quanto", "pagar", "preco")):
            body = self._valor_lines(audit)
        else:
            body = list(format_tax_lines(audit))
            if audit.has_retentions:
                body.append(f"✂️ Retenções na fonte: {_brl(audit.total_retentions)}")
            body += self._veredicto_lines(audit)
        if not body:
            body = ["• sem detalhes de impostos extraídos desta nota."]
        return head + "\n" + "\n".join(body)

    def _regime_lines(self, a: InvoiceAudit) -> list[str]:
        names = {(tl.name or "").upper() for tl in a.tax_lines}
        has_iss = any("ISS" in n for n in names)
        has_icms = any("ICMS" in n for n in names)
        lines: list[str] = []
        if has_iss and not has_icms:
            lines.append("• Esta nota é de SERVIÇO (tem ISS): o imposto é o ISSQN, regido pela "
                         "LC 116/2003 (alíquota de 2% a 5% do município).")
        elif has_icms and not has_iss:
            lines.append("• Esta nota é de MERCADORIA/TRANSPORTE (tem ICMS): o imposto é o "
                         "ICMS estadual.")
        elif has_iss and has_icms:
            lines.append("• A nota tem ISS e ICMS (mix de serviço + mercadoria) — cada um com "
                         "sua base e alíquota próprias.")
        else:
            lines.append("• Não identifiquei ISS nem ICMS nos impostos desta nota.")
        if a.service_code:
            desc = f"• Código do serviço (LC 116/2003): {a.service_code}"
            if a.city:
                desc += f" — incidência em {a.city}"
                try:
                    from ..parsing.aliquotas import get_aliquota
                    desc += f" → alíquota da tabela: {get_aliquota(a.city, a.service_code):.2%}"
                except Exception:  # noqa: BLE001
                    pass
            lines.append(desc)
        lines.append(self._regime_da_emissora(a))
        return lines

    _REGIME_MEANING = {
        "Simples Nacional": ("IRPJ, CSLL, PIS, COFINS e ISS/ICMS vão dentro do DAS mensal — "
                             "por isso a nota só destaca o imposto da operação. "
                             "Retenções na fonte ainda se aplicam."),
        "MEI": ("guia mensal única (DAS) de valor fixo; o MEI não sofre retenções na fonte "
                "de IR/CSLL/PIS/COFINS pelo tomador."),
        "Lucro Presumido": ("PIS 0,65% + COFINS 3% cumulativos sobre a receita; IRPJ/CSLL "
                            "sobre o lucro presumido por lei."),
        "Lucro Real": ("PIS 0,65% + COFINS 4% não cumulativos; IRPJ/CSLL sobre o lucro de fato."),
    }

    def _regime_da_emissora(self, a: InvoiceAudit) -> str:
        """Regime da emissora COM o porquê: identificado na nota (campo/rodapé/XML),
        'NÃO OPTANTE' informado, lido das observações ou inferido pelos impostos."""
        regime, evidence = a.regime, a.regime_evidence
        if not regime and not evidence:
            regime, evidence = self._regime_from_notes(a)
        if regime:
            out = [f"• Regime da emissora: {regime.upper()}."]
            if evidence:
                out.append(f"  - Por quê: {evidence}.")
            meaning = self._REGIME_MEANING.get(regime)
            if meaning:
                out.append(f"  - O que isso significa: {meaning}")
            return "\n".join(out)
        if evidence:  # ex.: "NÃO OPTANTE" pelo Simples
            return "\n".join([
                "• Regime da emissora: NÃO é Simples Nacional.",
                f"  - Por quê: {evidence}.",
                "  - O regime exato (Lucro Presumido ou Real) não é especificado na nota; "
                "ele define IRPJ/CSLL/PIS/COFINS da emissora, mas não muda o ISS/ICMS "
                "desta operação.",
            ])
        regime_txt, por_que = self._regime_inferido(a)
        return "\n".join([
            f"• Regime da emissora: {regime_txt}.",
            f"  - Por quê: {por_que}.",
            "  - O regime define IRPJ/CSLL/PIS/COFINS da emissora; não muda o ISS/ICMS "
            "desta operação.",
        ])

    def _regime_from_notes(self, a: InvoiceAudit) -> tuple[str | None, str | None]:
        """Regime citado nas observações da nota (caminho OCR/LLM)."""
        notes = (a.notes or "").lower()
        for kw, nome in (("lucro presumido", "Lucro Presumido"),
                         ("lucro real", "Lucro Real"),
                         ("simples", "Simples Nacional")):
            if kw in notes:
                return nome, "consta nas observações da nota"
        if re.search(r"\bmei\b", notes):
            return "MEI", "consta nas observações da nota"
        return None, None

    def _regime_inferido(self, a: InvoiceAudit) -> tuple[str, str]:
        """Inferência de regime quando a nota não informa: (texto, porquê)."""
        nft = (a.nf_type or "").upper()

        def _hl(name: str) -> bool:
            return any(name in (tl.name or "").upper() and (tl.amount or Decimal(0)) > 0
                       for tl in a.tax_lines)

        if nft in ("NFE", "NFCE"):
            if _hl("PIS") or _hl("COFINS"):
                return ("não consta na nota — provável Lucro Presumido ou Real",
                        "a NFe destaca PIS/COFINS: no Simples Nacional esses tributos vão "
                        "dentro do DAS e não aparecem destacados na nota")
            return ("não consta na nota — pode ser Simples Nacional",
                    "a NFe não destaca PIS/COFINS (no Simples vão dentro do DAS) — "
                    "não é conclusivo")
        if nft == "NFS-E":
            return ("não consta na nota",
                    "NFS-e não destaca PIS/COFINS (tributos federais sobre a receita), "
                    "então o regime não pode ser inferido dos impostos desta nota")
        return ("não consta na nota", "sem dados suficientes na nota para inferir")

    def _aliquota_lines(self, a: InvoiceAudit, t: str) -> list[str]:
        target = None
        for tax in ("ICMS-ST", "ICMS", "ISS", "IPI", "PIS", "COFINS"):
            if tax.lower() in t:
                target = tax
                break
        lines: list[str] = []
        for tl in a.tax_lines:
            n = (tl.name or "").upper()
            if "RET" in n or "IRRF" in n:
                continue  # retenção: coberta por "há retenção?"
            if target and target not in n:
                continue
            if "ISS" not in n:
                if tl.rate is None:
                    continue
                pct = tl.rate / Decimal(100) if tl.rate > 1 else tl.rate
                if "ICMS" in n and "ST" not in n:
                    lines.append(f"• {tl.name}: alíquota na nota {pct:.2%} (referência SP: "
                                 "18% interno; interestadual 4% importados, 7% "
                                 "Sul/Sudeste -> Norte, Nordeste, Centro-Oeste ou ES, "
                                 "12% nos demais casos).")
                elif "ST" in n or "SUBSTITUI" in n:
                    lines.append(f"• {tl.name}: alíquota efetiva {pct:.2%} — definida por "
                                 "legislação/convênio estadual (não há tabela única).")
                elif n.startswith("IPI"):
                    lines.append(f"• IPI: alíquota {pct:.2%} — definida por produto (TIPI), "
                                 "de 0% a 30%+.")
                elif "PIS" in n:
                    lines.append(f"• PIS: {pct:.2%} (referência: 0,65%).")
                elif "COFINS" in n:
                    lines.append(f"• COFINS: {pct:.2%} (referência: 3% cumulativo ou "
                                 "4% não cumulativo).")
                else:
                    lines.append(f"• {tl.name}: alíquota {pct:.2%}.")
                continue
            if a.city and a.service_code:
                try:
                    from ..parsing.aliquotas import get_aliquota
                    rate = get_aliquota(a.city, a.service_code)
                    lines.append(
                        f"• {tl.name}: a LC 116/2003 fixa o ISS entre 2% e 5%, definido pelo "
                        f"município. Para {a.city} + cód. {a.service_code}, a tabela indica "
                        f"{rate:.2%}."
                    )
                except Exception:  # noqa: BLE001
                    lines.append(f"• {tl.name}: a LC 116/2003 fixa o ISS entre 2% e 5%, definido "
                                 "pelo município de incidência.")
            else:
                lines.append(
                    f"• {tl.name}: a LC 116/2003 fixa o ISS entre 2% e 5%; a alíquota exata "
                    "depende do município da incidência (local do tomador) e do código do serviço."
                )
            if tl.rate is not None:
                pct = tl.rate / Decimal(100) if tl.rate > 1 else tl.rate
                lines.append(f"  - Alíquota impressa na nota: {self._pct(pct)}.")
        if not lines:
            if target:
                lines.append(f"• Esta nota não tem {target} destacado.")
                if target == "IPI" and (a.nf_type or "").upper() == "NFS-E":
                    lines.append("• Serviços não pagam IPI (imposto de produtos industrializados).")
            else:
                lines.append(
                    "• Sem alíquotas identificadas nos impostos desta nota. Referências: ICMS (SP) "
                    "18% interno; ISS 2%–5% (LC 116); PIS 0,65%; COFINS 3%/4%; IPI por produto (TIPI)."
                )
        return lines

    # -- retenções: alíquotas por caso (IN RFB 1.234/2012 + Lei 10.684/2003) -- #
    @staticmethod
    def _issuer_simples(a: InvoiceAudit) -> bool | None:
        """True/False se o regime do emissor é conhecido; None se indeterminado."""
        regime = (a.regime or "").lower()
        if "simples" in regime or "mei" in regime:
            return True
        if "lucro presumido" in regime or "lucro real" in regime:
            return False
        notes = (a.notes or "").lower()
        if "simples" in notes:
            return True
        if "lucro presumido" in notes or "lucro real" in notes:
            return False
        return None

    @staticmethod
    def _tomador_pj(a: InvoiceAudit) -> bool | None:
        """True se tomador é PJ (CNPJ 14 dígitos), False se PF (CPF 11), None se indeterminado."""
        digits = "".join(ch for ch in (a.customer_doc or "") if ch.isdigit())
        if len(digits) == 14:
            return True
        if len(digits) == 11:
            return False
        return None

    def _retention_rates(self, a: InvoiceAudit) -> list[tuple[str, list[Decimal], str]]:
        """[(imposto, [alíquotas possíveis], justificativa)] para o caso desta nota.

        Alíquotas de RETENÇÃO na fonte (não de apuração):
        IN RFB 1.234/2012, art. 3º §§3º–5º e Lei 10.833/2003, art. 31 —
        CSLL 1%, COFINS 3% e PIS 0,65% valem inclusive quando a prestadora
        é não cumulativa (1,65%/7,6% são alíquotas de apuração, não de retenção).
        """
        code = (a.service_code or "").strip()
        juridico = code.startswith(("06.", "06-"))
        simples = self._issuer_simples(a)

        def pct(v: str) -> Decimal:
            return Decimal(v) / Decimal(100)

        if juridico:
            ir = ([pct("1.5")], "1,5% — serviço profissional/jurídico (item 06 da LC 116)")
        elif code:
            ir = ([pct("4.8")], f"4,8% — demais serviços (IN RFB 1.234, Anexo I; cód. {code})")
        else:
            ir = ([pct("1.5"), pct("4.8")],
                  "1,5% (profissional/jurídico) ou 4,8% (demais serviços — IN RFB 1.234)")
        csll = ([pct("1")], "1% — Lei 10.833/2003, art. 31")
        if simples is True:
            iss = ([pct("2")], "2% — prestador no Simples Nacional")
        elif simples is False:
            iss = ([pct("3")], "3% — prestador fora do Simples Nacional")
        else:
            iss = ([pct("2"), pct("3")], "2% (Simples Nacional) ou 3% (demais regimes)")
        return [
            ("IR", *ir),
            ("CSLL", *csll),
            ("PIS", [pct("0.65")], "0,65% — Lei 10.833/2003, art. 31 (mesmo p/ não cumulativa)"),
            ("COFINS", [pct("3")], "3% — Lei 10.833/2003, art. 31 (mesmo p/ não cumulativa)"),
            ("ISS", *iss),
        ]

    def _retention_lines(self, a: InvoiceAudit) -> list[str]:
        nft = (a.nf_type or "").upper()
        base = a.service_value
        if base is None:
            for tl in a.tax_lines:
                if "ISS" in (tl.name or "").upper() and tl.base is not None:
                    base = tl.base
                    break
        if base is None:
            base = a.subtotal

        def shown(name: str) -> TaxLine | None:
            for tl in a.retention_lines:
                n2 = (tl.name or "").upper()
                if (name == "IR" and n2.startswith("IR")) or n2.startswith(name):
                    return tl
            return None

        if not a.has_retentions and nft != "NFS-E":
            return [
                "• Não identifiquei retenção na fonte nesta nota.",
                "• Retenções (IR, CSLL, PIS, COFINS, ISS) incidem sobre serviços (NFS-e); "
                "em NFe de mercadoria são raras.",
            ]

        if a.has_retentions:
            base_txt = f" — base de cálculo {_brl(base)}" if base is not None else ""
            lines = [f"Retenções desta nota{base_txt}:"]
            for name, rates, just in self._retention_rates(a):
                tl = shown(name)
                if tl is not None and tl.amount is not None:
                    eff = (tl.amount / base) if base is not None else None
                    eff_txt = f" ({self._pct(eff)})" if eff is not None else ""
                    exps = [base * r for r in rates] if base is not None else []
                    ok = any(abs(tl.amount - e) <= Decimal("0.05") for e in exps)
                    mark = "✅" if ok else "⚠️"
                    line = f"• {name}: {_brl(tl.amount)} na nota{eff_txt} — {just} {mark}"
                    if not ok and exps:
                        exp_txt = " ou ".join(_brl(e) for e in exps)
                        line += f" (esperado {exp_txt})"
                    lines.append(line)
                elif base is not None:
                    exp_txt = " ou ".join(f"{_brl(base * r)} ({self._pct(r)})" for r in rates)
                    lines.append(f"• {name}: sem valor destacado — esperado {exp_txt}; {just}")
                else:
                    lines.append(f"• {name}: sem valor destacado — {just}")
            total = a.total_retentions
            if total is not None:
                if a.service_value is not None and a.net_value is not None:
                    lines.append(f"• Total retido {_brl(total)} → líquido {_brl(a.net_value)} "
                                 f"(serviço {_brl(a.service_value)} − {_brl(total)}).")
                else:
                    lines.append(f"• Total retido: {_brl(total)}.")
        else:
            lines = ["• Não há retenção destacada nesta nota."]
            simples = self._issuer_simples(a)
            tomador_pj = self._tomador_pj(a)
            if simples is True:
                lines.append(
                    "• Prestador no Simples Nacional: dispensada a retenção federal de "
                    "IR/CSLL/PIS/COFINS (IN RFB 1.234/2012, art. 4º, XI) — não há valores "
                    "hipotéticos a listar."
                )
            elif simples is None:
                lines.append(
                    "• Retenção hipotética só cabe quando o prestador está FORA do Simples "
                    "e o tomador é PJ — não identifiquei o regime desta nota, então não vou "
                    "estimar valores."
                )
            elif tomador_pj is not True:
                lines.append(
                    "• Retenção na fonte de IR/CSLL/PIS/COFINS só se aplica quando o tomador "
                    "é pessoa jurídica — não identifiquei CNPJ do tomador nesta nota, então "
                    "não vou estimar valores."
                )
            elif base is not None:
                lines.append(f"Se o tomador reter, os valores esperados (base {_brl(base)}) seriam:")
                for name, rates, just in self._retention_rates(a):
                    exp_txt = " ou ".join(f"{_brl(base * r)} ({self._pct(r)})" for r in rates)
                    lines.append(f"  - {name}: {exp_txt} — {just}")
            else:
                lines.append("• Sem o valor do serviço não consigo calcular os valores esperados.")
        lines.append(
            "Base legal: IN RFB 1.234/2012 e Lei 10.833/2003, arts. 30–31 "
            "(IR/CSLL/PIS/COFINS) e Lei 10.684/2003 (ISS). "
            "Quem retém entrega ao fisco; o valor retido não reduz a base do imposto."
        )
        return lines

    # -- DIFAL (diferencial de alíquota de ICMS) --------------------------- #
    def _difal_lines(self, a: InvoiceAudit) -> list[str]:
        difal = [tl for tl in a.tax_lines if "DIFAL" in (tl.name or "").upper()]
        if difal:
            lines = ["• " + self._tax_line_detail(tl) for tl in difal]
            lines.append(
                "O DIFAL é o diferencial de alíquota do ICMS em operação interestadual "
                "(EC 87/2015): a origem cobra a alíquota interestadual (4% para bens "
                "importados; 7% da origem no Sul/Sudeste com destino no Norte, "
                "Nordeste, Centro-Oeste ou ES; 12% nos demais casos) e o DIFAL é a "
                "diferença até a alíquota interna do destino — há DIFAL no "
                "uso/consumo (ou ativo imobilizado) de contribuinte e na venda a "
                "pessoa física/não contribuinte; na revenda não há."
            )
            return lines
        nft = (a.nf_type or "").upper()
        has_icms = any("ICMS" in (tl.name or "").upper() for tl in a.tax_lines)
        lines = ["• Esta nota não tem DIFAL destacado."]
        if nft == "NFS-E":
            lines.append("• É uma nota de serviço (NFS-e): não incide ICMS/DIFAL — o imposto é o ISS.")
        elif has_icms:
            lines.append("• O ICMS aparece com alíquota interna: DIFAL só entra quando o "
                         "destinatário está em outro estado (operação interestadual).")
        else:
            lines.append("• Não identifiquei ICMS (nem DIFAL) nesta nota.")
        lines.append(
            "Lembrete: em operação interestadual há DIFAL no uso/consumo (ou ativo "
            "imobilizado) de contribuinte — recolhido pelo destinatário — e na venda "
            "a pessoa física/não contribuinte — recolhido pelo remetente; em revenda "
            "não há DIFAL."
        )
        return lines

    # -- redução de base de cálculo ---------------------------------------- #
    def _reducao_lines(self, a: InvoiceAudit) -> list[str]:
        icms = [tl for tl in a.tax_lines
                if "ICMS" in (tl.name or "").upper() and "ST" not in (tl.name or "").upper()]
        if not icms:
            nft = (a.nf_type or "").upper()
            if nft == "NFS-E":
                return ["• Esta nota é de serviço (NFS-e): não tem ICMS — redução de base é "
                        "um mecanismo do ICMS sobre mercadorias."]
            return ["• Não identifiquei ICMS nesta nota (redução de base é um mecanismo do ICMS)."]
        ref = a.subtotal
        lines = []
        for tl in icms:
            if tl.base is None:
                lines.append(f"• {tl.name}: base não identificada na nota — não consigo aferir redução.")
                continue
            if ref is not None and tl.base < ref:
                diff = ref - tl.base
                pct = (diff / ref) if ref else None
                pct_s = f" ({self._pct(pct)} da base cheia)" if pct is not None else ""
                lines.append(
                    f"• {tl.name}: base {_brl(tl.base)} é menor que o valor da operação "
                    f"{_brl(ref)} → redução de base de {_brl(diff)}{pct_s}."
                )
                lines.append(
                    "  - Redução de base é benefício concedido pelo estado (lei/convênio): a "
                    "alíquota é a mesma, mas incide sobre base menor. Não altera o total da nota."
                )
            else:
                lines.append(f"• {tl.name}: base {_brl(tl.base)} = valor da operação — "
                             "sem redução de base.")
        return lines

    # -- nota citada mas ausente da conversa -------------------------------- #
    def _missing_invoice(self, num: str, audits: list[InvoiceAudit]) -> str:
        lines = [f"🔎 Não tenho a nota {num} nesta conversa."]
        have = [a.number for a in audits if a.number]
        if have:
            lines.append(f"Notas que eu tenho: {', '.join(have)}.")
        lines.append(
            "Se enviar a nota (foto ou arquivo), eu verifico os impostos, retenções, "
            "DIFAL, redução de base e o cálculo."
        )
        return "\n".join(lines)

    # -- "uma nota" sem dizer qual (referência ambígua) ---------------------- #
    def _which_note_reply(self, audits: list[InvoiceAudit]) -> str:
        """'consegue ver os impostos de uma nota minha?' com várias notas na
        conversa: o cliente não disse qual nota — não supor. Mostro o resumo
        dos impostos de CADA uma e peço o número da que ele quer em detalhe."""
        lines = [f"Claro! 😊 Tenho {len(audits)} notas nesta conversa — "
                 "segue o resumo dos impostos de cada uma:"]
        for a in audits[:10]:
            head = f"• {a.nf_type or 'Fatura'} {a.number} ({a.issuer})"
            if a.amount is not None:
                head += f" — {_brl(a.amount)}"
            taxes = "; ".join(f"{tl.name} {_brl(tl.amount)}"
                              for tl in a.tax_lines if tl.amount is not None)
            if taxes:
                head += f" [{taxes}]"
            if a.has_retentions:
                head += f" ✂️ retido {_brl(a.total_retentions or Decimal('0'))}"
            lines.append(head)
        if len(audits) > 10:
            lines.append(f"… e mais {len(audits) - 10} nota(s).")
        lines.append("Me diz o número da nota que você quer ver em detalhe "
                     "(ou diga 'a última').")
        return "\n".join(lines)

    # -- conjunto das notas / extrato --------------------------------------- #
    @staticmethod
    def _np(n: int, singular: str, plural: str) -> str:
        return singular if n == 1 else plural

    def _answer_collection(self, t: str, audits: list[InvoiceAudit]) -> str:
        if any(k in t for k in ("reten", "retido")):
            return self._collection_retentions(audits)
        if "difal" in t:
            return self._collection_difal(audits)
        if any(k in t for k in ("diverg", "erro", "confer", "confianca")):
            return self._collection_problems(audits)
        if "aliquota" in t:
            return self._collection_aliquotas(audits)
        if "regime" in t:
            return self._collection_regime(audits)
        return self._collection_summary(audits)

    def _collection_retentions(self, audits: list[InvoiceAudit]) -> str:
        with_ret = [a for a in audits if a.has_retentions]
        n = len(audits)
        lines = [f"✂️ Retenções {self._np(n, 'na', 'nas')} "
                 f"{self._np(n, 'nota', 'notas')} desta conversa ({n}):"]
        if not with_ret:
            if len(audits) == 1:
                # com uma única nota na conversa, já mostro os valores esperados
                # dela (a dúvida é tirada sobre o caso do cliente)
                lines += self._retention_lines(audits[0])
                return "\n".join(lines)
            lines.append("• Nenhuma nota tem retenção na fonte destacada.")
            lines.append("Se o tomador reter, os valores esperados saem por nota — "
                         "pergunte sobre o número da NF.")
            return "\n".join(lines)
        total = Decimal("0")
        for a in with_ret:
            tr = a.total_retentions or Decimal("0")
            total += tr
            detail = ", ".join(f"{tl.name} {_brl(tl.amount)}"
                               for tl in a.retention_lines if tl.amount is not None)
            line = f"• {a.nf_type or 'Fatura'} {a.number} ({a.issuer}): {_brl(tr)}"
            if detail:
                line += f" [{detail}]"
            lines.append(line)
        lines.append(f"• Total retido: {_brl(total)}.")
        lines.append("Quem retém é o tomador — ele entrega ao fisco (IN RFB 1.234/2012).")
        return "\n".join(lines)

    def _collection_difal(self, audits: list[InvoiceAudit]) -> str:
        """DIFAL em CADA nota da conversa: as que têm (com valor/base/alíquota) e as
        que não têm, com o motivo (serviço, operação interna, sem ICMS)."""
        n = len(audits)
        lines = [f"🧮 DIFAL {self._np(n, 'na', 'nas')} "
                 f"{self._np(n, 'nota', 'notas')} desta conversa ({n}):"]
        shown = 0
        for a in audits:
            if shown >= 10:
                lines.append(f"… e mais {n - 10} nota(s).")
                break
            shown += 1
            difal = [tl for tl in a.tax_lines if "DIFAL" in (tl.name or "").upper()]
            head = f"• {a.nf_type or 'Fatura'} {a.number} ({a.issuer})"
            if difal:
                lines.append(f"{head}: " + "; ".join(self._tax_line_detail(tl) for tl in difal))
            elif (a.nf_type or "").upper() == "NFS-E":
                lines.append(f"{head}: sem DIFAL — é serviço (NFS-e): incide ISS, não ICMS.")
            elif any("ICMS" in (tl.name or "").upper() for tl in a.tax_lines):
                lines.append(f"{head}: sem DIFAL destacado — ICMS pela alíquota interna "
                             "(operação interna).")
            else:
                lines.append(f"{head}: sem DIFAL (sem ICMS destacado na nota).")
        return "\n".join(lines)

    def _collection_problems(self, audits: list[InvoiceAudit]) -> str:
        bad = [a for a in audits if a.problems]
        if not bad:
            return (f"✅ Todas as {len(audits)} notas desta conversa passaram na conferência "
                    "do cálculo dos impostos.")
        lines = [f"⚠️ {len(bad)} de {len(audits)} notas com divergência no cálculo:"]
        for a in bad:
            lines.append(f"• {a.nf_type or 'Fatura'} {a.number} ({a.issuer}):")
            for c in a.problems:
                lines.append(f"    {_SEV_EMOJI[c.severity.value]} {c.message}")
        return "\n".join(lines)

    def _collection_aliquotas(self, audits: list[InvoiceAudit]) -> str:
        n = len(audits)
        lines = [f"📊 Alíquotas {self._np(n, 'da', 'das')} "
                 f"{self._np(n, 'nota', 'notas')} desta conversa ({n}):"]
        found = False
        for a in audits:
            for tl in a.tax_lines:
                nm = (tl.name or "").upper()
                if tl.rate is None or any(p in nm for p in ("RET", "IRRF")):
                    continue
                found = True
                lines.append(f"• {a.nf_type or 'Fatura'} {a.number} — {tl.name}: "
                             f"{_rate_pct(tl.rate)}")
        if not found:
            lines.append("• Sem alíquotas identificadas nas notas desta conversa.")
        return "\n".join(lines)

    def _collection_regime(self, audits: list[InvoiceAudit]) -> str:
        n = len(audits)
        lines = [f"🧭 Regime {self._np(n, 'da', 'das')} "
                 f"{self._np(n, 'nota', 'notas')} desta conversa ({n}):"]
        for a in audits:
            notes = (a.notes or "").lower()
            if "simples" in notes:
                reg = "Simples Nacional (consta na nota)"
            elif "lucro presumido" in notes:
                reg = "Lucro Presumido (consta na nota)"
            elif "lucro real" in notes:
                reg = "Lucro Real (consta na nota)"
            elif "mei" in notes:
                reg = "MEI (consta na nota)"
            else:
                reg = "não consta na nota"
            lines.append(f"• {a.nf_type or 'Fatura'} {a.number}: {reg}")
        lines.append("O regime define IRPJ/CSLL/PIS/COFINS da emissora; não muda o ISS/ICMS "
                     "da operação.")
        return "\n".join(lines)

    # -- impostos por linha nas notas da conversa --------------------------- #
    def _collection_tax(self, audits: list[InvoiceAudit], pats: tuple[str, ...],
                        label: str, emoji: str = "🧾") -> str:
        """Linhas de imposto (nome contém algum padrão de ``pats``, sem retenções)
        em cada nota da conversa — p/ ancorar perguntas genéricas nas notas reais."""
        n = len(audits)
        lines = [f"{emoji} {label} {self._np(n, 'na', 'nas')} "
                 f"{self._np(n, 'nota', 'notas')} desta conversa ({n}):"]
        found = shown = 0
        for a in audits:
            hits = [tl for tl in a.tax_lines
                    if any(p in (tl.name or "").upper() for p in pats)
                    and not any(p in (tl.name or "").upper() for p in ("RET", "IRRF"))]
            if not hits:
                continue
            found += 1
            if shown >= 10:
                continue
            shown += 1
            lines.append(f"• {a.nf_type or 'Fatura'} {a.number} ({a.issuer}): "
                         + "; ".join(self._tax_line_detail(tl) for tl in hits))
        if found > shown:
            lines.append(f"… e mais {found - shown} nota(s) com {label}.")
        if not found:
            lines.append(f"• Nenhuma nota desta conversa tem {label} destacado.")
        return "\n".join(lines)

    def _collection_icms(self, audits: list[InvoiceAudit]) -> str:
        return self._collection_tax(audits, ("ICMS",), "ICMS")

    def _collection_iss(self, audits: list[InvoiceAudit]) -> str:
        return self._collection_tax(audits, ("ISS",), "ISS")

    def _collection_pis_cofins(self, audits: list[InvoiceAudit]) -> str:
        return self._collection_tax(audits, ("PIS", "COFINS"), "PIS/COFINS")

    def _collection_ipi(self, audits: list[InvoiceAudit]) -> str:
        return self._collection_tax(audits, ("IPI",), "IPI")

    def _collection_reducao(self, audits: list[InvoiceAudit]) -> str:
        """Redução de base de ICMS nas notas: base do ICMS menor que o valor da
        operação (mesma lógica da auditoria, agregada por nota)."""
        n = len(audits)
        lines = [f"📉 Redução de base {self._np(n, 'na', 'nas')} "
                 f"{self._np(n, 'nota', 'notas')} desta conversa ({n}):"]
        found = 0
        for a in audits:
            icms = [tl for tl in a.tax_lines
                    if "ICMS" in (tl.name or "").upper() and "ST" not in (tl.name or "").upper()]
            for tl in icms:
                if tl.base is None or a.subtotal is None or tl.base >= a.subtotal:
                    continue
                found += 1
                diff = a.subtotal - tl.base
                pct = diff / a.subtotal if a.subtotal else None
                pct_s = f" ({self._pct(pct)} da base cheia)" if pct is not None else ""
                lines.append(
                    f"• {a.nf_type or 'Fatura'} {a.number} ({a.issuer}): base {_brl(tl.base)} "
                    f"menor que o valor da operação {_brl(a.subtotal)} → redução "
                    f"{_brl(diff)}{pct_s}."
                )
        if not found:
            lines.append("• Nenhuma nota desta conversa tem base de ICMS menor que o valor "
                         "da operação (sem redução de base identificada).")
        return "\n".join(lines)

    def _collection_summary(self, audits: list[InvoiceAudit]) -> str:
        n = len(audits)
        lines = [f"🧾 Resumo fiscal {self._np(n, 'da', 'das')} "
                 f"{self._np(n, 'nota', 'notas')} desta conversa ({n}):"]
        total_ret = Decimal("0")
        for a in audits:
            head = f"• {a.nf_type or 'Fatura'} {a.number} ({a.issuer})"
            if a.amount is not None:
                head += f" — {_brl(a.amount)}"
            taxes = "; ".join(f"{tl.name} {_brl(tl.amount)}"
                              for tl in a.tax_lines if tl.amount is not None)
            if taxes:
                head += f" [{taxes}]"
            if a.has_retentions:
                tr = a.total_retentions or Decimal("0")
                total_ret += tr
                head += f" ✂️ retido {_brl(tr)}"
            if a.problems:
                head += f" ⚠️ {len(a.problems)} divergência(s)"
            lines.append(head)
        if total_ret > 0:
            lines.append(f"Total retido na fonte: {_brl(total_ret)}.")
        return "\n".join(lines)

    # -- extrato bancário: movimentos que mencionam tributos ---------------- #
    def _answer_extrato(self, transactions: list) -> str:
        hits = [tx for tx in transactions
                if tx.description and self._TAX_TXN_RE.search(tx.description)]
        lines = [f"🏦 Movimentos do extrato com menção a tributos "
                 f"({len(hits)} de {len(transactions)}):"]
        if not hits:
            lines.append("• Nenhum movimento menciona tributos na descrição "
                         "(ISS, ICMS, PIS, COFINS, IR, retenções, DARF, GPS, DAS…).")
        else:
            for tx in hits[:15]:
                lines.append(f"• {tx.date} — {_brl(tx.amount)} — {tx.description}")
            if len(hits) > 15:
                lines.append(f"• … e mais {len(hits) - 15} movimento(s).")
        lines.append("Dica: o detalhamento dos impostos vem das notas — pergunte sobre o "
                     "número da NF (ex.: 'tem retenção na nota 123?').")
        return "\n".join(lines)

    def _imposto_lines(self, a: InvoiceAudit, t: str) -> list[str]:
        """Impostos da nota, filtrando pelo imposto perguntado (ex.: 'qual o ISS...').

        Para ISS, o valor é calculado DIRETAMENTE pela tabela de alíquotas
        (cidade + cód. LC 116/2003) — o valor impresso na nota só é citado
        quando difere do cálculo da tabela.
        """
        target = None
        for tax in ("ICMS-ST", "ICMS", "ISS", "IPI", "PIS", "COFINS", "CSLL"):
            if tax.lower() in t:
                target = tax
                break
        lines: list[str] = []
        for tl in a.tax_lines:
            n = (tl.name or "").upper()
            if target and target not in n:
                continue
            iss_t = self._iss_tabela(a, tl) if _is_iss_name(n) else None
            if iss_t is not None:
                rate, base, valor, city, code = iss_t
                base_txt = f"base {_brl(base)}" if base is not None else "base não identificada"
                line = (f"• {tl.name}: {base_txt} × {_rate_pct(rate)} "
                        f"(tabela {city}, cód. {code}) = {_brl(valor)}")
                if tl.amount is not None and valor is not None \
                        and abs(tl.amount - valor) > self._TOL:
                    line += f" — a nota informa {_brl(tl.amount)} (diferença {tl.amount - valor:+.2f})"
                lines.append(line)
                continue
            if tl.amount is not None:
                detail = f"• {tl.name}: {_brl(tl.amount)}"
            else:
                detail = f"• {tl.name}: valor não identificado na nota"
            parts = []
            if tl.base is not None:
                parts.append(f"base {_brl(tl.base)}")
            if tl.rate is not None:
                parts.append(f"alíquota {_rate_pct(tl.rate)}")
            if parts:
                detail += f" ({', '.join(parts)})"
            if tl.estimated:
                detail += " — valor calculado (mascarado/ausente na nota)"
            lines.append(detail)
        if not lines:
            if target:
                lines.append(f"• Esta nota não tem {target} destacado.")
            else:
                lines.append("• Sem detalhes de impostos extraídos desta nota.")
        lines += self._problema_lines(a)
        return lines

    def _valor_lines(self, a: InvoiceAudit) -> list[str]:
        """Valor/total da nota e a composição (serviço, impostos, retenções, líquido)."""
        nft = (a.nf_type or "").upper()
        lines: list[str] = []
        if a.amount is not None:
            lines.append(f"• Total da nota: {_brl(a.amount)}.")
        parts: list[str] = []
        if nft == "NFS-E" and a.service_value is not None:
            parts.append(f"serviço {_brl(a.service_value)}")
        elif a.subtotal is not None:
            parts.append(f"produtos {_brl(a.subtotal)}")
        for tl in a.tax_lines:
            if tl.amount is not None and not any(p in (tl.name or "").upper()
                                                 for p in ("RET", "IRRF")):
                parts.append(f"{tl.name} {_brl(tl.amount)}")
        if a.has_retentions:
            parts.append(f"retenções {_brl(a.total_retentions or Decimal('0'))}")
        if parts:
            lines.append("• Composição: " + " | ".join(parts) + ".")
        if nft == "NFS-E" and a.has_retentions and a.service_value is not None:
            tr = a.total_retentions or Decimal("0")
            liq = a.net_value if a.net_value is not None else _q(a.service_value - tr)
            lines.append(f"• Líquido a pagar: {_brl(liq)} (serviço {_brl(a.service_value)} "
                         f"− retenções {_brl(tr)}).")
        if not lines:
            lines.append("• Sem valores extraídos desta nota.")
        return lines

    def _conferencia_lines(self, a: InvoiceAudit) -> list[str]:
        probs = a.problems
        if not probs:
            return ["✅ Cálculo conferido: base × alíquota e total batem com o que está na nota."]
        out = [f"⚠️ {len(probs)} divergência(s) encontrada(s):"]
        for c in probs:
            out.append(f"{_SEV_EMOJI[c.severity.value]} {c.message}")
        return out

    def _veredicto_lines(self, a: InvoiceAudit) -> list[str]:
        if a.ok:
            return ["✅ Parecer: cálculo de impostos conferido, sem divergências."]
        out = [f"⚠️ Parecer: {len(a.problems)} divergência(s):"]
        for c in a.problems:
            out.append(f"{_SEV_EMOJI[c.severity.value]} {c.message}")
        return out

    def _problema_lines(self, a: InvoiceAudit) -> list[str]:
        probs = a.problems
        if not probs:
            return ["✅ Sem divergências no cálculo dos impostos."]
        out = [f"⚠️ {len(probs)} divergência(s):"]
        for c in probs:
            out.append(f"{_SEV_EMOJI[c.severity.value]} {c.message}")
        return out

    # -- LLM (perguntas livres) ------------------------------------------- #
    def _llm_answer(self, text: str, audits: list[InvoiceAudit]) -> str:
        if self.llm is None or not getattr(self.llm, "available", False):
            return ""
        notes = self._notes_digest(audits)
        system = (
            "Você é contador brasileiro, especialista em tributos (ISS, ICMS, DIFAL, "
            "ICMS-ST, PIS, COFINS, IPI, IRPJ, CSLL, INSS, IRPF), regimes (MEI, Simples "
            "Nacional, Lucro Presumido, Lucro Real) e temas de pessoa física (rescisão, "
            "ganho de capital, DIRPF). Responda a pergunta do cliente de forma "
            "DIRETA e objetiva (máx. 5 linhas, pt-BR). Não fuja da pergunta; se a "
            "resposta depender de dados que você não tem, diga o que depende e o que "
            "falta. Alíquotas de referência: ICMS SP 18% interno; interestadual 4% "
            "(bens importados), 7% (origem no Sul/Sudeste — PR, SC, RS, SP, RJ, MG — "
            "com destino no Norte, Nordeste, Centro-Oeste ou ES) e 12% (demais casos); "
            "DIFAL = alíquota interna do destino − interestadual (em pontos "
            "percentuais): há DIFAL no uso/consumo (ou ativo imobilizado) de "
            "contribuinte e na venda a pessoa física/não contribuinte; em revenda "
            "não há DIFAL. Sempre termine com: \"Confirme a alíquota interna e as "
            "regras do estado de destino com seu contador.\" "
            "ISS 2-5% (município, não muda com o regime); PIS 0,65%; COFINS 3%/4%; "
            "IPI 0-30% (TIPI); IRPJ 15%+10%; CSLL 9%; retenções IR 1,5/3%, CSLL 1/3%, "
            "PIS 1,65%, COFINS 7,6%, ISS 2/3%. Dados de referência: IRPF mensal "
            "(2024) isento até R$ 2.428,80, alíquotas 7,5% a 27,5%; MEI limite "
            "R$ 81 mil/ano (excesso até 10% = carência até 31/12; acima = vira ME no "
            "Simples no mês seguinte); Simples limite R$ 4,8 mi/ano (mesma regra de "
            "carência); MEI+CLT ao mesmo tempo é permitido; rescisão: parte "
            "indenizatória isenta (Lei 7.713/88 art. 6 V), parte salarial tributada "
            "na fonte com restituição na DIRPF; ganho de capital 15% (imóvel isento "
            "até R$ 440 mil/ano; ações isentas até R$ 20 mil/mês; FII 20%). "
            "Prazos de SPED e EFD (pagamento/entrega) e suas penalidades estão "
            "FORA DO ESCOPO deste bot: se o cliente perguntar sobre isso, diga "
            "simplesmente que está fora do escopo e volte ao que está dentro "
            "(ISS, ICMS, DIFAL, PIS, COFINS, IPI, IRPJ, CSLL, INSS, IRPF, regimes, "
            "retenções e notas).\n\n"
            f"NOTAS DESTA CONVERSA:\n{notes}\n\n"
            'Responda SOMENTE JSON válido: {"reply": "<resposta>"}'
        )
        try:
            data = self.llm.chat_json(system, [{"role": "user", "content": text}])
            return str(data.get("reply", "")).strip()
        except Exception:  # noqa: BLE001
            return ""

    @staticmethod
    def _notes_digest(audits: list[InvoiceAudit]) -> str:
        if not audits:
            return "(nenhuma nota nesta conversa)"
        out = []
        for a in audits:
            line = f"- {a.nf_type or 'Fatura'} {a.number} ({a.issuer}) total={a.amount}"
            if a.tax_lines:
                line += " | " + "; ".join(
                    f"{tl.name} base={tl.base} alq={tl.rate} valor={tl.amount}"
                    for tl in a.tax_lines
                )
            if a.city:
                line += f" | cidade={a.city} cod_servico={a.service_code}"
            out.append(line)
        return "\n".join(out)


__all__ = ["TaxAdvisor"]
