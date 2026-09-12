"""ETL – document parsing.

Extracts plain text from common enterprise document formats. Binary parsing
libraries (``unstructured``, ``pypdf``) are optional; we always support
txt/md/csv and degrade gracefully for others.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any


def parse_file(path: str | Path) -> str:
    p = Path(path)
    text = p.read_text(encoding="utf-8", errors="ignore")
    if p.suffix.lower() in {".txt", ".md", ".csv", ".json", ".log"}:
        return text
    if p.suffix.lower() == ".pdf":
        try:
            from pypdf import PdfReader
            return "\n".join(page.extract_text() or "" for page in PdfReader(str(p)).pages)
        except Exception:
            return text
    # fallback
    return text


def parse_text(text: str) -> str:
    return text
