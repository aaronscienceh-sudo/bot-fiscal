"""Parser de NFe/NFCe (Nota Fiscal de Produtos).

Dois caminhos:

  * **XML** (``parse_nfe_xml``): lê o arquivo XML da NFe de forma
    *namespace-agnóstica* (ignora o prefixo ``ns0:`` etc.). Extrai número,
    série, chave de acesso, emitente/destinatário (nome + CNPJ/CPF), data de
    emissão, valor total e os impostos (ICMS, IPI, PIS, COFINS, ISSQN).

  * **DANFE / PDF** (``parse_nfe_pdf``): lê o texto do PDF do DANFE e extrai
    por regex os mesmos campos (nº, série, chave de acesso, emitente,
    destinatário, data, valor total).

Detecta NFe por marcadores ("NFe", "NOTA FISCAL ELETRÔNICA", "DANFE",
"CHAVE DE ACESSO", "infNFe").
"""
from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from ..domain.models import Invoice, TaxLine
from ..domain.rules import parse_amount, parse_date
from ..domain.tax import InvoiceAudit, detect_regime, verify_invoice_taxes

_NFE_MARKERS = re.compile(
    r"\bnfe\b|nf-?e|nota\s+fiscal\s+eletr[ôo]nica|danfe|chave\s+de\s+acesso|infNFe",
    re.I,
)
_KEY_RE = re.compile(r"\b(\d{44})\b")
_CNPJ_RE = re.compile(r"\b\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}\b")
_CPF_RE = re.compile(r"\b\d{3}\.\d{3}\.\d{3}-\d{2}\b")
_DOC_RE = re.compile(r"\b(\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}|\d{3}\.\d{3}\.\d{3}-\d{2})\b")
_AMT_RE = re.compile(r"\d{1,3}(?:\.\d{3})*,\d{2}|\d+,\d{2}")
_DATE_RE = re.compile(r"\b(\d{2}/\d{2}/\d{4})\b")

# Rótulos do bloco "CÁLCULO DO IMPOSTO" do DANFE (linha do rótulo, valor na
# linha seguinte) -> campo interno.
_NFE_TAX_LABELS = {
    "BASE DE CÁLC. DO ICMS": "base_icms",
    "VALOR DO ICMS": "icms",
    "BASE DE CÁLC. ICMS S.T.": "base_st",
    "VALOR DO ICMS SUBST.": "st",
    "VALOR DO PIS": "pis",
    "V. TOTAL PRODUTOS": "vprod",
    "VALOR DO FRETE": "frete",
    "VALOR DO SEGURO": "seguro",
    "DESCONTO": "desc",
    "OUTRAS DESPESAS": "outras",
    "VALOR TOTAL IPI": "ipi",
    "VALOR DA COFINS": "cofins",
    "V. TOTAL DA NOTA": "vnf",
}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _local(tag: str) -> str:
    """Remove o namespace de uma tag: '{ns}nNF' -> 'nNF'."""
    return tag.rsplit("}", 1)[-1]


def _find(el, name: str):
    """Procura descendente por nome local (namespace-agnóstico)."""
    if el is None:
        return None
    for node in el.iter():
        if _local(node.tag) == name:
            return node
    return None


def _findall(el, name: str) -> list:
    if el is None:
        return []
    return [n for n in el.iter() if _local(n.tag) == name]


def _text(el, name: str) -> str | None:
    node = _find(el, name)
    if node is not None and node.text:
        return node.text.strip()
    return None


def _dec(value: str | None):
    if value is None:
        return None
    try:
        return parse_amount(value)
    except Exception:  # noqa: BLE001
        return None


def _build(source: str, *, number, series, issue_date, access_key,
           issuer, issuer_doc, customer, customer_doc,
           amount, subtotal, tax_lines, nf_type="NFe",
           freight=None, insurance=None, other_expenses=None, st=None, discounts=None):
    if amount is None:
        amount = subtotal
    if amount is None:
        return None, None
    number = number or "NFe"
    h = hashlib.md5(f"{source}|{number}|{issuer or ''}|{amount}".encode()).hexdigest()[:8]
    tax_lines = [tl for tl in (tax_lines or []) if tl.amount is not None or tl.base is not None]
    if st is not None and not any("ST" in (tl.name or "").upper() for tl in tax_lines):
        tax_lines.append(TaxLine(name="ICMS-ST", amount=st))
    inv = Invoice(
        id=f"INV-{h}",
        number=number,
        issuer=issuer or "Desconhecido",
        issue_date=issue_date,
        amount=amount,
        description=(f"{nf_type} {number}" + (f" série {series}" if series else "")),
        nf_type=nf_type,
        series=series,
        access_key=access_key,
        issuer_doc=issuer_doc,
        customer=customer,
        customer_doc=customer_doc,
        tax_lines=tax_lines,
        source_file=source,
    )
    audit = InvoiceAudit(
        source=source,
        issuer=inv.issuer,
        number=number,
        issue_date=issue_date,
        amount=amount,
        subtotal=subtotal if subtotal is not None else amount,
        tax_lines=tax_lines,
        nf_type=nf_type,
        series=series,
        access_key=access_key,
        issuer_doc=issuer_doc,
        customer=customer,
        customer_doc=customer_doc,
        freight=freight,
        insurance=insurance,
        other_expenses=other_expenses,
        st=st,
        discounts=discounts,
    )
    verify_invoice_taxes(audit)
    return inv, audit


# --------------------------------------------------------------------------- #
# NFe em XML
# --------------------------------------------------------------------------- #
def parse_nfe_xml(path: str | Path) -> tuple[list[Invoice], list[InvoiceAudit], list[str]]:
    """Lê o XML de uma NFe. Retorna ([], [], []) se não parecer NFe."""
    path = Path(path)
    source = path.name
    try:
        tree = ET.parse(str(path))
    except Exception as e:  # noqa: BLE001
        return [], [], [f"Falha ao abrir XML {path.name}: {e}"]
    root = tree.getroot()

    inf = _find(root, "infNFe")
    if inf is None:
        # pode ser uma pasta/lançamento com várias NFe; tenta a primeira
        if not _NFE_MARKERS.search(root.tag) and not _find(root, "ide"):
            return [], [], []

    ide = _find(inf, "ide") if inf is not None else _find(root, "ide")
    if ide is None:
        return [], [], []

    number = _text(ide, "nNF")
    series = _text(ide, "serie")
    dh_emi = _text(ide, "dhEmi")
    issue_date = None
    if dh_emi:
        try:
            issue_date = datetime.strptime(dh_emi[:19], "%Y-%m-%dT%H:%M:%S").date()
        except ValueError:
            issue_date = parse_date(dh_emi)

    emit = _find(inf, "emit") if inf is not None else _find(root, "emit")
    dest = _find(inf, "dest") if inf is not None else _find(root, "dest")
    issuer = _text(emit, "xNome") if emit is not None else None
    issuer_doc = (_text(emit, "CNPJ") or _text(emit, "CPF")) if emit is not None else None
    customer = _text(dest, "xNome") if dest is not None else None
    customer_doc = (_text(dest, "CNPJ") or _text(dest, "CPF")) if dest is not None else None

    # chave de acesso: tag <ChaveAcesso> ou <protNFe>...<chave>; senão reconstrói
    access_key = _text(root, "ChaveAcesso")
    if access_key is None:
        prot = _find(root, "protNFe")
        if prot is not None:
            access_key = _text(prot, "chNFe")
    if access_key is None:
        parts = [
            _text(ide, "cUF"), _text(ide, "ano") and _text(ide, "ano")[:2],
            _text(ide, "mesEmi"), _text(ide, "tpAmb"),
            (issuer_doc or "").replace(".", "").replace("/", "").replace("-", "") if issuer_doc else "",
            series or "", number or "", _text(ide, "serie") or "",
        ]
        # reconstrução simples: só se tiver 44 dígitos
        cand = "".join(p for p in parts if p).replace(" ", "")
        cand = re.sub(r"\D", "", cand)
        if len(cand) == 44:
            access_key = cand

    # totais
    total = _find(inf, "ICMSTot") if inf is not None else _find(root, "ICMSTot")
    amount = _dec(_text(total, "vNF")) if total is not None else None
    subtotal = _dec(_text(total, "vProd")) if total is not None else None
    if subtotal is None and total is not None:
        subtotal = _dec(_text(total, "vBC"))
    freight = _dec(_text(total, "vFrete")) if total is not None else None
    insurance = _dec(_text(total, "vSeg")) if total is not None else None
    other_expenses = _dec(_text(total, "vOutro")) if total is not None else None
    st = _dec(_text(total, "vST")) if total is not None else None
    discounts = _dec(_text(total, "vDesc")) if total is not None else None

    tax_lines: list[TaxLine] = []
    if total is not None:
        for name, tag in (("ICMS", "vICMS"), ("IPI", "vIPI"), ("PIS", "vPIS"),
                          ("COFINS", "vCOFINS"), ("ISSQN", "vISSQN")):
            val = _dec(_text(total, tag))
            if val is not None and val != 0:
                tax_lines.append(TaxLine(name=name, amount=val))

    inv, audit = _build(
        source, number=number, series=series, issue_date=issue_date,
        access_key=access_key, issuer=issuer, issuer_doc=issuer_doc,
        customer=customer, customer_doc=customer_doc,
        amount=amount, subtotal=subtotal, tax_lines=tax_lines, nf_type="NFe",
        freight=freight, insurance=insurance, other_expenses=other_expenses,
        st=st, discounts=discounts,
    )
    if inv is None:
        return [], [], [f"{path.name}: parece NFe, mas não consegui extrair o valor."]
    if audit is not None:
        # indSimplesNac: 1 = optante pelo Simples, 2 = não optante
        ind = (_text(inf, "indSimplesNac") if inf is not None
               else _text(root, "indSimplesNac"))
        if ind == "1":
            audit.regime = "Simples Nacional"
            audit.regime_evidence = 'o XML da NFe tem indSimplesNac="1" (optante pelo Simples Nacional)'
        elif ind == "2":
            audit.regime_evidence = ('o XML da NFe tem indSimplesNac="2" '
                                     "(não optante pelo Simples Nacional)")
    return [inv], [audit] if audit else [], []


# --------------------------------------------------------------------------- #
# NFe / DANFE em PDF (texto)
# --------------------------------------------------------------------------- #
def _pdf_text(path: str | Path) -> str:
    from pypdf import PdfReader
    reader = PdfReader(str(path))
    return "\n".join((pg.extract_text() or "") for pg in reader.pages)


def extract_nfe_from_text(text: str, source: str):
    """Extrai NFe/DANFE de texto linear (melhor esforço). Retorna (Invoice|None, audit|None)."""
    if not (text or "").strip() or not _NFE_MARKERS.search(text):
        return None, None
    t = text

    number = None
    m = re.search(r"n[ºo°][.:]?\s*:?\s*(\d[\d.]{2,})", t, re.I)
    if m:
        number = m.group(1).replace(".", "").replace(" ", "")

    series = None
    m = re.search(r"s[ée]rie\s*:?\s*(\d+)", t, re.I)
    if m:
        series = m.group(1)

    access_key = None
    # chave pode vir agrupada (4224 1147 ...) ou contínua
    grouped = re.search(r"chave\s+de\s+acesso\s*:?\s*([\d\s]{40,60})", t, re.I)
    if grouped:
        digits = re.sub(r"\D", "", grouped.group(1))
        if len(digits) == 44:
            access_key = digits
    if access_key is None:
        m = _KEY_RE.search(t)
        if m:
            access_key = m.group(1)

    issue_date = None
    m = re.search(r"(?:emiss[ãa]o|data\s+da\s+emiss[ãa]o)\s*:?\s*(\d{2}/\d{2}/\d{4})", t, re.I)
    if m:
        issue_date = parse_date(m.group(1))
    if issue_date is None:
        m = _DATE_RE.search(t)
        if m:
            issue_date = parse_date(m.group(1))

    amount = None
    m = re.search(r"valor\s+total\s*:?\s*(?:R\$\s*)?(\d{1,3}(?:\.\d{3})*,\d{2}|\d+,\d{2})", t, re.I)
    if m:
        amount = parse_amount(m.group(1))

    # --- bloco "CÁLCULO DO IMPOSTO" do DANFE: rótulo na linha, valor na seg. #
    subtotal = None
    tax_lines: list[TaxLine] = []
    freight = insurance = other_expenses = st = discounts = None
    m = re.search(r"C[ÁA]LCULO\s+DO\s+IMPOSTO(.*?)(?:TRANSPORTADOR|DADOS\s+DOS\s+PRODUTOS)",
                  t, re.S | re.I)
    if m:
        block = m.group(1).splitlines()
        vals: dict[str, Decimal] = {}
        for i, ln in enumerate(block):
            key = _NFE_TAX_LABELS.get(ln.strip().upper())
            if key and i + 1 < len(block):
                nxt = block[i + 1].strip()
                if _AMT_RE.fullmatch(nxt):
                    vals[key] = parse_amount(nxt)
        subtotal = vals.get("vprod")
        freight = vals.get("frete")
        insurance = vals.get("seguro")
        other_expenses = vals.get("outras")
        st = vals.get("st")
        discounts = vals.get("desc")
        ipi = vals.get("ipi")
        if vals.get("vnf") is not None:
            amount = vals["vnf"]
        # alíquotas: últimos 2 valores da linha do produto (ALÍQ. ICMS / ALÍQ. IPI)
        alq_icms = None
        prod = re.search(r"DADOS\s+DOS\s+PRODUTOS\s*/\s*SERVI[ÇC]OS(.*?)DADOS\s+ADICIONAIS",
                         t, re.S | re.I)
        if prod:
            for pline in prod.group(1).splitlines():
                amts = _AMT_RE.findall(pline)
                if len(amts) >= 7:
                    alq_icms = parse_amount(amts[-2])
                    break
        if vals.get("icms") is not None:
            tax_lines.append(TaxLine(name="ICMS", base=vals.get("base_icms"),
                                     rate=alq_icms, amount=vals["icms"]))
        if ipi:
            tax_lines.append(TaxLine(name="IPI", amount=ipi))
        if vals.get("pis"):
            tax_lines.append(TaxLine(name="PIS", amount=vals["pis"]))
        if vals.get("cofins"):
            tax_lines.append(TaxLine(name="COFINS", amount=vals["cofins"]))
        if st is not None:
            tax_lines.append(TaxLine(name="ICMS-ST", base=vals.get("base_st"), amount=st))
        # DANFE cujos rótulos vêm todos na mesma linha (texto linear do pypdf:
        # "BASE DE CÁLCULO DO ICMS V ALOR DO ICMS ...") não casam rótulo/valor:
        # o total é o MAIOR valor do bloco (vNF ≥ qualquer componente)
        if amount is None:
            nums = [parse_amount(x) for x in _AMT_RE.findall(m.group(0))]
            nums = [n for n in nums if n is not None]
            if nums:
                amount = max(nums)

    # emitente: "RECEBEMOS DE <nome> OS PRODUTOS" (topo do DANFE)
    issuer = None
    m = re.search(r"recebemos\s+de\s+(.+?)\s+os\s+produtos", t, re.I)
    if m:
        issuer = m.group(1).strip()
    if issuer is None:
        m = re.search(r"identifica[çc][ãa]o\s+do\s+emitente\s*\n\s*([^\n]+)", t, re.I)
        if m:
            issuer = m.group(1).strip()

    # destinatário: "DESTINATÁRIO: <nome>" ou bloco "NOME / RAZÃO SOCIAL"
    customer = None
    m = re.search(r"destinat[áa]rio\s*:?\s*([A-Za-zÀ-ú][^\n-]{3,60})", t, re.I)
    if m:
        customer = m.group(1).strip()

    docs = _DOC_RE.findall(t)
    issuer_doc = docs[0] if docs else None
    customer_doc = docs[1] if len(docs) > 1 else None

    inv, audit = _build(
        source, number=number, series=series, issue_date=issue_date,
        access_key=access_key, issuer=issuer, issuer_doc=issuer_doc,
        customer=customer, customer_doc=customer_doc,
        amount=amount, subtotal=subtotal, tax_lines=tax_lines, nf_type="NFe",
        freight=freight, insurance=insurance, other_expenses=other_expenses,
        st=st, discounts=discounts,
    )
    if audit is not None:
        audit.regime, audit.regime_evidence = detect_regime(t)
    return inv, audit


def parse_nfe_pdf(path: str | Path) -> tuple[list[Invoice], list[InvoiceAudit], list[str]]:
    """Tenta ler o PDF como NFe/DANFE. Retorna ([], [], []) se não parecer NFe."""
    path = Path(path)
    source = path.name
    try:
        text = _pdf_text(path)
    except Exception as e:  # noqa: BLE001
        return [], [], [f"Falha ao abrir PDF {path.name}: {e}"]

    if not _NFE_MARKERS.search(text):
        return [], [], []

    inv, audit = extract_nfe_from_text(text, source)
    if inv is None:
        return [], [], [f"{path.name}: parece NFe/DANFE, mas não consegui extrair o valor."]
    return [inv], [audit] if audit else [], []
