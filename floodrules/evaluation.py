"""Score extracted rules against hand-labeled ground truth.

Each product is flattened into atomic facts ("location:cbrs", "state:all_except:AK",
"limit:dwelling:1250000"...). Precision and recall are computed over facts, overall
and per category. Products are matched to labels by keywords in the insurer/program
name. Facts from an unmatched predicted product count as false positives, and facts
of an unmatched label count as misses.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .schemas import Product

CATEGORIES = ["availability", "state", "location", "property", "zone", "limit", "scalar"]


def facts(p: Product) -> set[str]:
    out = {f"availability:{p.availability.mode}"}
    if p.availability.mode in ("all_except", "only_listed"):
        out |= {f"state:{p.availability.mode}:{s}" for s in p.availability.states}
    out |= {f"location:{x.kind}" for x in p.location_exclusions}
    out |= {f"property:{x.kind}" for x in p.property_exclusions}
    out |= {f"zone:{z}:{c.rule}" for c in p.zone_conditions for z in c.zones}
    out |= {f"limit:{x.kind}:{x.amount_usd}" for x in p.limits}
    for name in ("surplus_lines", "elevation_certificate_required", "waiting_period_days"):
        v = getattr(p, name)
        if v is not None:
            out.add(f"scalar:{name}:{v}")
    return out


def category(fact: str) -> str:
    return fact.split(":", 1)[0]


@dataclass
class Counts:
    tp: int = 0
    fp: int = 0
    fn: int = 0

    def add(self, o: "Counts") -> None:
        self.tp += o.tp
        self.fp += o.fp
        self.fn += o.fn

    @property
    def precision(self) -> float | None:
        return self.tp / (self.tp + self.fp) if (self.tp + self.fp) else None

    @property
    def recall(self) -> float | None:
        return self.tp / (self.tp + self.fn) if (self.tp + self.fn) else None

    @property
    def f1(self) -> float | None:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if p and r else (0.0 if p is not None and r is not None else None)

    def as_dict(self) -> dict:
        def rnd(x: float | None) -> float | None:
            return None if x is None else round(x, 3)

        return {"tp": self.tp, "fp": self.fp, "fn": self.fn,
                "precision": rnd(self.precision), "recall": rnd(self.recall), "f1": rnd(self.f1)}


@dataclass
class DocScore:
    overall: Counts = field(default_factory=Counts)
    by_category: dict[str, Counts] = field(default_factory=lambda: {c: Counts() for c in CATEGORIES})
    products_expected: int = 0
    products_matched: int = 0
    unmatched_predictions: list[str] = field(default_factory=list)
    misses: list[str] = field(default_factory=list)
    false_positives: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"overall": self.overall.as_dict(),
                "by_category": {k: v.as_dict() for k, v in self.by_category.items() if v.tp + v.fp + v.fn},
                "products_expected": self.products_expected, "products_matched": self.products_matched,
                "unmatched_predictions": self.unmatched_predictions,
                "misses": sorted(self.misses), "false_positives": sorted(self.false_positives)}


@dataclass
class Label:
    match: list[str]
    product: Product
    match_any: list[list[str]] = field(default_factory=list)   # alternative names; any one matching is enough


def load_labels(path: str | Path) -> list[Label]:
    data = json.loads(Path(path).read_text())
    return [Label(match=[m.lower() for m in item.get("match", [])], product=Product.model_validate(item["product"]),
                  match_any=[alt.lower().split() for alt in item.get("match_any", [])])
            for item in data["products"]]


def _matches(label: Label, p: Product) -> bool:
    name = f"{p.insurer} {p.program}".lower()
    if label.match_any:
        return any(all(w in name for w in alt) for alt in label.match_any)
    return all(m in name for m in label.match)


def score(predicted: list[Product], labels: list[Label]) -> DocScore:
    s = DocScore(products_expected=len(labels))
    used: set[int] = set()

    def tally(pred: set[str], gold: set[str], tag: str) -> None:
        for f in pred | gold:
            c = s.by_category[category(f)]
            if f in pred and f in gold:
                c.tp += 1
                s.overall.tp += 1
            elif f in pred:
                c.fp += 1
                s.overall.fp += 1
                s.false_positives.append(f"{tag}: {f}")
            else:
                c.fn += 1
                s.overall.fn += 1
                s.misses.append(f"{tag}: {f}")

    for lab in labels:
        idx = next((i for i, p in enumerate(predicted) if i not in used and _matches(lab, p)), None)
        tag = " ".join(lab.match or (lab.match_any[0] if lab.match_any else ["?"]))
        if idx is None:
            tally(set(), facts(lab.product), tag)
            continue
        used.add(idx)
        s.products_matched += 1
        tally(facts(predicted[idx]), facts(lab.product), tag)

    for i, p in enumerate(predicted):
        if i not in used:
            s.unmatched_predictions.append(f"{p.insurer} / {p.program}")
            tally(facts(p), set(), f"{p.insurer} / {p.program}")
    return s


def combine(scores: list[DocScore]) -> DocScore:
    total = DocScore()
    for s in scores:
        total.overall.add(s.overall)
        for k, v in s.by_category.items():
            total.by_category[k].add(v)
        total.products_expected += s.products_expected
        total.products_matched += s.products_matched
    return total
