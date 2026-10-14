"""Parser de extrato bancário OFX/QFX (e XML simples).

Implementação leve via regex — sem dependências externas.
Lê blocos <STMTTRN> com TRNTYPE, DATEPOSTED, TRNAMT, NAME, REFNUM/FITID.
"""
from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, timezone
from pathlib import Path

from ..domain.models import Transaction
from ..domain.rules import extract_reference, parse_amount

_TRN_BLOCK = re.compile(r"<STMTTRN>(.*?)</STMTTRN>", re.S | re.I)


def _ofx_date(s: str) -> date | None:
    s = s.strip()
    if not s:
        return None
    # 20260815  ou  20260815120000  ou  20260815120000-03'00'
    m = re.match(r"(\d{4})(\d{2})(\d{2})", s)
    if not m:
        return None
    y, mo, d = map(int, m.groups())
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def _field(block: str, name: str) -> str:
    # estilo XML: <NAME>valor</NAME>
    m = re.search(r"<" + name + r">\s*([^<]*?)\s*</" + name + r">", block, re.I)
    if m:
        return m.group(1).strip()
    # estilo SGML (padrão OFX): <NAME>valor  (até a próxima tag ou fim de linha)
    m = re.search(r"<" + name + r">\s*([^\n<]*)", block, re.I)
    if m:
        return m.group(1).strip()
    return ""


def parse_ofx(path: str | Path) -> tuple[list[Transaction], list[str]]:
    path = Path(path)
    source = path.name
    text = path.read_text(encoding="utf-8", errors="replace")
    txns: list[Transaction] = []
    errors: list[str] = []

    blocks = _TRN_BLOCK.findall(text)
    if not blocks:
        errors.append("Nenhum bloco <STMTTRN> encontrado no arquivo OFX.")
        return txns, errors

    for i, block in enumerate(blocks, start=1):
        trntype = _field(block, "TRNTYPE").upper()
        d = (_ofx_date(_field(block, "DTPOSTED"))
             or _ofx_date(_field(block, "DATEPOSTED"))
             or _ofx_date(_field(block, "DTUSER")))
        amt_raw = _field(block, "TRNAMT")
        name = _field(block, "NAME")
        memo = _field(block, "MEMO")
        refnum = _field(block, "REFNUM") or _field(block, "FITID")
        description = " ".join(x for x in (name, memo) if x).strip()

        if d is None or not amt_raw:
            continue
        amount = parse_amount(amt_raw)
        # OFX já traz o sinal; garante que débito (saída) fique negativo
        if trntype in ("DEBIT", "CREDIT") and amount > 0 and trntype == "DEBIT":
            amount = -amount
        h = hashlib.md5(f"{source}|{d}|{amount}|{description}|{i}".encode()).hexdigest()[:8]
        txns.append(Transaction(
            id=f"TXN-{h}",
            date=d,
            amount=amount,
            description=description,
            reference=refnum or extract_reference(description),
            counterparty=name or None,
            source_file=source,
        ))

    return txns, errors
