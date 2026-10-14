"""Dispatch de arquivos: detecta o formato pelo sufixo e delega ao parser.

Ordem para PDF:
  1. NFS-e (Nota Fiscal de Serviços) — layout de formulário por posição.
  2. NFe / DANFE (Nota Fiscal de Produtos) — por texto.
  3. Faturas genéricas / extrato — heurísticas.

Ordem para XML:
  1. NFe (namespace-agnóstico).
  2. OFX/QFX (extrato bancário).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..domain.models import Invoice, Transaction
from ..domain.tax import InvoiceAudit


@dataclass
class ParsedFile:
    path: str
    transactions: list[Transaction] = field(default_factory=list)
    invoices: list[Invoice] = field(default_factory=list)
    audits: list[InvoiceAudit] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _parse_pdf(path: Path):
    """Tenta NFS-e, depois NFe/DANFE, depois faturas genéricas."""
    from .nfs_parser import parse_nfs_pdf
    from .nfe_parser import parse_nfe_pdf
    from .pdf_parser import extract_fatura_audit, parse_pdf, pdf_to_text

    invs, audits, err = parse_nfs_pdf(path)
    if invs:
        return [], invs, audits, err
    invs, audits, err = parse_nfe_pdf(path)
    if invs:
        return [], invs, audits, err
    txns, invs, err = parse_pdf(path)
    # fatura genérica também gera parecer: lê os impostos destacados no
    # corpo (bloco de tributos, retenções) em vez de entregar Invoice sem
    # cálculo fiscal
    audits = []
    try:
        audit = extract_fatura_audit(pdf_to_text(path), path.name)
    except Exception:  # noqa: BLE001 - PDF ilegível não derruba o parse
        audit = None
    if audit is not None:
        audits = [audit]
    return txns, invs, audits, err


def _parse_xml(path: Path):
    """Tenta NFe, depois OFX/QFX."""
    from .nfe_parser import parse_nfe_xml
    from .ofx_parser import parse_ofx

    invs, audits, err = parse_nfe_xml(path)
    if invs:
        return [], invs, audits, err
    txns, err = parse_ofx(path)
    return txns, [], [], err


def _parse_text(path: Path):
    """Texto puro (.txt/.log): tenta NFS-e e NFe por regex."""
    from .nfs_parser import extract_nfs_from_text
    from .nfe_parser import extract_nfe_from_text

    text = path.read_text(encoding="utf-8", errors="replace")
    inv, audit = extract_nfs_from_text(text, path.name)
    if inv is None:
        inv, audit = extract_nfe_from_text(text, path.name)
    if inv is None:
        return [], [], [f"{path.name}: não reconheci fatura/nota no texto."]
    audits = [audit] if audit else []
    return [], [inv], audits, []


def parse_file(path: str | Path) -> ParsedFile:
    path = Path(path)
    suffix = path.suffix.lower()
    result = ParsedFile(path=str(path))

    try:
        if suffix in (".csv", ".tsv"):
            from .csv_parser import parse_csv
            txns, invs, err = parse_csv(path, delimiter=None)
            audits = []
        elif suffix in (".xlsx", ".xls"):
            from .csv_parser import parse_excel
            txns, invs, err = parse_excel(path)
            audits = []
        elif suffix in (".ofx", ".qfx"):
            from .ofx_parser import parse_ofx
            txns, err = parse_ofx(path)
            invs, audits = [], []
        elif suffix == ".xml":
            txns, invs, audits, err = _parse_xml(path)
        elif suffix == ".pdf":
            txns, invs, audits, err = _parse_pdf(path)
        elif suffix in (".txt", ".log", ".text"):
            txns, invs, audits, err = _parse_text(path)
        else:
            result.errors.append(f"Formato não suportado: {suffix}")
            return result
    except Exception as e:  # noqa: BLE001 - não queremos derrubar o pipeline
        result.errors.append(f"Erro ao processar {path.name}: {e}")
        return result

    result.transactions = txns
    result.invoices = invs
    result.audits = audits
    result.errors.extend(err)
    return result


def parse_directory(directory: str | Path) -> list[ParsedFile]:
    """Processa todos os arquivos suportados de um diretório."""
    directory = Path(directory)
    results = []
    supported = {".csv", ".tsv", ".xlsx", ".xls", ".ofx", ".qfx", ".xml", ".pdf",
                 ".txt", ".log", ".text"}
    for f in sorted(directory.iterdir()):
        if f.is_file() and f.suffix.lower() in supported:
            results.append(parse_file(f))
    return results
