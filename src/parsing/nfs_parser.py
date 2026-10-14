"""Parser de NFS-e (Nota Fiscal de Serviços Eletrônica) a partir de PDF.

As NFS-e municipais (ex.: Sorocaba) usam layout de formulário em que a
extração de texto *linear* do pypdf embaralha rótulos e valores (ex.: o
bloco "CÁLCULO DO ISSQN" sai como ``5,000000 1,5430,90 0,00 30,900,00``).
Por isso a estratégia principal é a extração **por posição**:

  1. Coleta as palavras com coordenadas (x, y) via ``visitor_text``.
  2. Agrupa em linhas por ``y`` (topo -> base).
  3. Localiza a linha de rótulos de um bloco e casa cada rótulo com o valor
     cujo ``x`` fica mais próximo — ex.: "Base Cálculo ISSQN" -> 30,90.

Há também um fallback por regex sobre o texto linear (``extract_nfs_from_text``),
usado para PDFs "limpos", arquivos .txt e saída de OCR (onde não há posição).

Detecta NFS-e por marcadores ("NFS-e", "NOTA FISCAL DE SERVIÇOS", "ISSQN",
"PREFEITURA").
"""
from __future__ import annotations

import hashlib
import re
from decimal import Decimal
from itertools import groupby
from pathlib import Path

from ..domain.models import Invoice, TaxLine
from ..domain.rules import parse_amount, parse_date
from ..domain.tax import InvoiceAudit, detect_regime, verify_invoice_taxes

_NFS_MARKERS = re.compile(
    r"nfs-?e|nota\s+fiscal\s+de\s+servi[çc]o[s]?|issqn|prefeitura",
    re.I,
)
_CNPJ_RE = re.compile(r"\b\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}\b")
_CPF_RE = re.compile(r"\b\d{3}\.\d{3}\.\d{3}-\d{2}\b")
_DOC_RE = re.compile(r"\b(\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}|\d{3}\.\d{3}\.\d{3}-\d{2})\b")
_AMT_RE = re.compile(r"\d{1,3}(?:\.\d{3})*,\d{2}|\d+,\d{2}")
_DATE_RE = re.compile(r"\b(\d{2}/\d{2}/\d{4})\b")
_COMP_RE = re.compile(r"\b(\d{2}/\d{4})\b")
_NUM_SER_RE = re.compile(r"(\d{3,})\s*/\s*([A-Za-z0-9]+)")
_CODE_RE = re.compile(r"[A-Za-z0-9]{6,14}")


# --------------------------------------------------------------------------- #
# Extração por posição (PDF)
# --------------------------------------------------------------------------- #
def _pos_words(page) -> list[tuple[float, float, str]]:
    words: list[tuple[float, float, str]] = []

    def visit(text, cm, tm, font_dict, font_size):
        t = (text or "").strip()
        if t:
            for w in t.split():
                words.append((float(tm[4]), float(tm[5]), w))

    page.extract_text(visitor_text=visit)
    return words


def _lines(words: list[tuple[float, float, str]]):
    """Agrupa palavras em linhas por ``y``. Retorna [(y, [(x, word), ...])] topo->base."""
    ws = sorted(words, key=lambda w: (-w[1], w[0]))
    out = []
    for y, grp in groupby(ws, key=lambda w: round(w[1])):
        out.append((y, [(x, w) for x, _y, w in grp]))
    return out


def _line_text(line) -> str:
    return " ".join(w for _x, w in line[1])


def _find_line(lines, *needles: str):
    """Primeira linha (topo->base) cujo texto contém QUALQUER dos needles."""
    for line in lines:
        txt = _line_text(line).lower()
        if any(n.lower() in txt for n in needles):
            return line
    return None


def _below(lines, y: float, gap: float = 15.0):
    """A linha mais próxima *abaixo* de ``y`` (menor y), dentro de ``gap``."""
    cands = [l for l in lines if y - gap <= l[0] < y]
    cands.sort(key=lambda l: -l[0])
    return cands[0] if cands else None


def _below_all(lines, y: float, gap: float = 20.0):
    """Todas as linhas *abaixo* de ``y``, dentro de ``gap``."""
    return [l for l in lines if y - gap <= l[0] < y]


_LABELISH = re.compile(
    r"^\s*(e-?mail|endereço|endereco|município|municipio|cpf|cnpj|inscrição|inscricao|"
    r"telefone|cep|uf|data|valor|base|alíquota|alquota|número|numero|série|serie|código|codigo)\b",
    re.I,
)


def _pick_name_line(lines, y: float, gap: float = 25.0):
    """Linha abaixo de ``y`` que parece um nome (nem e-mail, nem rótulo)."""
    cands = [l for l in lines if y - gap <= l[0] < y]
    cands.sort(key=lambda l: -l[0])
    for l in cands:
        txt = _line_text(l)
        if "@" in txt:
            continue
        if txt.rstrip().endswith(":"):
            continue
        if _LABELISH.search(txt):
            continue
        if len(txt.split()) >= 2:
            return l
    return None


def _label_x(line, needle: str, which: str = "first") -> float | None:
    """X da (primeira|última) palavra do rótulo que contém ``needle``."""
    xs = [x for x, w in line[1] if needle.lower() in w.lower()]
    if not xs:
        return None
    return xs[0] if which == "first" else xs[-1]


def _match_x(label_line, value_lines, needle: str, which: str = "first"):
    """Token de ``value_lines`` cujo ``x`` fica mais próximo do rótulo ``needle``."""
    if not label_line:
        return None
    lx = _label_x(label_line, needle, which)
    if lx is None:
        return None
    best, bestd = None, float("inf")
    for vl in value_lines:
        for x, w in vl[1]:
            d = abs(x - lx)
            if d < bestd:
                bestd, best = d, w
    return best


def _column_value(label_line, value_lines, needle: str, which: str = "first"):
    """Token de ``value_lines`` que está na MESMA COLUNA do rótulo ``needle``.

    Nos formulários de NFS-e os rótulos ficam alinhados à esquerda e os valores
    à direita da coluna, então o "x mais próximo" do rótulo costuma ser o valor
    da coluna seguinte. Aqui cada valor é atribuído à última coluna cujo x de
    rótulo fica à esquerda do valor (com tolerância para valores que caem
    levemente à esquerda do rótulo).
    """
    if not label_line:
        return None
    lx = _label_x(label_line, needle, which)
    if lx is None:
        return None
    cols = sorted({round(x) for x, _w in label_line[1]})
    merged: list[float] = []
    for x in cols:
        if not merged or x - merged[-1] > 15:
            merged.append(x)
    left = [c for c in merged if c <= lx + 10]
    if not left:
        return None
    c0 = left[-1]  # coluna do rótulo
    best, bestd = None, float("inf")
    for vl in value_lines:
        for x, w in vl[1]:
            own = [c for c in merged if c <= x + 10]
            if not own or own[-1] != c0:
                continue  # valor de outra coluna
            d = x - c0 if x >= c0 else (c0 - x) + 40
            if d < bestd:
                bestd, best = d, w
    return best


def _amt(token: str | None):
    if token is None:
        return None
    t = token.strip()
    return parse_amount(t) if _AMT_RE.fullmatch(t) else None


_ALIQ_RE = re.compile(r"\d+,\d+")


def _alq(token: str | None):
    """Alíquota: aceita mais decimais que _amt (ex.: '5,000000')."""
    if token is None:
        return None
    t = token.strip()
    return parse_amount(t) if _ALIQ_RE.fullmatch(t) else None


def _build(source: str, *, number, series, issue_date, competency, code,
           issuer, issuer_doc, customer, customer_doc,
           valor_servico, base, aliquota, valor_issqn, valor_liquido,
           retentions=None, discounts=None, city=None, service_code=None):
    """Monta Invoice + InvoiceAudit a partir dos campos extraídos."""
    amount = valor_servico
    if amount is None and valor_liquido is not None:
        amount = valor_liquido
    if amount is None:
        return None, None
    number = number or "NFS"
    h = hashlib.md5(f"{source}|{number}|{issuer or ''}|{amount}".encode()).hexdigest()[:8]
    tax_lines = []
    if valor_issqn is not None or base is not None or aliquota is not None:
        tax_lines.append(TaxLine(name="ISSQN", base=base, rate=aliquota, amount=valor_issqn))
    inv = Invoice(
        id=f"INV-{h}",
        number=number,
        issuer=issuer or "Desconhecido",
        issue_date=issue_date,
        amount=amount,
        description=(f"NFS-e {number}" + (f" série {series}" if series else "")),
        nf_type="NFS-e",
        series=series,
        access_key=code,
        issuer_doc=issuer_doc,
        customer=customer,
        customer_doc=customer_doc,
        competency=competency,
        tax_lines=tax_lines,
        source_file=source,
    )
    audit = InvoiceAudit(
        source=source,
        issuer=inv.issuer,
        number=number,
        issue_date=issue_date,
        amount=amount,
        subtotal=base if base is not None else amount,
        tax_lines=tax_lines,
        nf_type="NFS-e",
        series=series,
        access_key=code,
        issuer_doc=issuer_doc,
        customer=customer,
        customer_doc=customer_doc,
        competency=competency,
        service_value=valor_servico,
        retentions=retentions,
        discounts=discounts,
        net_value=valor_liquido,
        city=city,
        service_code=service_code,
    )
    verify_invoice_taxes(audit)
    return inv, audit


def _extract_position(words, source: str):
    """Extrai os campos de uma NFS-e a partir das palavras com posição."""
    lines = _lines(words)

    # --- DADOS DA NFS-e (linha de rótulos + linha de valores) -------------- #
    number = series = code = competency = None
    issue_date = None
    dados = _find_line(lines, "Código de Verificação", "Competência da NFS-e")
    if dados:
        val = _below(lines, dados[0], gap=15)
        if val:
            vtext = _line_text(val)
            # número/série: tokens próximos do rótulo "Número"
            nx = _label_x(dados, "Número")
            if nx is not None:
                toks = " ".join(w for x, w in val[1] if abs(x - nx) <= 30)
                m = _NUM_SER_RE.search(toks)
                if m:
                    number, series = m.group(1), m.group(2)
            # data de emissão
            d = _match_x(dados, [val], "Emissão")
            if d:
                dm = _DATE_RE.search(d)
                if dm:
                    issue_date = parse_date(dm.group(1))
            # competência (MM/AAAA) — casa por X p/ não pegar o mês da data
            c = _match_x(dados, [val], "Competência")
            if c and _COMP_RE.fullmatch(c.strip()):
                competency = c.strip()
            # código de verificação
            cd = _match_x(dados, [val], "Verificação")
            if cd and _CODE_RE.fullmatch(cd.strip()):
                code = cd.strip()

    # --- EMITENTE ---------------------------------------------------------- #
    issuer = issuer_doc = None
    emi = _find_line(lines, "EMITENTE DA NFS-e", "EMITENTE")
    if emi:
        for line in _below_all(lines, emi[0], gap=60):
            docs = _DOC_RE.findall(_line_text(line))
            if docs:
                issuer_doc = docs[0]
                break
        nome = _find_line(lines, "Nome/Razão Social", "Nome/Razao Social", "Razão Social", "Razao Social")
        if nome:
            v = _pick_name_line(lines, nome[0], gap=25)
            if v:
                issuer = _line_text(v).strip() or None

    # --- TOMADOR ----------------------------------------------------------- #
    customer = customer_doc = None
    tom = _find_line(lines, "TOMADOR DO SERVIÇO", "TOMADOR")
    if tom:
        for line in _below_all(lines, tom[0], gap=60):
            docs = _DOC_RE.findall(_line_text(line))
            if docs:
                customer_doc = docs[0]
                break
        nome = _find_line(lines, "Nome/Nome")
        if nome:
            v = _pick_name_line(lines, nome[0], gap=25)
            if v:
                customer = _line_text(v).strip() or None

    # --- CÁLCULO DO ISSQN -------------------------------------------------- #
    valor_servico = base = aliquota = valor_issqn = None
    calc = _find_line(lines, "CÁLCULO DO ISSQN", "Calculo do ISSQN")
    if calc:
        labels = _below(lines, calc[0], gap=15)
        if labels:
            vals = _below_all(lines, labels[0], gap=15)
            valor_servico = _amt(_column_value(labels, vals, "Serviço"))
            base = _amt(_column_value(labels, vals, "Base"))
            a = _column_value(labels, vals, "Alíquota") or _column_value(labels, vals, "Alquota")
            aliquota = _alq(a)
            valor_issqn = _amt(_column_value(labels, vals, "ISSQN", which="last"))

    # --- VALOR TOTAL DA NOTA ---------------------------------------------- #
    valor_liquido = total_base = retentions = discounts = None
    total = _find_line(lines, "VALOR TOTAL DA NOTA")
    if total:
        labels = _below(lines, total[0], gap=15)
        if labels:
            vals = _below_all(lines, labels[0], gap=20)
            total_base = _amt(_column_value(labels, vals, "Base"))
            retentions = (_amt(_column_value(labels, vals, "Retenções"))
                          or _amt(_column_value(labels, vals, "Retencao")))
            discounts = _amt(_column_value(labels, vals, "Descontos"))
            vl = _column_value(labels, vals, "Líquido") or _column_value(labels, vals, "Liquido")
            valor_liquido = _amt(vl)

    # base mascarada ("*****") no bloco ISSQN: usa a do bloco total
    if base is None and total_base is not None:
        base = total_base

    # --- município da incidência + código do serviço (tabela de alíquotas) -- #
    city = service_code = None
    inc = _find_line(lines, "Município da Incidência do ISSQN",
                     "Municipio da Incidencia do ISSQN")
    if inc:
        vals = _below_all(lines, inc[0], gap=15)
        c = _column_value(inc, vals, "Município", which="first")
        if c:
            city = c.strip()
    for line in lines:
        m = re.search(r"Servi[çc]o:\s*(\d{2}\.\d{2})", _line_text(line))
        if m:
            service_code = m.group(1)
            break

    return _build(
        source,
        number=number, series=series, issue_date=issue_date,
        competency=competency, code=code,
        issuer=issuer, issuer_doc=issuer_doc,
        customer=customer, customer_doc=customer_doc,
        valor_servico=valor_servico, base=base,
        aliquota=aliquota, valor_issqn=valor_issqn,
        valor_liquido=valor_liquido,
        retentions=retentions, discounts=discounts,
        city=city, service_code=service_code,
    )


def parse_nfs_pdf(path: str | Path) -> tuple[list[Invoice], list[InvoiceAudit], list[str]]:
    """Tenta ler o PDF como NFS-e. Retorna ([], [], []) se não parecer NFS-e."""
    from pypdf import PdfReader
    path = Path(path)
    source = path.name
    try:
        reader = PdfReader(str(path))
    except Exception as e:  # noqa: BLE001
        return [], [], [f"Falha ao abrir PDF {path.name}: {e}"]

    pages = reader.pages
    words: list[tuple[float, float, str]] = []
    all_text = []
    for pg in pages:
        try:
            words.extend(_pos_words(pg))
        except Exception:  # noqa: BLE001
            pass
        try:
            all_text.append(pg.extract_text() or "")
        except Exception:  # noqa: BLE001
            pass
    text = "\n".join(all_text)

    if not _NFS_MARKERS.search(text):
        return [], [], []  # não é NFS-e; deixa outro parser cuidar

    inv, audit = _extract_position(words, source)
    if inv is None:
        # fallback: regex sobre o texto linear (PDF limpo / OCR / .txt)
        inv, audit = extract_nfs_from_text(text, source)
    if inv is None:
        return [], [], [f"{path.name}: parece NFS-e, mas não consegui extrair o valor."]
    if audit is not None:
        _patch_danfse(text, inv, audit)
        audit.regime, audit.regime_evidence = detect_regime(text)
    return [inv], [audit] if audit else [], []


def _field(text: str, *labels: str) -> str | None:
    """Valor (linha seguinte) de um rótulo do DANFSe em texto linear."""
    for lab in labels:
        m = re.search(lab + r"\s*\n([^\n]+)", text, re.IGNORECASE)
        if m:
            v = m.group(1).strip()
            if v and v != "-":
                return v
    return None


def _patch_danfse(text: str, inv: Invoice, audit: InvoiceAudit) -> None:
    """Completa uma NFS-e lida pelo layout de posições com o que o DANFSe
    (sistema próprio do município, ex.: Sorocaba) traz no texto linear.

    Os rótulos do DANFSe ("Nome / Nome Empresarial", "BC ISSQN",
    "Município ... de Incidência do ISSQN") não batem com os da NFS-e de
    formulário genérico, então emitente/tomador/base/ISSQN saem vazios — e
    o parecer fica sem emitente e sem imposto nenhum. Só preenche o que
    faltou (não sobrescreve o que o parser posicional já achou)."""
    if not re.search(r"DANFSe|Documento Auxiliar da NFS-e|BC\s*ISSQN",
                     text, re.IGNORECASE):
        return

    # emitente / tomador: dois "Nome / Nome Empresarial" (prestador e tomador)
    names = re.findall(r"Nome\s*/\s*Nome Empresarial\s*\n\s*([^\n]+)", text)
    if names:
        if not inv.issuer or inv.issuer == "Desconhecido":
            inv.issuer = names[0].strip()
            audit.issuer = inv.issuer
        if len(names) > 1 and (not audit.customer
                               or audit.customer == "Indicador Municipal"):
            audit.customer = names[1].strip()
            inv.customer = audit.customer

    # município da incidência (necessário p/ tabela de alíquotas de ISS)
    if not audit.city:
        city = _field(text, r"Munic[íi]pio\s*/\s*Sigla UF\s*/\s*Pa[íi]s de "
                            r"Incid[êe]ncia do ISSQN",
                      r"Local da Presta[çc][ãa]o\s*/\s*Sigla UF")
        if city:
            audit.city = city.split("/")[0].strip()

    # bloco de ISSQN: BC, alíquota aplicada e ISSQN apurado
    base = aliquota = apurado = None
    m = re.search(r"BC\s*ISSQN\s*\n\s*R\$\s*([\d.,]+)", text)
    if m:
        base = _amt(m.group(1))
    a = _alq(_field(text, r"Al[íi]quota Aplicada"))
    if a is not None:
        aliquota = a
    ap = _amt(_field(text, r"ISSQN Apurado"))
    if ap is not None:
        apurado = ap

    simples = bool(re.search(r"simples\s+nacional", text, re.IGNORECASE))
    if base is not None and not any(_is_iss_line(t) for t in audit.tax_lines):
        if apurado is not None:
            audit.tax_lines.append(TaxLine(name="ISSQN", base=base,
                                           rate=aliquota, amount=apurado))
        elif simples:
            # Simples Nacional: ISSQN não é apurado à parte ("-") — vai no DAS
            audit.tax_lines.append(TaxLine(
                name="ISSQN (Simples — no DAS)", base=base,
                rate=Decimal("0"), amount=Decimal("0.00")))
        else:
            audit.tax_lines.append(TaxLine(name="ISSQN", base=base,
                                           rate=aliquota, amount=apurado))
        if audit.subtotal is None:
            audit.subtotal = base
        verify_invoice_taxes(audit)  # re-verifica (substitui os checks)


def _is_iss_line(tl: TaxLine) -> bool:
    n = (tl.name or "").upper()
    return "ISS" in n and not any(p in n for p in ("RET", "IRRF"))


# --------------------------------------------------------------------------- #
# Fallback por texto linear (PDF limpo / .txt / OCR)
# --------------------------------------------------------------------------- #
def extract_nfs_from_text(text: str, source: str):
    """Extrai NFS-e de texto linear (melhor esforço). Retorna (Invoice|None, audit|None)."""
    if not (text or "").strip() or not _NFS_MARKERS.search(text):
        return None, None
    t = text

    number = series = None
    # 1º: rótulo explícito ("Número da Nota", "Nº da NFS-e") — mais confiável
    m = re.search(r"(?:n[úu]mero|n[ºo°])\s+da\s+(?:nota|nfs-?e)\s*:?\s*\n?\s*(\d{3,})",
                  t, re.I)
    if m:
        number = m.group(1)
    else:
        # 2º: "número / série"; mascara CNPJ/CPF p/ não pegar dígito de documento
        # (ex.: "99.999.999/9999-99" virava número "999")
        t_masked = _DOC_RE.sub(" ", t)
        m = _NUM_SER_RE.search(t_masked)
        if m:
            number, series = m.group(1), m.group(2)

    issue_date = None
    m = _DATE_RE.search(t)
    if m:
        issue_date = parse_date(m.group(1))

    # competência: remove datas completas antes p/ não pegar o mês da data
    competency = None
    t_no_dates = _DATE_RE.sub(" ", t)
    m = _COMP_RE.search(t_no_dates)
    if m:
        competency = m.group(1)

    # código de verificação: alfanumérico, sem e-mail
    code = None
    t_no_email = re.sub(r"[\w.+-]+@[\w.-]+\.\w+", " ", t)
    for cand in _CODE_RE.findall(t_no_email):
        if any(c.isalpha() for c in cand) and any(c.isdigit() for c in cand):
            code = cand
            break

    docs = _DOC_RE.findall(t)
    issuer_doc = docs[0] if docs else None
    customer_doc = docs[1] if len(docs) > 1 else None

    issuer = None
    m = re.search(r"(?:Nome/Raz[ãa]o Social|Raz[ãa]o Social)\s*:?\s*\n?\s*([A-ZÀ-Ü0-9 .&/-]{6,80})",
                  t)
    if m:
        issuer = m.group(1).strip() or None
    customer = None
    m = re.search(r"TOMADOR[^\n]*\n(?:[^\n]*\n){0,3}?\s*([A-Za-zà-ú][A-Za-zà-ú .]{3,40})", t)
    if m:
        customer = m.group(1).strip() or None

    # valores: o maior tende a ser o total/serviço
    amts = [parse_amount(a) for a in _AMT_RE.findall(t)]
    amts = [a for a in amts if a > 0]
    valor_servico = max(amts) if amts else None

    base = aliquota = valor_issqn = None
    # "Base Cálculo ISSQN (R$)" (Sorocaba) e "Base de Cálculo (ISS)" (Recife)
    m = re.search(r"Base\s+(?:de\s+)?C[áa]lculo\s*(?:ISSQN|\(?ISS\)?)[^\n]*", t, re.I)
    if m:
        seg = t[m.start(): m.start() + 120]
        am = _AMT_RE.findall(seg)
        if am:
            base = parse_amount(am[0])
    m = re.search(r"Al[íi]quota[^\n]*", t, re.I)
    if m:
        seg = t[m.start(): m.start() + 40]
        am = _AMT_RE.findall(seg)
        if am:
            aliquota = parse_amount(am[0])
    # "Valor ISSQN" e "Valor do ISS"
    m = re.search(r"Valor\s+(?:do\s+)?ISS(?:QN)?\b[^\n]*", t, re.I)
    if m:
        seg = t[m.start(): m.start() + 40]
        am = _AMT_RE.findall(seg)
        if am:
            valor_issqn = parse_amount(am[0])

    # município da incidência + código do serviço (tabela de alíquotas)
    # "Município da Incidência do ISSQN\nSOROCABA - SP" e "Município de Incidência\nRecife - PE"
    city = None
    m = re.search(r"Munic[íi]pio\s+(?:da|de)\s+Incid[êe]ncia(?:\s+do\s+ISSQN)?"
                  r"\s*\n\s*([A-ZÀ-Ü][A-Za-zÀ-Ü ]*?)\s*-\s*[A-Z]{2}\b", t)
    if m:
        city = m.group(1).strip()
    service_code = None
    m = re.search(r"Servi[çc]o:\s*(\d{2}\.\d{2})", t)
    if not m:
        # "Código da Atividade" só vale em formato LC 116 (XX.YY)
        m = re.search(r"C[óo]digo\s+da\s+Atividade\s*:?\s*\n?\s*(\d{2}\.\d{2})", t)
    if m:
        service_code = m.group(1)

    return _build(
        source,
        number=number, series=series, issue_date=issue_date,
        competency=competency, code=code,
        issuer=issuer, issuer_doc=issuer_doc,
        customer=customer, customer_doc=customer_doc,
        valor_servico=valor_servico, base=base,
        aliquota=aliquota, valor_issqn=valor_issqn,
        valor_liquido=None,
        city=city, service_code=service_code,
    )
