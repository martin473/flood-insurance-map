"""Check every extracted rule against the document it came from.

A rule survives only if its evidence quote appears in the document text. Matching
ignores case, whitespace, hyphenation at line breaks and curly quotes, because PDF
text extraction mangles those. If the quote is found on a different page than cited,
the rule is kept and its page corrected.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from .pdftext import Document
from .schemas import Evidence, Extraction, Product

MIN_QUOTE_WORDS = 3
# curly quotes, en/em dashes and non-breaking spaces -> plain ASCII
_PUNCT = str.maketrans("\u2018\u2019\u201c\u201d\u2013\u2014\u00a0", "''\"\"-- ")


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = text.translate(_PUNCT)
    text = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", text)   # re-join words hyphenated across lines
    text = re.sub(r"[^\w$%.,'\"/&+-]+", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


@dataclass
class Rejection:
    product: str
    field: str
    rule: str
    reason: str
    quote: str | None


@dataclass
class VerifyReport:
    kept: int = 0
    page_corrected: int = 0
    rejected: list[Rejection] = field(default_factory=list)

    @property
    def checked(self) -> int:
        return self.kept + len(self.rejected)

    @property
    def verified_rate(self) -> float:
        return self.kept / self.checked if self.checked else 1.0


class Verifier:
    def __init__(self, doc: Document):
        self.pages = [normalize(p) for p in doc.pages]

    def find(self, ev: Evidence | None) -> tuple[bool, int | None, str]:
        if ev is None or not ev.quote.strip():
            return False, None, "no evidence quote"
        q = normalize(ev.quote)
        if len(q.split()) < MIN_QUOTE_WORDS:
            return False, None, "quote too short to verify"
        cited = ev.page - 1
        if 0 <= cited < len(self.pages) and q in self.pages[cited]:
            return True, ev.page, ""
        for i, p in enumerate(self.pages):
            if q in p:
                return True, i + 1, "found on a different page"
        return False, None, "quote not found in document"

    def check(self, extraction: Extraction) -> tuple[Extraction, VerifyReport]:
        report = VerifyReport()
        products: list[Product] = []
        for prod in extraction.products:
            label = f"{prod.insurer} / {prod.program}"
            data = prod.model_copy(deep=True)

            av_ev = data.availability.evidence
            ok, page, why = self.find(av_ev)
            if ok and av_ev is not None:
                self._keep(report, av_ev, page)
            else:
                report.rejected.append(Rejection(label, "availability", f"{data.availability.mode} {data.availability.states}",
                                                 why, data.availability.evidence.quote if data.availability.evidence else None))
                data.availability = data.availability.model_copy(update={"mode": "not_stated", "states": [], "evidence": None})

            for fname in ("location_exclusions", "property_exclusions", "zone_conditions", "limits"):
                kept = []
                for item in getattr(data, fname):
                    ok, page, why = self.find(item.evidence)
                    if ok and item.evidence is not None:
                        self._keep(report, item.evidence, page)
                        kept.append(item)
                    else:
                        report.rejected.append(Rejection(label, fname, _describe(item), why,
                                                         item.evidence.quote if item.evidence else None))
                setattr(data, fname, kept)
            products.append(data)
        return Extraction(products=products), report

    @staticmethod
    def _keep(report: VerifyReport, ev: Evidence, page: int | None) -> None:
        report.kept += 1
        if page is not None and page != ev.page:
            ev.page = page
            report.page_corrected += 1


def _describe(item) -> str:
    d = item.model_dump(exclude={"evidence"})
    return ", ".join(f"{k}={v}" for k, v in d.items() if v not in (None, [], ""))
