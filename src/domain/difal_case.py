"""Análise determinística de CASOS de DIFAL descritos pelo cliente.

Responde perguntas sobre o caso do cliente DIRETAMENTE — ex.:
"tenho uma empresa de software em BH e vou prestar serviço para uma empresa
de POA, vou pagar DIFAL?" — a partir de:

  * origem/destino (estados, por cidade ou UF citada no texto);
  * natureza da operação (serviço LC 116/2003 x mercadoria);
  * destinatário (1) revenda — contribuinte, sem DIFAL; (2) uso/consumo ou
    ativo imobilizado — contribuinte, com DIFAL recolhido pelo destinatário;
    (3) pessoa física/não contribuinte — com DIFAL recolhido pelo remetente.

Determinístico (funciona sem LLM):
  * serviço (LC 116/2003) -> NÃO incide DIFAL; ISS no município do tomador;
  * operação interna (mesmo estado) -> NÃO incide DIFAL;
  * mercadoria interestadual -> DIFAL = alíquota interna do destino −
    interestadual (ver ``icms_rates.aliquota_interestadual``: 4% importados,
    7% Sul/Sudeste -> Norte/Nordeste/Centro-Oeste/ES, 12% os demais casos);
    venda a consumidor final não contribuinte -> destino cobra por
    antecipação.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from .icms_rates import INTERNAL_RATES, aliquota_interestadual
from .rules import normalize_name, strip_accents


# --------------------------------------------------------------------------- #
# Geografia: cidade/UF -> UF (nomes normalizados: minúsculas, sem acento)
# --------------------------------------------------------------------------- #
CITY_UF: dict[str, str] = {
    # abreviações de uso corrente
    "bh": "MG", "poa": "RS", "rio": "RJ",
    # MG
    "belo horizonte": "MG", "contagem": "MG", "uberlandia": "MG",
    "juiz de fora": "MG", "montes claros": "MG", "uba": "MG",
    "ipatinga": "MG", "uberaba": "MG",
    # RS
    "porto alegre": "RS", "caxias do sul": "RS", "pelotas": "RS",
    "canoas": "RS", "santa maria": "RS", "novo hamburgurgo": "RS",
    "sao leopoldo": "RS",
    # SP
    "sao paulo": "SP", "campinas": "SP", "sao bernardo do campo": "SP",
    "guarulhos": "SP", "osasco": "SP", "sorocaba": "SP",
    # RJ
    "rio de janeiro": "RJ", "niteroi": "RJ", "duque de caxias": "RJ",
    # PR
    "curitiba": "PR", "londrina": "PR", "maringa": "PR",
    # SC
    "florianopolis": "SC", "joinville": "SC", "blumenau": "SC",
    # BA
    "salvador": "BA", "feira de santana": "BA",
    # PE
    "recife": "PE", "olinda": "PE",
    # CE
    "fortaleza": "CE",
    # GO
    "goiania": "GO",
    # DF
    "brasilia": "DF",
    # ES
    "vitoria": "ES",
    # MS
    "campo grande": "MS",
    # MT
    "cuiaba": "MT",
    # MA
    "sao luis": "MA",
    # PI
    "teresina": "PI",
    # AM
    "manaus": "AM",
    # PA
    "belem": "PA",
    # AP
    "macapa": "AP",
    # RR
    "boavista": "RR",
    # AC
    "rio branco": "AC",
    # AL
    "maceio": "AL",
    # PB
    "joao pessoa": "PB",
    # RN
    "natal": "RN",
    # SE
    "aracaju": "SE",
    # TO
    "palmas": "TO",
    # RO
    "porto velho": "RO",
}

# Nomes de UF (sem "para": colide com a preposição; "pa" abreviado cobre)
UF_NAMES: dict[str, str] = {
    "acre": "AC", "alagoas": "AL", "amapa": "AP", "amazonas": "AM",
    "bahia": "BA", "ceara": "CE", "espirito santo": "ES",
    "goias": "GO", "maranhao": "MA", "mato grosso": "MT",
    "mato grosso do sul": "MS", "minas gerais": "MG", "minas": "MG",
    "paraiba": "PB", "parana": "PR", "pernambuco": "PE",
    "piaui": "PI", "rio de janeiro": "RJ", "rio grande do norte": "RN",
    "rio grande do sul": "RS", "rondonia": "RO", "roraima": "RR",
    "santa catarina": "SC", "sao paulo": "SP", "sergipe": "SE",
    "tocantins": "TO", "distrito federal": "DF",
}

# Abreviaturas de UF (sem "se": colide com o pronome "se eu comprar...")
UF_ABBR: dict[str, str] = {
    "ac": "AC", "al": "AL", "ap": "AP", "am": "AM", "ba": "BA",
    "ce": "CE", "df": "DF", "es": "ES", "go": "GO", "ma": "MA",
    "mt": "MT", "ms": "MS", "mg": "MG", "pa": "PA", "pb": "PB",
    "pr": "PR", "pe": "PE", "pi": "PI", "rj": "RJ", "rn": "RN",
    "rs": "RS", "ro": "RO", "rr": "RR", "sc": "SC", "sp": "SP",
    "to": "TO",
}

UF_DISPLAY = {
    "AC": "Acre", "AL": "Alagoas", "AM": "Amazonas", "AP": "Amapá",
    "BA": "Bahia", "CE": "Ceará", "DF": "Distrito Federal",
    "ES": "Espírito Santo", "GO": "Goiás", "MA": "Maranhão",
    "MG": "Minas Gerais", "MS": "Mato Grosso do Sul", "MT": "Mato Grosso",
    "PA": "Pará", "PB": "Paraíba", "PE": "Pernambuco", "PI": "Piauí",
    "PR": "Paraná", "RJ": "Rio de Janeiro", "RN": "Rio Grande do Norte",
    "RS": "Rio Grande do Sul", "RO": "Rondônia", "RR": "Roraima",
    "SC": "Santa Catarina", "SE": "Sergipe", "SP": "São Paulo",
    "TO": "Tocantins",
}

CITY_DISPLAY = {
    "bh": "Belo Horizonte", "poa": "Porto Alegre", "rio": "Rio de Janeiro",
    "belo horizonte": "Belo Horizonte", "contagem": "Contagem",
    "uberlandia": "Uberlândia", "juiz de fora": "Juiz de Fora",
    "montes claros": "Montes Claros", "uba": "Uba",
    "ipatinga": "Ipatinga", "uberaba": "Uberaba",
    "porto alegre": "Porto Alegre", "caxias do sul": "Caxias do Sul",
    "pelotas": "Pelotas", "canoas": "Canoas", "santa maria": "Santa Maria",
    "novo hamburgurgo": "Novo Hamburgo", "sao leopoldo": "São Leopoldo",
    "sao paulo": "São Paulo", "campinas": "Campinas",
    "sao bernardo do campo": "São Bernardo do Campo",
    "guarulhos": "Guarulhos", "osasco": "Osasco", "sorocaba": "Sorocaba",
    "rio de janeiro": "Rio de Janeiro", "niteroi": "Niterói",
    "duque de caxias": "Duque de Caxias",
    "curitiba": "Curitiba", "londrina": "Londrina", "maringa": "Maringá",
    "florianopolis": "Florianópolis", "joinville": "Joinville",
    "blumenau": "Blumenau",
    "salvador": "Salvador", "feira de santana": "Feira de Santana",
    "recife": "Recife", "olinda": "Olinda", "fortaleza": "Fortaleza",
    "goiania": "Goiânia", "brasilia": "Brasília", "vitoria": "Vitória",
    "campo grande": "Campo Grande", "cuiaba": "Cuiabá",
    "sao luis": "São Luís", "teresina": "Teresina", "manaus": "Manaus",
    "belem": "Belém", "macapa": "Macapá", "boavista": "Boa Vista",
    "rio branco": "Rio Branco", "maceio": "Maceió",
    "joao pessoa": "João Pessoa", "natal": "Natal", "aracaju": "Aracaju",
    "palmas": "Palmas", "porto velho": "Porto Velho",
}

# --------------------------------------------------------------------------- #
# Alíquotas de ICMS: a regra interestadual é ÚNICA e mora em icms_rates.py
# --------------------------------------------------------------------------- #
_INTERNAL_RATES = INTERNAL_RATES

# --------------------------------------------------------------------------- #
# Natureza da operação / destinatário (palavras normalizadas)
# --------------------------------------------------------------------------- #
_SERVICE_WORDS = (
    "servico", "servicos", "software", "tecnologia", "consultoria",
    "desenvolvimento", "manutencao", "suporte", "saas", "hospedagem",
    "prestar", "prestacao", "nfse", "nfs-e",
)
_GOODS_WORDS = (
    "mercadoria", "mercadorias", "produto", "produtos", "venda",
    "vender", "comprar", "compra", "revenda", "nfe", "nf-e",
    "equipamento", "equipamentos", "maquina", "maquinas", "materiais",
    "insumos", "peca", "pecas", "estoque",
)
# Venda de licença de software (transferência de propriedade) = ICMS (jurisprudência
# STF/STJ) — mesmo que o texto cite "software" (que sozinho indica serviço)
_LICENSE_GOODS_WORDS = (
    "licenca de software", "licenca de uso", "licenca permanente",
    "licenca definitiva", "venda de licenca", "transferencia de licenca",
)
# Destinatário: só o que o cliente afirma sobre o DESTINATÁRIO decide.
# "empresa"/"cliente"/"contribuinte" sozinhos NÃO decidem: tanto quem revende
# quanto quem usa/consume são contribuintes — o DIFAL muda de regra entre eles.
_CONSUMER_FINAL_WORDS = (
    "pessoa fisica", "pf", "consumidor final", "nao contribuinte",
)
_USO_CONSUMO_WORDS = (
    "uso e consumo", "uso proprio", "consumir", "consumo proprio",
    "ativo imobilizado", "imobilizado", "usar na empresa", "usar e consumir",
    "vou usar", "vai usar",
)
_RESALE_WORDS = (
    "revenda", "revender", "vou revender", "vai revender",
)


@dataclass
class DifalCase:
    """Caso de DIFAL descrito pelo cliente (origem/destino + natureza)."""
    origin: str | None          # UF de origem (ou None)
    destination: str | None     # UF de destino (ou None)
    origin_label: str           # ex.: "Belo Horizonte/MG" ou "Minas Gerais"
    destination_label: str
    kind: str                   # servico | mercadoria | ambiguo | desconhecido
    recipient: str              # revenda | uso_consumo | consumidor_final | desconhecido
    interna_informada: Decimal | None = None   # alíquota interna dita pelo cliente


def _find_mentions(t: str) -> list[tuple[int, int, str, str]]:
    """Menções a cidade/UF no texto normalizado: [(inicio, fim, uf, label)]
    em ordem de aparição. Menções contidas em outra já mantida são descartadas
    (ex.: 'mato grosso' dentro de 'mato grosso do sul')."""
    found: list[tuple[int, int, str, str]] = []

    def add(pattern: str, uf: str, label: str) -> None:
        for m in re.finditer(r"(?<!\w)" + re.escape(pattern) + r"(?!\w)", t):
            found.append((m.start(), m.end(), uf, label))

    for city, uf in sorted(CITY_UF.items(), key=lambda kv: -len(kv[0])):
        add(city, uf, f"{CITY_DISPLAY[city]}/{uf}")
    for name, uf in sorted(UF_NAMES.items(), key=lambda kv: -len(kv[0])):
        add(name, uf, UF_DISPLAY[uf])
    for abbr, uf in UF_ABBR.items():
        add(abbr, uf, UF_DISPLAY[uf])
    # "para" = Pará (UF) SÓ depois de de/do/da/dos/das ("do Pará" -> "do para"):
    # a normalização tira o acento e "Pará" vira indistinguível da preposição
    # "para" — que por isso não está em UF_NAMES. A preposição nunca vem logo
    # depois de de/do/da ("vender para X", "para ele"), então o contexto resolve.
    for m in re.finditer(r"\b(de|do|da|dos|das)\s+para\b", t):
        s = m.end() - 4
        found.append((s, s + 4, "PA", UF_DISPLAY["PA"]))
    # "no Pará", "em Pará": o contexto de lugar deixa claro que é a UF
    # (a preposição "para" nunca vem logo depois de "no"/"em")
    for m in re.finditer(r"\b(no|na|em|numa|num)\s+para\b", t):
        s = m.end() - 4
        found.append((s, s + 4, "PA", UF_DISPLAY["PA"]))

    found.sort(key=lambda x: (x[0], -(x[1] - x[0])))
    out: list[tuple[int, int, str, str]] = []
    last_end = -1
    for start, end, uf, label in found:
        if start < last_end:
            continue
        out.append((start, end, uf, label))
        last_end = end
    return out


# Sinal explícito de que o cliente disse serem estados DIFERENTES ("são dois
# estados diferentes", "outro estado"): se o parsing der origem == destino, o
# 2º estado provavelmente não foi reconhecido — não responder "operação
# interna" com confiança; procurar UF diferente nas menções ou pedir o destino.
_DIFFERENT_STATES_RE = re.compile(
    r"\b(dois|2)\s+estados\s+(diferentes|distintos)"
    r"|\bestados\s+(diferentes|distintos)"
    r"|\boutros?\s+estados?\b"
)


def _split_origin_destination(
    t: str, mentions: list[tuple[int, int, str, str]], different: bool = False
) -> tuple[str | None, str | None, str, str]:
    """(origem, destino, label_origem, label_destino).

    Com 2+ menções: a 1ª é origem, a 2ª é destino ("de X ... para Y").
    Com 1 menção: a preposição decide ("de X" -> origem; "para X" -> destino);
    sem sinal, cai em origem ("tenho empresa em BH").
    """
    if len(mentions) >= 2:
        o, d = mentions[0], mentions[1]
        if different and o[2] == d[2]:
            # cliente disse que são estados diferentes, mas as 2 primeiras
            # menções caíram na mesma UF: o destino real provavelmente não foi
            # reconhecido — usa a 1ª UF diferente (ou pede o destino)
            alt = next((m for m in mentions[1:] if m[2] != o[2]), None)
            if alt is None:
                return o[2], None, o[3], ""
            d = alt
        return o[2], d[2], o[3], d[3]
    start, end, uf, label = mentions[0]
    before = t[:start].split()
    after = t[end:].split()
    prev = before[-1] if len(before) > 1 else ""
    nxt = after[0] if after else ""
    if prev in ("de", "da", "do", "desde"):
        return uf, None, label, ""
    if prev in ("para", "pra", "ate") or nxt in ("para", "pra"):
        return None, uf, "", label
    return uf, None, label, ""


def _classify_kind(t: str) -> str:
    has_license = any(w in t for w in _LICENSE_GOODS_WORDS)
    has_service = any(w in t for w in _SERVICE_WORDS)
    has_goods = any(w in t for w in _GOODS_WORDS)
    if has_license:
        return "mercadoria"
    if has_service and has_goods:
        return "ambiguo"
    if has_service:
        return "servico"
    if has_goods:
        return "mercadoria"
    return "desconhecido"


def _classify_recipient(t: str) -> str:
    """Destinatário da operação: só o que o cliente AFIRMA decide.

    * revenda         — vai REVENDER a mercadoria (contribuinte, sem DIFAL);
    * uso_consumo     — vai USAR/CONSUMIR ou imobilizar o bem (contribuinte,
                        com DIFAL recolhido pelo destinatário);
    * consumidor_final— pessoa física / não contribuinte (com DIFAL recolhido
                        pelo remetente);
    * desconhecido    — o resto ("cliente de MG", "uma empresa",
                        "contribuinte" sozinho): o bot PERGUNTA os 3 casos.
    """
    if any(w in t for w in _CONSUMER_FINAL_WORDS):
        return "consumidor_final"
    if any(w in t for w in _USO_CONSUMO_WORDS):
        return "uso_consumo"
    if any(w in t for w in _RESALE_WORDS):
        return "revenda"
    return "desconhecido"


# --------------------------------------------------------------------------- #
# Utilidades do cálculo (alíquota interna informada, valor da nota)
# --------------------------------------------------------------------------- #
def _soft_norm(s: str) -> str:
    """Minúsculas e sem acentos, mas MANTENDO pontuação, símbolos e %.

    ``normalize_name`` apaga "%", "R$", "." e "," — inútil para extrair a
    alíquota ("19%") e o valor da nota ("R$ 10.000,00").
    """
    return strip_accents(s or "").lower()


def _extract_internal_rate(t: str) -> Decimal | None:
    """Alíquota interna dita pelo cliente ("a interna é 19%", "19%").

    Exige o sinal de % — um "3" solto (resposta da lista de casos) NÃO é
    alíquota. Recebe texto com acentuação preservada (``_soft_norm``).
    """
    t = _soft_norm(t)
    m = re.search(r"interna\s*(?:de|e|:|=)?\s*(\d{1,2}(?:,\d+)?)\s*%", t)
    if m:
        return Decimal(m.group(1).replace(",", "."))
    # mensagem curta só com o número ("19%")
    m = re.fullmatch(r"(?:a\s+)?(\d{1,2}(?:,\d+)?)\s*%\s*", t.strip())
    if m:
        return Decimal(m.group(1).replace(",", "."))
    return None


def _extract_nota_value(t: str) -> Decimal | None:
    """Valor da nota dito pelo cliente ("R$ 10.000,00", "valor de 10000")."""
    t = _soft_norm(t)
    m = re.search(r"r\$\s*([\d.]+(?:,\d{1,2})?)", t)
    if not m:
        m = re.search(r"valor(?:\s+da\s+nota)?\s*(?:de|e|:)\s*([\d.]+(?:,\d{1,2})?)",
                      t)
    if not m:
        return None
    raw = m.group(1).replace(".", "").replace(",", ".")
    try:
        return Decimal(raw)
    except Exception:  # noqa: BLE001 - valor malformado: ignora
        return None


def _interna_do_destino(c: DifalCase) -> Decimal | None:
    """Alíquota interna do destino: a tabela do código ou a dita pelo cliente.

    Se não houver nenhuma das duas, NÃO se inventa número — o bot pede.
    """
    if c.interna_informada is not None:
        return c.interna_informada
    return _INTERNAL_RATES.get(c.destination)


def precisa_interna(c: DifalCase) -> bool:
    """True quando o caso ainda depende da alíquota interna do destino."""
    if not (c.origin and c.destination) or c.origin == c.destination:
        return False
    return _interna_do_destino(c) is None


def precisa_destinatario(c: DifalCase) -> bool:
    """True quando o caso ainda depende de saber o que vale para o destinatário."""
    return bool(
        c.kind == "mercadoria" and c.origin and c.destination
        and c.origin != c.destination and c.recipient == "desconhecido"
    )


def _brl(v: Decimal) -> str:
    """Valor em reais no formato brasileiro (1.234,56)."""
    s = f"{v:,.2f}"
    return s.replace(",", "\x00").replace(".", ",").replace("\x00", ".")


def _difal_reais_lines(difal: Decimal, text: str) -> list[str]:
    """Valor em reais do DIFAL — SÓ quando o cliente informou o valor da nota."""
    valor = _extract_nota_value(text)
    if valor is None:
        return []
    em_reais = (valor * difal / Decimal("100")).quantize(Decimal("0.01"))
    return [
        "",
        f"* valor informado: R$ {_brl(valor)} → DIFAL estimado: "
        f"R$ {_brl(em_reais)} ({difal}% sobre o valor).",
        "Obs.: o cálculo pode usar base \"por dentro\" e o estado pode cobrar "
        "adicional de FCP (fundo de combate à pobreza) — confirme o valor com "
        "seu contador.",
    ]


# --------------------------------------------------------------------------- #
# Resposta do caso
# --------------------------------------------------------------------------- #
_CONTADOR_CLOSING = (
    "Confirme a alíquota interna e as regras do estado de destino com seu contador."
)


def _ask_recipient(c: DifalCase) -> str:
    """Pergunta OBRIGATÓRIA antes de responder 'sim/não' sobre DIFAL."""
    route = f"{c.origin_label} → {c.destination_label}" \
        if c.origin and c.destination else "a operação"
    return (
        f"Antes de dizer se há DIFAL em {route}, me confirma qual destes casos "
        "vale para o destinatário?\n"
        "1) Vai REVENDER a mercadoria (destinatário contribuinte de ICMS)\n"
        "2) Vai USAR ou CONSUMIR na própria empresa, ou lançar no ATIVO "
        "IMOBILIZADO (destinatário contribuinte de ICMS)\n"
        "3) É PESSOA FÍSICA ou NÃO CONTRIBUINTE (consumidor final)\n\n"
        "Responde só o número (1, 2 ou 3) que eu calculo na hora."
    )


def _ask_interna(c: DifalCase) -> str:
    """Sem alíquota interna cadastrada: pede o número em vez de inventar."""
    return "\n".join([
        f"Me informa a alíquota interna de {UF_DISPLAY.get(c.destination, c.destination)}? "
        "Ex.: \"a interna é 19%\".",
        "Sem esse número eu não calculo o DIFAL — não vou inventar alíquota.",
        "",
        _CONTADOR_CLOSING,
    ])


def _answer_service(c: DifalCase) -> str:
    lines = [
        "Não — nesse caso não há DIFAL.",
        "",
        "O que você descreve é um serviço (lista da LC 116/2003), e serviço "
        "não incide ICMS — o DIFAL só existe dentro do ICMS. O imposto do "
        "serviço é o ISS, municipal, cobrado no município do tomador.",
    ]
    if c.destination:
        lines.append(
            f"\n* a NFS-e é emitida com destino a {c.destination_label} e o ISS é "
            f"pago/retido em {c.destination_label} (alíquota de 2% a 5%, definida "
            f"pelo município);")
        lines.append("* não há ICMS/DIFAL a recolher no estado de origem para essa "
                     "operação.")
    else:
        lines.append(
            "\n* o ISS incide onde está o tomador (quem contrata) — é lá que a NFS-e "
            "é emitida e o imposto pago (alíquota de 2% a 5%);")
        lines.append("* não há ICMS/DIFAL a recolher para essa operação, seja ela "
                     "interna ou interestadual.")
    return "\n".join(lines)


def _answer_resale(c: DifalCase, inter: Decimal) -> str:
    """Caso 1 — destinatário vai REVENDER (contribuinte): não há DIFAL."""
    return "\n".join([
        "Não — nesse caso (revenda) não há DIFAL (0%).",
        "",
        f"Operação interestadual {c.origin_label} → {c.destination_label}:",
        f"* alíquota interestadual: {int(inter)}% (cobrada pela origem);",
        "* o ICMS interestadual destacado pela origem é aproveitado pelo "
        "destinatário (contribuinte de ICMS) como CRÉDITO — não há DIFAL a "
        "recolher.",
        "",
        _CONTADOR_CLOSING,
    ])


def _answer_uso_consumo(c: DifalCase, inter: Decimal, interna: Decimal,
                        difal: Decimal, text: str) -> str:
    """Caso 2 — uso/consumo ou ativo imobilizado de contribuinte: há DIFAL."""
    lines = [
        f"Sim — há DIFAL de {difal} pontos percentuais.",
        "",
        f"Operação interestadual {c.origin_label} → {c.destination_label} "
        "(uso/consumo ou ativo imobilizado de contribuinte):",
        f"* alíquota interestadual: {int(inter)}% (cobrada pela origem);",
        f"* alíquota interna do destino: {int(interna)}%;",
        f"* DIFAL = a diferença: {difal} pontos percentuais.",
        "* Em regra, o DIFAL é recolhido pelo DESTINATÁRIO (contribuinte) ao "
        "estado de destino — confirme na legislação do estado.",
    ]
    lines += _difal_reais_lines(difal, text)
    lines += ["", _CONTADOR_CLOSING]
    return "\n".join(lines)


def _answer_consumidor_final(c: DifalCase, inter: Decimal, interna: Decimal,
                             difal: Decimal, text: str) -> str:
    """Caso 3 — pessoa física / não contribuinte: há DIFAL (remetente recolhe)."""
    lines = [
        f"Sim — há DIFAL de {difal} pontos percentuais.",
        "",
        f"Operação interestadual {c.origin_label} → {c.destination_label} "
        "(venda a pessoa física / não contribuinte — consumidor final):",
        f"* alíquota interestadual: {int(inter)}% (cobrada pela origem);",
        f"* alíquota interna do destino: {int(interna)}%;",
        f"* DIFAL = a diferença: {difal} pontos percentuais.",
        "* O REMETENTE (a empresa que vende) recolhe o DIFAL para o estado de "
        "destino.",
    ]
    lines += _difal_reais_lines(difal, text)
    lines += ["", _CONTADOR_CLOSING]
    return "\n".join(lines)


def _answer_goods(c: DifalCase, text: str = "") -> str:
    if c.origin and c.destination:
        if c.origin == c.destination:
            rate = _INTERNAL_RATES.get(c.origin)
            rate_txt = f" ({int(rate)}% em {UF_DISPLAY[c.origin]})" \
                if rate is not None else ""
            return (
                "Não — não há DIFAL.\n\n"
                f"Operação interna (origem e destino em {c.origin_label}): incide "
                f"o ICMS pela alíquota interna do estado{rate_txt}. DIFAL só existe "
                f"em operação interestadual (destinatário em outro estado).\n\n"
                f"{_CONTADOR_CLOSING}"
            )
        inter = aliquota_interestadual(c.origin, c.destination)
        if c.recipient == "desconhecido":
            return _ask_recipient(c)
        interna = _interna_do_destino(c)
        if interna is None:
            return _ask_interna(c)
        difal = interna - inter
        if c.recipient == "revenda":
            return _answer_resale(c, inter)
        if c.recipient == "uso_consumo":
            return _answer_uso_consumo(c, inter, interna, difal, text)
        return _answer_consumidor_final(c, inter, interna, difal, text)
    known = c.origin_label if c.origin else c.destination_label
    return (
        "Para te responder com o número exato, preciso dos dois estados "
        f"(origem e destino) — você citou apenas {known}.\n\n"
        "A regra: em operação interestadual de mercadoria o DIFAL é a diferença "
        "entre a alíquota interna do destino e a interestadual (4% para bens "
        "importados; 7% da origem no Sul/Sudeste com destino no Norte, "
        "Nordeste, Centro-Oeste ou ES; 12% nos demais casos) — e ele só é "
        "devido em caso de USO/CONSUMO (ou ativo imobilizado) de contribuinte "
        "e de VENDA A PESSOA FÍSICA/NÃO CONTRIBUINTE; na REVENDA não há DIFAL. "
        "Operação interna (mesmo estado) não gera DIFAL.\n\n"
        f"{_CONTADOR_CLOSING}"
    )


def _answer_unknown(c: DifalCase) -> str:
    """Sem palavra de natureza (serviço x mercadoria): o cliente não
    especificou a operação — responde os dois cenários, com o número do
    DIFAL quando origem e destino são conhecidos."""
    route = f" ({c.origin_label} → {c.destination_label})" \
        if c.origin and c.destination else ""
    lines = [
        "Depende da natureza da operação — você não especificou se é "
        "serviço ou mercadoria, e DIFAL só existe dentro do ICMS "
        "(mercadoria).",
        "",
    ]
    if c.destination:
        lines.append(
            f"* se for serviço (NFS-e, LC 116/2003){route}: não há "
            f"DIFAL — o imposto é o ISS, pago/retido em {c.destination_label} "
            "(alíquota de 2% a 5%, definida pelo município);"
        )
    else:
        lines.append(
            "* se for serviço (NFS-e, LC 116/2003): não há DIFAL — o "
            "imposto é o ISS, pago no município do tomador;"
        )
    if c.origin and c.destination:
        if c.origin == c.destination:
            lines.append(
                f"* se for mercadoria (NFe){route}: não há DIFAL — "
                "operação interna (mesmo estado), ICMS pela alíquota interna."
            )
        else:
            inter = aliquota_interestadual(c.origin, c.destination)
            interna = _interna_do_destino(c)
            if interna is not None:
                difal = interna - inter
                lines.append(
                    f"* se for mercadoria (NFe){route}: a alíquota interestadual "
                    f"é {int(inter)}% e a interna do destino {int(interna)}% — "
                    "qual destes casos vale para o destinatário?"
                )
                lines.append(
                    "  1) revenda (contribuinte): não há DIFAL (0%);"
                )
                lines.append(
                    f"  2) uso/consumo ou ativo imobilizado (contribuinte): "
                    f"DIFAL de {difal} pontos percentuais, recolhido pelo "
                    "destinatário;"
                )
                lines.append(
                    f"  3) pessoa física / não contribuinte: DIFAL de "
                    f"{difal} pontos percentuais, recolhido pelo remetente."
                )
            else:
                lines.append(
                    f"* se for mercadoria (NFe){route}: a alíquota interestadual "
                    f"é {int(inter)}%; me informa a alíquota interna de "
                    f"{UF_DISPLAY[c.destination]} (ex.: \"a interna é 19%\") "
                    "que eu calculo o DIFAL (não invento alíquota)."
                )
    else:
        lines.append(
            "* se for mercadoria (NFe): em operação interestadual o DIFAL é a "
            "diferença entre a alíquota interna do destino e a interestadual "
            "(4% para importados; 7% da origem no Sul/Sudeste com destino no "
            "Norte, Nordeste, Centro-Oeste ou ES; 12% nos demais casos) — há "
            "DIFAL no uso/consumo (ou ativo imobilizado) de contribuinte e na "
            "venda a pessoa física/não contribuinte; na revenda não há; "
            "operação interna também não gera DIFAL."
        )
    lines += [
        "",
        "Me confirma se é serviço ou mercadoria e, se for mercadoria, "
        "qual destes casos vale para o destinatário: 1) revenda, 2) "
        "uso/consumo ou ativo imobilizado, 3) pessoa física/não contribuinte?",
    ]
    lines += ["", _CONTADOR_CLOSING]
    return "\n".join(lines)


def _answer_ambiguous(c: DifalCase) -> str:
    route = ""
    if c.origin and c.destination:
        route = f" ({c.origin_label} → {c.destination_label})"
    return (
        "Depende da natureza da operação — você citou serviço e mercadoria junto.\n\n"
        f"* se for serviço (LC 116/2003){route}: não há DIFAL — incide ISS no "
        "município do tomador;\n"
        f"* se for mercadoria{route}: em operação interestadual há DIFAL no "
        "uso/consumo (ou ativo imobilizado) de contribuinte e na venda a pessoa "
        "física/não contribuinte (diferença entre a alíquota interna do destino "
        "e a interestadual); na revenda não há DIFAL.\n\n"
        "Me confirma qual é a operação (ou me manda a nota) que eu te dou o número exato."
        "\n\n" + _CONTADOR_CLOSING
    )


# --------------------------------------------------------------------------- #
# API pública
# --------------------------------------------------------------------------- #
def analyze_difal_case(text: str) -> DifalCase | None:
    """Analisa o caso de DIFAL descrito no texto.

    Retorna ``None`` quando o texto não cita estado de origem/destino (não é
    um caso — ex.: "o que é DIFAL?").
    """
    t = normalize_name(text)
    mentions = _find_mentions(t)
    if not mentions:
        return None
    different = bool(_DIFFERENT_STATES_RE.search(t))
    origin, destination, o_label, d_label = _split_origin_destination(
        t, mentions, different)
    return DifalCase(
        origin=origin,
        destination=destination,
        origin_label=o_label,
        destination_label=d_label,
        kind=_classify_kind(t),
        recipient=_classify_recipient(t),
        interna_informada=_extract_internal_rate(text),
    )


def answer_difal_case(case: DifalCase, text: str = "") -> str:
    """Resposta direta ao caso de DIFAL do cliente (determinística).

    ``text`` é a pergunta completa do cliente (usada, entre outras coisas,
    para saber se ele informou o VALOR da nota — aí o DIFAL sai também em
    reais, com o alerta de base "por dentro"/FCP).
    """
    if case.kind == "servico":
        return _answer_service(case)
    if case.kind == "mercadoria":
        return _answer_goods(case, text=text)
    if case.kind == "ambiguo":
        return _answer_ambiguous(case)
    # "desconhecido" (sem palavra de natureza): o cliente não especificou a
    # operação — responde os dois cenários (serviço x mercadoria)
    return _answer_unknown(case)


__all__ = [
    "DifalCase", "analyze_difal_case", "answer_difal_case",
    "precisa_destinatario", "precisa_interna",
]
