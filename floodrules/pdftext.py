"""PDF to per-page text, plus the page-tagged form the model reads."""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader


@dataclass(frozen=True)
class Document:
    name: str
    pages: list[str]          # pages[0] is page `first_page`
    first_page: int = 1
    total_pages: int | None = None   # set on excerpts made by chunks()

    @property
    def last_page(self) -> int:
        return self.first_page + len(self.pages) - 1

    def tagged(self) -> str:
        """Text with explicit page markers (real page numbers) so the model can cite pages."""
        body = "\n\n".join(f'<page number="{i}">\n{t.strip()}\n</page>' for i, t in enumerate(self.pages, self.first_page))
        if self.total_pages and self.total_pages > len(self.pages):
            body = f'<excerpt pages="{self.first_page}-{self.last_page}" of="{self.total_pages}"/>\n\n' + body
        return body

    def chunks(self, max_chars: int) -> list["Document"]:
        """Consecutive page groups of at most max_chars each (a single oversized page stays on its own).
        A document that already fits comes back unchanged, as one chunk."""
        if sum(len(p) for p in self.pages) <= max_chars:
            return [self]
        groups: list[tuple[int, list[str]]] = []
        start, size = 0, 0
        cur: list[str] = []
        for i, page in enumerate(self.pages):
            if cur and size + len(page) > max_chars:
                groups.append((start, cur))
                start, cur, size = i, [], 0
            cur.append(page)
            size += len(page)
        groups.append((start, cur))
        n = len(self.pages)
        return [Document(name=self.name, pages=g, first_page=self.first_page + s, total_pages=n) for s, g in groups]


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
