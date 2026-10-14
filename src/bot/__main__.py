"""Ponto de entrada: ``python3 -m src.bot``."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from src.bot.telegram_bot import run_bot  # noqa: E402
from src.config import load_config  # noqa: E402


def main() -> None:
    cfg = load_config()
    run_bot(cfg)


if __name__ == "__main__":
    main()
