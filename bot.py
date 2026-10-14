"""Bot conversacional de conciliação no Telegram.

O cliente manda mensagem/arquivo no Telegram, a IA faz as perguntas certas
e a conciliação é devolvida no chat (resumo + CSV + Markdown).

Uso:
    export TELEGRAM_BOT_TOKEN=123:abc
    python bot.py

O Ollama precisa estar rodando (ollama serve) para o cérebro usar o LLM;
senão o bot usa o modo template (perguntas determinísticas).
"""
from __future__ import annotations

from src.bot import run_bot
from src.config import load_config


def main():
    cfg = load_config()
    run_bot(cfg)


if __name__ == "__main__":
    main()
