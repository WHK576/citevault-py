"""PDF 文本抽取（可选依赖）。"""

from __future__ import annotations

import io
import shutil
import subprocess

from .extract import normalize_text


class PdfError(Exception):
    """PDF 解析失败或缺少依赖。"""


def _load_pypdf():
    try:
        from pypdf import PdfReader
    except ImportError:
        return None
    return PdfReader


def extract_pdf_text(raw: bytes) -> str:
    """优先用 pypdf，其次用系统 pdftotext；都没有时报可操作的错误。"""
    if not raw:
        raise PdfError("PDF 内容为空")

    reader_class = _load_pypdf()
    if reader_class is not None:
        try:
            reader = reader_class(io.BytesIO(raw))
            texts = [page.extract_text() or "" for page in reader.pages]
        except Exception as exc:
            raise PdfError(f"PDF 解析失败: {exc}") from exc
        return normalize_text("\n".join(texts))

    if shutil.which("pdftotext"):
        result = subprocess.run(
            ["pdftotext", "-", "-"],
            input=raw,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            message = result.stderr.decode("utf-8", errors="replace").strip()
            raise PdfError(f"pdftotext 失败: {message}")
        return normalize_text(result.stdout.decode("utf-8", errors="replace"))

    raise PdfError("PDF 需要安装 pypdf：pip install citevault[pdf]")
