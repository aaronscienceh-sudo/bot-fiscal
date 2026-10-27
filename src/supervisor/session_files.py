"""Auxílio para varrer as sessões do bot (data/sessions/<chat_id>/).

Carrega e (re)parsa os arquivos que o usuário enviou em cada sessão — o
mesmo caminho do bot (parse_file / OCR + build_audit) — para que os
verificadores auditem o que o bot realmente processou.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..llm import FallbackClient, build_audit
from ..parsing import OCRError, ParsedFile, ocr_image, parse_file, tesseract_available

PARSE_SUFFIXES = {".csv", ".tsv", ".xlsx", ".xls", ".ofx", ".qfx", ".xml",
                  ".pdf", ".txt", ".log", ".text"}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
SKIP_NAMES = {".estado_bot.json"}
MAX_FILE_BYTES = 25_000_000


@dataclass
class SessionFile:
    chat_id: str
    path: Path
    kind: str = "doc"          # doc | image | unsupported
    parsed: ParsedFile = field(default_factory=lambda: ParsedFile(path=""))
    error: str = ""
    ocr_ran: bool = False


def _load_image(sf: SessionFile, ocr_cfg: dict) -> None:
    if not (ocr_cfg.get("enabled", True)
            and tesseract_available(ocr_cfg.get("tesseract_bin"))):
        return
    try:
        text = ocr_image(
            sf.path,
            tesseract_bin=ocr_cfg.get("tesseract_bin"),
            languages=ocr_cfg.get("languages", "por+eng"),
            psm=int(ocr_cfg.get("psm", 3)),
            timeout=float(ocr_cfg.get("timeout", 120)),
        )
    except OCRError as e:
        sf.error = str(e)
        sf.ocr_ran = True
        return
    sf.ocr_ran = True
    audit = build_audit(FallbackClient(), text, source=sf.path.name)
    if audit:
        sf.parsed.audits.append(audit)


def load_sessions(sessions_dir: str | Path | None,
                  ocr_cfg: dict | None = None,
                  only_chat: str | None = None) -> list[SessionFile]:
    """Reparsa os arquivos das sessões (só a `only_chat`, se informada)."""
    if not sessions_dir:
        return []
    sessions_dir = Path(sessions_dir)
    if not sessions_dir.is_dir():
        return []
    ocr_cfg = ocr_cfg or {}
    out: list[SessionFile] = []
    for chat_dir in sorted(p for p in sessions_dir.iterdir() if p.is_dir()):
        if only_chat and chat_dir.name != only_chat:
            continue
        for f in sorted(chat_dir.iterdir()):
            if (not f.is_file() or f.name in SKIP_NAMES
                    or f.stat().st_size > MAX_FILE_BYTES):
                continue
            suf = f.suffix.lower()
            if suf in PARSE_SUFFIXES:
                out.append(SessionFile(chat_id=chat_dir.name, path=f, kind="doc",
                                       parsed=parse_file(f)))
            elif suf in IMAGE_SUFFIXES:
                sf = SessionFile(chat_id=chat_dir.name, path=f, kind="image",
                                 parsed=ParsedFile(path=str(f)))
                _load_image(sf, ocr_cfg)
                out.append(sf)
            else:
                out.append(SessionFile(chat_id=chat_dir.name, path=f,
                                       kind="unsupported"))
    return out


def get_sessions(ctx) -> list[SessionFile]:
    """Sessões carregadas (com cache no contexto da rodada)."""
    if ctx.sessions_cache is None:
        ctx.sessions_cache = load_sessions(
            ctx.sessions_dir, ctx.cfg.get("ocr") or {}, only_chat=ctx.only_chat)
    return ctx.sessions_cache


def short_path(chat_id: str, path: Path) -> str:
    return f"{chat_id}/{path.name}"
