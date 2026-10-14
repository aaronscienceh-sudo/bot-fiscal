"""OCR de imagens (fotos de faturas) via Tesseract.

Extrai o texto de uma imagem para que a camada de IA consiga ler os dados da
fatura. Usa o binário `tesseract` via subprocess — sem dependências Python
pesadas (nada de torch/paddle), o que importa nesta máquina com 3,4 GB de RAM.

Requisito:  sudo apt-get install tesseract-ocr tesseract-ocr-por
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path


class OCRError(RuntimeError):
    """Falha de OCR (binário ausente, imagem ilegível, timeout…)."""


def tesseract_available(tesseract_bin: str | None = None) -> bool:
    return shutil.which(tesseract_bin or "tesseract") is not None


def ocr_image(image_path: str | Path, *,
              tesseract_bin: str | None = None,
              languages: str = "por+eng",
              psm: int = 3,
              timeout: float = 120.0) -> str:
    """Roda OCR em uma imagem e devolve o texto extraído.

    languages: pacotes de idioma do tesseract (ex.: 'por+eng').
    psm (page segmentation mode): 3 = página automática; 6 = bloco único
         (bom para documentos limpos); 11 = sparse text (pouco texto).
    """
    image_path = Path(image_path)
    bin_ = tesseract_bin or "tesseract"
    if shutil.which(bin_) is None:
        raise OCRError(
            f"Tesseract não encontrado ({bin_}). Instale com: "
            "sudo apt-get install tesseract-ocr tesseract-ocr-por"
        )

    with tempfile.TemporaryDirectory(prefix="ocr_") as td:
        img = Path(td) / "img"
        img.write_bytes(image_path.read_bytes())
        txt = Path(td) / "out.txt"
        out_base = txt.with_suffix("")
        cmd = [bin_, str(img), str(out_base), "-l", languages, "--psm", str(psm)]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            raise OCRError(f"OCR excedeu o tempo limite ({timeout}s).") from e
        if proc.returncode != 0:
            stderr = proc.stderr.decode(errors="replace").strip()
            raise OCRError(f"Tesseract falhou: {stderr[:300]}")
        if not txt.exists():
            return ""
        return txt.read_text(encoding="utf-8", errors="replace").strip()
