"""Parser de PDF (melhor esforço).

Extrai texto com pypdf e aplica heurísticas para encontrar:
  * FATURAS  -> blocos com número + valor (+ emissor/vencimento)
  * TRANSAÇÕES -> linhas com data + valor (quando parece extrato bancário)

PDFs reais variam muito; para layouts específicos, adicione um template
aqui. As heurísticas abaixo cobrem o formato de fatura mais comum.
"""
from __future__ import annotations

import hashlib
import re
from decimal import Decimal
from pathlib import Path

from ..domain.models import Invoice, TaxLine, Transaction
from ..domain.rules import extract_reference, parse_amount, parse_date
from ..domain.tax import InvoiceAudit, detect_regime, verify_invoice_taxes

_AMOUNT_RE = re.compile(r"(?:R\$\s*)?(\d{1,3}(?:\.\d{3})+(?:,\d{2})?|\d+(?:,\d{2})|\d+\.\d{2})")
_DATE_RE = re.compile(r"(\d{2}/\d{2}/\d{4}|\d{4}-\d{2}-\d{2}|\d{2}/\d{2}/\d{2})")
_NUM_RE = re.compile(r"(?:n[ºo°]|num(?:ero)?|fatura|nota|nfe|nf)\s*:?\s*(\d{3,})", re.I)
_STATEMENT_HINTS = ("extrato", "saldo", "movimento", "lancamento", "histórico", "historico")


def pdf_to_text(path: str | Path) -> str:
    from pypdf import PdfReader
    reader = PdfReader(str(path))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def _find_amount(block: str, keywords: list[str]):
    for kw in keywords:
        for line in block.splitlines():
            if kw in line.lower():
                m = _AMOUNT_RE.search(line.replace("R$", ""))
                if m:
                    return parse_amount(m.group(1))
    amounts = [parse_amount(m.group(1)) for line in block.splitlines()
               for m in _AMOUNT_RE.finditer(line.replace("R$", ""))]
    amounts = [a for a in amounts if a > 0]
    return max(amounts) if amounts else None


def _find_date_near(block: str, keywords: list[str]):
    for kw in keywords:
        for line in block.splitlines():
            if kw in line.lower():
                m = _DATE_RE.search(line)
                if m:
                    return parse_date(m.group(1))
    return None


def _find_issuer(block: str, number_line: str):
    for kw in ("emissor", "fornecedor", "empresa", "razao social", "razão social", "cliente", "cnpj"):
        for line in block.splitlines():
            if kw in line.lower():
                # pega o texto depois do ':'
                if ":" in line:
                    return line.split(":", 1)[1].strip()
                return line.strip()
    return None


def extract_invoices_from_text(text: str, source: str) -> list[Invoice]:
    invoices: list[Invoice] = []
    lines = [l.strip() for l in text.splitlines()]
    seen: set[str] = set()
    for i, line in enumerate(lines):
        m = _NUM_RE.search(line)
        if not m:
            continue
        number = m.group(1)
        if number in seen:
            continue
        block = "\n".join(lines[i:i + 8])
        amount = _find_amount(block, ["valor", "total", "a pagar", "deve", "amount", "valor_total"])
        if amount is None:
            continue
        seen.add(number)
        h = hashlib.md5(f"{source}|{number}".encode()).hexdigest()[:8]
        invoices.append(Invoice(
            id=f"INV-{h}",
            number=number,
            issuer=_find_issuer(block, line) or "Desconhecido",
            issue_date=_find_date_near(block, ["emissao", "emissão", "data_emissao", "issue"]),
            due_date=_find_date_near(block, ["vencimento", "venc", "due", "pagamento", "pagto"]),
            amount=amount,
            description=line,
            source_file=source,
        ))
    return invoices


def extract_transactions_from_text(text: str, source: str) -> list[Transaction]:
    txns: list[Transaction] = []
    seen: set[str] = set()
    for line in text.splitlines():
        dm = _DATE_RE.search(line)
        if not dm:
            continue
        am = _AMOUNT_RE.search(line.replace("R$", ""))
        if not am:
            continue
        d = parse_date(dm.group(1))
        amount = parse_amount(am.group(1))
        if d is None or amount == 0:
            continue
        key = f"{d}|{amount}|{line[:40]}"
        if key in seen:
            continue
        seen.add(key)
        h = hashlib.md5(key.encode()).hexdigest()[:8]
        txns.append(Transaction(
            id=f"TXN-{h}",
            date=d,
            amount=amount,
            description=line.strip(),
            reference=extract_reference(line),
            source_file=source,
        ))
    return txns


def parse_pdf(path: str | Path) -> tuple[list[Transaction], list[Invoice], list[str]]:
    path = Path(path)
    source = path.name
    errors: list[str] = []
    try:
        text = pdf_to_text(path)
    except Exception as e:  # noqa: BLE001
        return [], [], [f"Falha ao ler PDF {path.name}: {e}"]

    if not text.strip():
        return [], [], [f"PDF {path.name} sem texto extraível (pode ser imagem/OCR)."]

    invoices = extract_invoices_from_text(text, source)
    txns: list[Transaction] = []
    if any(h in text.lower() for h in _STATEMENT_HINTS):
        txns = extract_transactions_from_text(text, source)

    if not invoices and not txns:
        errors.append(f"PDF {path.name}: nada reconhecido pelas heurísticas. Considere um template.")
    return txns, invoices, errors


# --------------------------------------------------------------------------- #
# Parecer fiscal de fatura genérica (PDF sem parser de NF-e/NFS-e próprio)
# --------------------------------------------------------------------------- #
_AMT_PT = r"[\d]{1,3}(?:\.\d{3})*,\d{2}"

_FATURA_HINT = re.compile(r"fatura|nota\s+fiscal|nfs-?e|danfe|chave\s+de\s+acesso",
                          re.I)


def _fatura_tax_lines(text: str) -> list[TaxLine]:
    """Impostos destacados no corpo da fatura.

    Casa o bloco de tributos de fatura com NF embutida (ex.: "INFORMAÇÕES
    DOS TRIBUTOS RESERVADO AO FISCO" da fatura de telecom):

        BASE DE CÁLCULO 70,00 TRIBUTO VALOR
        VALOR ICMS 12,60 PIS 0,44
        VALOR FCP 0,00 COFINS 2,00

    Pega também retenções ("IR RETIDO: R$ 15,00"). O parágrafo genérico de
    alíquotas ("18% ICMS, 0.65% PIS...") NÃO casa: ele traz a taxa ANTES do
    nome do imposto.
    """
    base = None
    m = re.search(rf"BASE\s+DE\s+C[ÁA]LCULO\s*({_AMT_PT})", text, re.I)
    if m:
        base = parse_amount(m.group(1))

    lines: list[TaxLine] = []
    seen: set[str] = set()
    pat = re.compile(
        rf"\b(?:VALOR\s+)?(ICMS|PIS|COFINS|ISSQN|ISS|IPI|CSLL|IRPJ|INSS|IR)"
        rf"\b[^\n%]{{0,30}}?({_AMT_PT})", re.I)
    for m in pat.finditer(text):
        name = m.group(1).upper()
        amount = parse_amount(m.group(2))
        if amount is None or amount == 0:
            continue
        label = f"{name} RETIDO" if re.search(r"retid", m.group(0), re.I) else name
        if label in seen:
            continue
        seen.add(label)
        rate = None
        if base and base > 0:
            # fração (0,18 = 18%) — convenção de _norm_rate/_rate_pct
            rate = (amount / base).quantize(Decimal("0.000001"))
        lines.append(TaxLine(name=label, base=base, rate=rate, amount=amount))
    return lines


def extract_fatura_audit(text: str, source: str) -> InvoiceAudit | None:
    """Parecer fiscal de fatura/nota que os parsers específicos não cobriram.

    Lê da fatura: impostos do bloco de tributos, valor total, número,
    emissão e competência — e devolve a auditoria com verificação
    (base × alíquota = valor). Retorna None se não parecer documento fiscal
    (ex.: extrato) ou se não achar valor nenhum.
    """
    if not (text or "").strip() or not _FATURA_HINT.search(text):
        return None

    invs = extract_invoices_from_text(text, source)
    main = max(invs, key=lambda i: i.amount) if invs else None
    tax_lines = _fatura_tax_lines(text)

    # valor total: NF destacada > "Total a pagar" > maior fatura do parser
    amount = None
    strong = False
    m = re.search(rf"VALOR\s+TOTAL\s+NF\s*({_AMT_PT})", text, re.I)
    if not m:
        m = re.search(rf"Total\s+a\s+pagar\s*(?:-\s*R\$)?\s*({_AMT_PT})", text, re.I)
    if m:
        amount = parse_amount(m.group(1))
        strong = True
    if amount is None and main is not None:
        amount = main.amount
    if amount is None:
        return None
    # sem estrutura de fatura (total destacado, bloco de tributos ou mês de
    # referência), não gera parecer — evita tratar extrato como documento
    # fiscal
    if not strong and not tax_lines and not re.search(
            r"M[ÊE]S\s+REFER[ÊE]NCIA", text, re.I):
        return None

    # número da nota ("Nº NFCOM 112855181", "Nº da Fatura ...")
    number = ""
    m = re.search(rf"N[ºo°]\s*(?:DA\s+FATURA|NFCOM|NF|NFS-?E|NFE)\s*[:#]?\s*(\d{{3,}})",
                  text, re.I)
    if m:
        number = m.group(1)
    elif main is not None:
        number = main.number

    issuer = main.issuer if main and main.issuer != "Desconhecido" else ""
    if not issuer:
        m = re.search(
            r"([A-ZÀ-Ü][A-Za-zÀ-ÿ0-9]+(?: [A-Za-zÀ-ÿ0-9]+){0,4} "
            r"(?:S\.A\.|S\.A|Ltda|LTDA|EIRELI)\b)", text)
        if m:
            issuer = m.group(1).strip()
    issuer = re.sub(r"\s+", " ", issuer or "").strip()

    issue_date = None
    m = re.search(r"DATA\s+DE\s+EMISS[ÃA]O\s*:?\s*(\d{2}/\d{2}/\d{4})", text, re.I)
    if m:
        issue_date = parse_date(m.group(1))
    elif main is not None:
        issue_date = main.issue_date

    due_date = None
    m = re.search(rf"VENCIMENTO[^\n]*\n\s*(\d{{2}}/\d{{2}}/\d{{4}})", text, re.I)
    if m:
        due_date = parse_date(m.group(1))
    elif main is not None:
        due_date = main.due_date

    competency = None
    m = re.search(r"M[ÊE]S\s+REFER[ÊE]NCIA\s*:?\s*(\d{2}/\d{4})", text, re.I)
    if m:
        competency = m.group(1)

    if re.search(r"nfs-?e|nota\s+fiscal\s+de\s+servi", text, re.I):
        nf_type = "NFS-e"
    elif re.search(r"\bnfe\b|danfe", text, re.I):
        nf_type = "NFe"
    else:
        nf_type = "Fatura"

    audit = InvoiceAudit(
        source=source,
        issuer=issuer or "Desconhecido",
        number=number,
        issue_date=issue_date,
        due_date=due_date,
        amount=amount,
        tax_lines=tax_lines,
        nf_type=nf_type,
        competency=competency,
    )
    audit.regime, audit.regime_evidence = detect_regime(text)
    verify_invoice_taxes(audit)
    return audit
