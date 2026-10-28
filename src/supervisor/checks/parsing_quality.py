"""Verificador de qualidade de parsing das sessões do bot.

Reparsa os arquivos enviados pelos chats e sinaliza: erros de parse,
arquivos sem nenhum dado extraído, fotos sem OCR e formatos não suportados.
"""
from __future__ import annotations

from ..models import CheckContext, Finding, Severity
from ..session_files import get_sessions, short_path

_MAX_FILES_IN_EVIDENCE = 12


def _f(id_: str, severity: Severity, message: str, evidence: str,
       suggestion: str, count: int = 1, source: str = "",
       files: list[str] | None = None) -> Finding:
    return Finding(id=id_, check="parsing_quality", severity=severity,
                   message=message, evidence=evidence, suggestion=suggestion,
                   count=count, source=source, files=files or [])


def _file_list(files: list[str]) -> str:
    shown = files[:_MAX_FILES_IN_EVIDENCE]
    extra = len(files) - len(shown)
    txt = ", ".join(shown)
    return txt + (f" (+{extra} mais)" if extra else "")


def run(ctx: CheckContext) -> tuple[list[Finding], dict[str, int]]:
    findings: list[Finding] = []
    stats = {"sessions": 0, "files_total": 0, "files_ok": 0,
             "files_empty": 0, "files_error": 0, "files_unsupported": 0,
             "images": 0, "images_ocr_ran": 0, "images_ocr_skipped": 0,
             "txns": 0, "invoices": 0, "audits": 0}

    sessions = get_sessions(ctx)
    if not sessions:
        findings.append(_f(
            "parsing_quality.no_sessions", Severity.LOW,
            "Nenhuma sessão do bot encontrada para auditar",
            f"Diretório: {ctx.sessions_dir or '(não definido)'}",
            "Confira bot.sessions_dir no config.yaml ou envie arquivos "
            "para o bot para gerar sessões.",
        ))
        return findings, stats

    chats = {sf.chat_id for sf in sessions}
    stats["sessions"] = len(chats)

    errors: list[str] = []
    error_files: list[str] = []
    empties: list[str] = []
    unsupported: list[str] = []
    ocr_failed: list[str] = []

    for sf in sessions:
        stats["files_total"] += 1
        label = short_path(sf.chat_id, sf.path)
        if sf.kind == "unsupported":
            stats["files_unsupported"] += 1
            unsupported.append(label)
            continue
        if sf.kind == "image":
            stats["images"] += 1
            if sf.ocr_ran:
                stats["images_ocr_ran"] += 1
            else:
                stats["images_ocr_skipped"] += 1
            if sf.error:
                stats["files_error"] += 1
                ocr_failed.append(f"{label} ({sf.error[:80]})")
                continue
        parsed = sf.parsed
        stats["txns"] += len(parsed.transactions)
        stats["invoices"] += len(parsed.invoices)
        stats["audits"] += len(parsed.audits)
        if parsed.errors:
            stats["files_error"] += 1
            errors.append(f"{label} ({'; '.join(parsed.errors)[:100]})")
            error_files.append(sf.path.name)
        elif not (parsed.transactions or parsed.invoices or parsed.audits):
            if sf.kind == "image" and not sf.ocr_ran:
                continue  # OCR desligado/indisponível — não é falha
            stats["files_empty"] += 1
            empties.append(label)
        else:
            stats["files_ok"] += 1

    if errors:
        findings.append(_f(
            "parsing_quality.errors", Severity.MEDIUM,
            "Arquivos com erro durante o parse",
            _file_list(errors),
            "Formato malformado ou parser quebrado. Confira o arquivo citado "
            "e o parser correspondente em src/parsing/.",
            count=len(errors),
            files=error_files,
        ))
    if ocr_failed:
        findings.append(_f(
            "parsing_quality.ocr_failure", Severity.LOW,
            "Fotos de faturas com falha de OCR",
            _file_list(ocr_failed),
            "Tesseract não conseguiu ler a foto. Peça foto nítida, com o "
            "documento inteiro no quadro e boa luz.",
            count=len(ocr_failed),
        ))
    if empties:
        findings.append(_f(
            "parsing_quality.empty", Severity.LOW,
            "Arquivos sem nenhum dado extraído",
            _file_list(empties),
            "O parse rodou mas não achou transações, faturas nem impostos — "
            "se o arquivo é válido, o parser não cobre esse layout.",
            count=len(empties),
        ))
    if unsupported:
        findings.append(_f(
            "parsing_quality.unsupported", Severity.LOW,
            "Arquivos com formato não suportado nas sessões",
            _file_list(unsupported),
            "O bot ignora esses formatos; se precisar deles, estenda o "
            "dispatch em src/parsing/base.py.",
            count=len(unsupported),
        ))
    if stats["images_ocr_skipped"]:
        findings.append(_f(
            "parsing_quality.ocr_skipped", Severity.LOW,
            "Fotos não reprocessadas (OCR indisponível nesta máquina)",
            f"{stats['images_ocr_skipped']} foto(s) nas sessões",
            "Instale o Tesseract (apt-get install tesseract-ocr "
            "tesseract-ocr-por) para o supervisor auditar as fotos também.",
            count=stats["images_ocr_skipped"],
        ))

    return findings, stats
