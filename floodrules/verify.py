"""Check every extracted rule against the document it came from.

A rule survives only if its evidence quote appears in the document text. Matching
ignores case, whitespace, hyphenation at line breaks and curly quotes, because PDF
text extraction mangles those. If the quote is found on a different page than cited,
the rule is kept and its page corrected.
"""

from __future__ import annotations

import difflib
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
    closest: str | None = None       # the most similar passage in the document, for diagnosing rejections
    similarity: float | None = None  # 0-1, difflib ratio between the quote and that passage


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

    def closest(self, quote: str | None) -> tuple[str | None, float | None]:
        """The passage in the document most like the quote: a window of the same number of words,
        scored with difflib. Only used to explain rejections, never to accept a rule."""
        q = normalize(quote or "")
        n = len(q.split())
        if not n:
            return None, None
        best, best_score = None, 0.0
        sm = difflib.SequenceMatcher(autojunk=False)
        sm.set_seq2(q)
        for page in self.pages:
            words = page.split()
            for size in {max(1, n - 2), n, n + 2}:
                for i in range(0, max(1, len(words) - size + 1)):
                    cand = " ".join(words[i:i + size])
                    sm.set_seq1(cand)
                    if sm.real_quick_ratio() <= best_score or sm.quick_ratio() <= best_score:
                        continue
                    r = sm.ratio()
                    if r > best_score:
                        best, best_score = cand, r
        return best, round(best_score, 2)

    def _reject(self, report: VerifyReport, label: str, fname: str, rule: str, why: str, quote: str | None) -> None:
        near, score = self.closest(quote) if why == "quote not found in document" else (None, None)
        report.rejected.append(Rejection(label, fname, rule, why, quote, near, score))

    def check(self, extraction: Extraction) -> tuple[Extraction, VerifyReport]:
        report = VerifyReport()
        products: list[Product] = []
        for prod in extraction.products:
            label = f"{prod.insurer} / {prod.program}"
            data = prod.model_copy(deep=True)

            av_ev = data.availability.evidence
            ok, page, why = self.find(av_ev)
            if data.availability.mode == "not_stated":
                pass   # nothing was claimed, so there's nothing to verify
            elif ok and av_ev is not None:
                self._keep(report, av_ev, page)
            else:
                self._reject(report, label, "availability", f"{data.availability.mode} {data.availability.states}",
                             why, data.availability.evidence.quote if data.availability.evidence else None)
                data.availability = data.availability.model_copy(update={"mode": "not_stated", "states": [], "evidence": None})

            for fname in ("location_exclusions", "property_exclusions", "zone_conditions", "limits"):
                kept = []
                for item in getattr(data, fname):
                    ok, page, why = self.find(item.evidence)
                    if ok and fname == "limits" and item.evidence is not None and item.amount_usd not in money_values(item.evidence.quote):
                        # The quote exists but doesn't state this amount: the model calculated or guessed it,
                        # e.g. "20% of the dwelling limit" written as $450,000.
                        ok, why = False, f"quote doesn't state ${item.amount_usd:,}"
                    if ok and item.evidence is not None:
                        self._keep(report, item.evidence, page)
                        kept.append(item)
                    else:
                        self._reject(report, label, fname, _describe(item), why, item.evidence.quote if item.evidence else None)
                setattr(data, fname, kept)
            products.append(data)
        return Extraction(products=products), report

    @staticmethod
    def _keep(report: VerifyReport, ev: Evidence, page: int | None) -> None:
        report.kept += 1
        if page is not None and page != ev.page:
            ev.page = page
            report.page_corrected += 1


_MONEY = re.compile(r"\$?\s*(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(million|mil|mm|m|thousand|k)?\b", re.I)
_SCALE = {"million": 1_000_000, "mil": 1_000_000, "mm": 1_000_000, "m": 1_000_000, "thousand": 1_000, "k": 1_000}


def money_values(text: str) -> set[int]:
    """Every dollar amount written in the text: $1,250,000, 1.25M, $2.5 million, 250K, 30,000."""
    out = set()
    for num, unit in _MONEY.findall(text):
        value = float(num.replace(",", ""))
        out.add(round(value * _SCALE.get(unit.lower(), 1)) if unit else round(value))
    return out


def _describe(item) -> str:
    d = item.model_dump(exclude={"evidence"})
    return ", ".join(f"{k}={v}" for k, v in d.items() if v not in (None, [], ""))
