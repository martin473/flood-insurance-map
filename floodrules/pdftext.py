"""PDF to per-page text, plus the page-tagged form the model reads."""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader


@dataclass(frozen=True)
class Document:
    name: str
    pages: list[str]  # index 0 is page 1

    def tagged(self) -> str:
        """Text with explicit page markers so the model can cite page numbers."""
        return "\n\n".join(f'<page number="{i}">\n{t.strip()}\n</page>' for i, t in enumerate(self.pages, 1))


def read_pdf(source: str | Path | bytes, name: str | None = None) -> Document:
    if isinstance(source, (bytes, bytearray)):
        reader = PdfReader(io.BytesIO(source))
        name = name or "upload.pdf"
    else:
        path = Path(source)
        reader = PdfReader(str(path))
        name = name or path.name
    pages = [(p.extract_text() or "") for p in reader.pages]
    if not any(t.strip() for t in pages):
        raise ValueError(f"{name}: no extractable text. The PDF is probably scanned and needs OCR first.")
    return Document(name=name, pages=pages)
