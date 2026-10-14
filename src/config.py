"""Carrega a configuração (config.yaml) e sobrepõe segredos via env."""
from __future__ import annotations

import os
from pathlib import Path

import yaml

DEFAULTS = {
    "llm": {
        "enabled": True,
        "base_url": "http://localhost:11434",
        "model": "llama3.2:1b-instruct-q4_K_M",
        "timeout": 120,
        "num_ctx": 2048,
    },
    "bot": {
        "mode": "template",      # template | auto | llm
        "sessions_dir": "data/sessions",
        "history_turns": 6,      # nº de turnos mantidos na conversa do LLM
        "auto_run": True,        # roda sozinho quando extrato+faturas chegam
        "pipeline_llm": False,   # true = LLM na conciliação (lento)
        "extract_with_llm": False,  # true = foto passa pelo LLM; false = regex
        "allowed_chat_ids": [],  # vazio = aceita qualquer chat; senão, só estes
    },
    "matching": {
        "amount_tolerance": "0.01",
        "date_window_days": 15,
        "name_threshold": 0.82,
        "fuzzy_confidence_threshold": 0.55,
        "late_tolerance_days": 0,
        "low_confidence_threshold": 0.5,
    },
    "ocr": {
        "enabled": True,
        "tesseract_bin": "tesseract",
        "languages": "por+eng",
        "psm": 3,
        "timeout": 120,
    },
    "tax": {
        "tolerance": "0.05",   # diferença máxima (R$) p/ considerar o cálculo do imposto correto
    },
    "email": {
        "enabled": False,   # caminho 2 do bot: "buscar no email" (IMAP)
        "host": "imap.gmail.com",
        "port": 993,
        "user": "",         # ou env EMAIL_IMAP_USER
        "password": "",     # env EMAIL_IMAP_PASSWORD (Gmail: senha de app)
        "folder": "INBOX",
        "days_back": 31,    # sem período dito, busca os últimos N dias
        "subject_keywords": ["nota", "nf-", "nfe", "nfse", "fatura", "invoice"],
        "max_files": 50,
    },
    "paths": {
        "inbox": "data/inbox",
        "reports": "data/reports",
    },
    "telegram": {
        "enabled": True,
        "token": "",
        "chat_id": "",
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _load_env_file(path: str | Path | None = None) -> None:
    """Carrega variáveis de um arquivo .env (sem dependências externas).

    Não sobrescreve variáveis já definidas no ambiente real.
    """
    if path is None:
        path = Path(__file__).resolve().parent.parent / ".env"
    path = Path(path)
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def load_config(path: str | Path | None = None) -> dict:
    _load_env_file()
    if path is None:
        path = Path(__file__).resolve().parent.parent / "config.yaml"
    cfg = DEFAULTS
    if path:
        path = Path(path)
        if path.exists():
            with path.open(encoding="utf-8") as f:
                user_cfg = yaml.safe_load(f) or {}
            cfg = _deep_merge(DEFAULTS, user_cfg)

    # segredos por variável de ambiente (nunca commitar token no yaml)
    tg = cfg["telegram"]
    tg["token"] = os.environ.get("TELEGRAM_BOT_TOKEN", tg.get("token", ""))
    tg["chat_id"] = os.environ.get("TELEGRAM_CHAT_ID", tg.get("chat_id", ""))
    cfg["llm"]["base_url"] = os.environ.get("OLLAMA_BASE_URL", cfg["llm"]["base_url"])
    cfg["llm"]["model"] = os.environ.get("OLLAMA_MODEL", cfg["llm"]["model"])
    em = cfg["email"]
    em["user"] = os.environ.get("EMAIL_IMAP_USER", em.get("user", ""))
    em["password"] = os.environ.get("EMAIL_IMAP_PASSWORD", em.get("password", ""))
    return cfg
