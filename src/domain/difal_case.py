"""Análise determinística de CASOS de DIFAL descritos pelo cliente.

Responde perguntas sobre o caso do cliente DIRETAMENTE — ex.:
"tenho uma empresa de software em BH e vou prestar serviço para uma empresa
de POA, vou pagar DIFAL?" — a partir de:

  * origem/destino (estados, por cidade ou UF citada no texto);
  * natureza da operação (serviço LC 116/2003 x mercadoria);
  * destinatário (empresa/contribuinte x pessoa física).

Determinístico (funciona sem LLM):
  * serviço (LC 116/2003) -> NÃO incide DIFAL; ISS no município do tomador;
  * operação interna (mesmo estado) -> NÃO incide DIFAL;
  * mercadoria interestadual -> DIFAL = alíquota interna do destino −
    interestadual (4%/7%/12%); destinatário não contribuinte -> MOC
    (cobrança por antecipação pelo destino).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from .rules import normalize_name


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
# Alíquotas de ICMS (valores de referência)
# --------------------------------------------------------------------------- #
# Alíquota interna padrão (%) por UF — apenas estados com padrão conhecido
# (Pará fica de fora: alíquota varia por produto).
_INTERNAL_RATES: dict[str, Decimal] = {
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

# Regiões com alíquota interestadual de 12% (com crédito)
_SOUTHEAST = frozenset({"SP", "RJ", "ES", "MG"})
_SOUTH = frozenset({"PR", "SC", "RS"})

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
_COMPANY_WORDS = ("empresa", "contribuinte", "sociedade", "ltda",
                  "estabelecimento", "fornecedor", "cliente")


@dataclass
class DifalCase:
    """Caso de DIFAL descrito pelo cliente (origem/destino + natureza)."""
    origin: str | None          # UF de origem (ou None)
    destination: str | None     # UF de destino (ou None)
    origin_label: str           # ex.: "Belo Horizonte/MG" ou "Minas Gerais"
    destination_label: str
    kind: str                   # servico | mercadoria | ambiguo | desconhecido
    recipient: str              # empresa | pf | desconhecido


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
    if re.search(r"\bpessoa fisica\b", t) or re.search(r"\bpf\b", t) \
            or "consumidor" in t or "nao contribuinte" in t:
        return "pf"
    if any(w in t for w in _COMPANY_WORDS):
        return "empresa"
    return "desconhecido"

# --------------------------------------------------------------------------- #
# Resposta do caso
# --------------------------------------------------------------------------- #
def _interstate_rate(origin: str, destination: str, credit: bool = True) -> Decimal:
    """Alíquota interestadual padrão (EC 87/2015): 12% na mesma região
    (Sudeste/Sul); nas demais, 4% (com crédito) ou 7% (sem crédito — destino
    não contribuinte / MOC)."""
    if (origin in _SOUTHEAST and destination in _SOUTHEAST) \
            or (origin in _SOUTH and destination in _SOUTH):
        return Decimal("12")
    return Decimal("4") if credit else Decimal("7")


def _answer_service(c: DifalCase) -> str:
    lines = [
        "**Não — nesse caso não há DIFAL.**",
        "",
        "O que você descreve é um **serviço** (lista da LC 116/2003), e serviço "
        "não incide ICMS — o DIFAL só existe dentro do ICMS. O imposto do "
        "serviço é o **ISS**, municipal, cobrado no município do tomador.",
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


def _answer_goods(c: DifalCase) -> str:
    if c.origin and c.destination:
        if c.origin == c.destination:
            rate = _INTERNAL_RATES.get(c.origin)
            rate_txt = f" ({int(rate)}% em {UF_DISPLAY[c.origin]})" \
                if rate is not None else ""
            return (
                "**Não — não há DIFAL.**\n\n"
                f"Operação **interna** (origem e destino em {c.origin_label}): incide "
                f"o ICMS pela alíquota interna do estado{rate_txt}. DIFAL só existe "
                f"em operação interestadual (destinatário em outro estado)."
            )
        inter = _interstate_rate(c.origin, c.destination, credit=c.recipient != "pf")
        interna = _INTERNAL_RATES.get(c.destination)
        if interna is None:
            return (
                "**Sim — há DIFAL nessa operação interestadual.**\n\n"
                f"Operação {c.origin_label} → {c.destination_label}: a origem cobra a "
                f"alíquota interestadual ({int(inter)}%) e o estado de destino cobra a "
                f"diferença até a alíquota interna dele (o DIFAL). A alíquota interna "
                f"de {UF_DISPLAY[c.destination]} não está na minha tabela de referência "
                f"— confirme na SEFAZ do destino."
            )
        difal = interna - inter
        lines = [
            f"**Sim — há DIFAL de {int(difal)}%.**",
            "",
            f"Operação interestadual {c.origin_label} → {c.destination_label}:",
            f"* alíquota interestadual: {int(inter)}% (cobrada pela origem);",
            f"* alíquota interna do destino: {int(interna)}%;",
            f"* DIFAL = a diferença: **{int(difal)}%** (cobrado pelo estado de destino).",
        ]
        if c.recipient == "pf":
            lines += [
                "",
                "⚠️ Destinatário pessoa física / não contribuinte: é o caso de **MOC** "
                "(mercadoria para consumo e uso) — o DIFAL é cobrado por antecipação "
                "pelo estado de destino, já embutido no preço da nota.",
            ]
        return "\n".join(lines)
    known = c.origin_label if c.origin else c.destination_label
    return (
        "Para te responder com o número exato, preciso dos **dois** estados "
        f"(origem e destino) — você citou apenas {known}.\n\n"
        "A regra: DIFAL só existe em operação **interestadual** de mercadoria — "
        "é a diferença entre a alíquota interna do estado de destino e a "
        "interestadual (4%, 7% ou 12%). Operação interna (mesmo estado) não "
        "gera DIFAL."
    )


def _answer_unknown(c: DifalCase) -> str:
    """Sem palavra de natureza (serviço x mercadoria): o cliente não
    especificou a operação — responde os dois cenários, com o número do
    DIFAL quando origem e destino são conhecidos."""
    route = f" ({c.origin_label} → {c.destination_label})" \
        if c.origin and c.destination else ""
    lines = [
        "**Depende da natureza da operação** — você não especificou se é "
        "serviço ou mercadoria, e DIFAL só existe dentro do ICMS "
        "(mercadoria).",
        "",
    ]
    if c.destination:
        lines.append(
            f"* se for **serviço** (NFS-e, LC 116/2003){route}: **não há "
            f"DIFAL** — o imposto é o ISS, pago/retido em {c.destination_label} "
            "(alíquota de 2% a 5%, definida pelo município);"
        )
    else:
        lines.append(
            "* se for **serviço** (NFS-e, LC 116/2003): **não há DIFAL** — o "
            "imposto é o ISS, pago no município do tomador;"
        )
    if c.origin and c.destination:
        if c.origin == c.destination:
            lines.append(
                f"* se for **mercadoria** (NFe){route}: **não há DIFAL** — "
                "operação interna (mesmo estado), ICMS pela alíquota interna."
            )
        else:
            inter = _interstate_rate(c.origin, c.destination,
                                     credit=c.recipient != "pf")
            interna = _INTERNAL_RATES.get(c.destination)
            if interna is not None:
                difal = interna - inter
                lines.append(
                    f"* se for **mercadoria** (NFe){route}: **há DIFAL de "
                    f"{int(difal)}%** — a origem cobra a interestadual "
                    f"({int(inter)}%) e o destino cobra a diferença até a "
                    f"alíquota interna dele ({int(interna)}%)."
                )
                if c.recipient == "pf":
                    lines.append(
                        "  - Destinatário pessoa física / não contribuinte: "
                        "é o caso de **MOC** — o DIFAL é cobrado por "
                        "antecipação pelo estado de destino."
                    )
            else:
                lines.append(
                    f"* se for **mercadoria** (NFe){route}: há DIFAL — "
                    f"diferença entre a alíquota interna do destino e a "
                    f"interestadual ({int(inter)}%); a interna de "
                    f"{UF_DISPLAY[c.destination]} não está na minha tabela "
                    "de referência — confirme na SEFAZ do destino."
                )
    else:
        lines.append(
            "* se for **mercadoria** (NFe): há DIFAL quando a operação é "
            "interestadual — diferença entre a alíquota interna do destino "
            "e a interestadual (4%, 7% ou 12%); operação interna não gera "
            "DIFAL."
        )
    lines += [
        "",
        "Me confirma qual é a operação (ou me manda a nota) que eu te dou o "
        "detalhamento completo.",
    ]
    return "\n".join(lines)


def _answer_ambiguous(c: DifalCase) -> str:
    route = ""
    if c.origin and c.destination:
        route = f" ({c.origin_label} → {c.destination_label})"
    return (
        "**Depende da natureza da operação** — você citou serviço e mercadoria junto.\n\n"
        f"* se for **serviço** (LC 116/2003){route}: **não há DIFAL** — incide ISS no "
        "município do tomador;\n"
        f"* se for **mercadoria**{route}: há DIFAL quando a operação é interestadual "
        "(diferença entre a alíquota interestadual e a interna do destino).\n\n"
        "Me confirma qual é a operação (ou me manda a nota) que eu te dou o número exato."
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
    )


def answer_difal_case(case: DifalCase) -> str:
    """Resposta direta ao caso de DIFAL do cliente (determinística)."""
    if case.kind == "servico":
        return _answer_service(case)
    if case.kind == "mercadoria":
        return _answer_goods(case)
    if case.kind == "ambiguo":
        return _answer_ambiguous(case)
    # "desconhecido" (sem palavra de natureza): o cliente não especificou a
    # operação — responde os dois cenários (serviço x mercadoria)
    return _answer_unknown(case)


__all__ = ["DifalCase", "analyze_difal_case", "answer_difal_case"]
