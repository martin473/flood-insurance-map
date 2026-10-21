"""Review gate between extraction and the live map.

A model extracted these rules, and the verifier already dropped anything whose quote isn't in the
document. That still leaves room for a quoted rule attached to the wrong program, or a missed
exclusion. So before a program is published, the facts that decide a Rhode Island answer are compared
with the hand-checked labels for that document:

* whether it's sold in the state
* its location exclusions (coastal barrier, floodway, island, over water...)
* its flood-zone conditions that restrict something (plain "eligible in zone X" is ignored)

A program is published only if all three match exactly. Other differences (limits, waiting periods,
building-level exclusions) don't change the map's answer, so they're reported but don't block.
Programs with no label haven't been reviewed and are never published.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .evaluation import Label, _matches
from .schemas import Product


def sold_in(p: Product, state: str) -> str:
    av = p.availability
    if av.mode == "all_except":
        return "no" if state in av.states else "yes"
    if av.mode == "only_listed":
        return "yes" if state in av.states else "no"
    return "not_stated"


def decision_facts(p: Product, state: str) -> set[str]:
    out = {f"sold_in_{state}:{sold_in(p, state)}"}
    out |= {f"location:{x.kind}" for x in p.location_exclusions}
    # "eligible in zone X" restricts nothing, so it can't change an answer
    out |= {f"zone:{z}:{c.rule}" for c in p.zone_conditions for z in c.zones if c.rule != "eligible"}
    return out


@dataclass
class Review:
    published: list[Product] = field(default_factory=list)
    held: list[dict] = field(default_factory=list)      # program, why
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"published": [f"{p.insurer} / {p.program}" for p in self.published], "held": self.held, "notes": self.notes}


def review(products: list[Product], labels: list[Label], state: str = "RI") -> Review:
    r = Review()
    used: set[int] = set()
    for lab in labels:
        name = f"{lab.product.insurer} / {lab.product.program}"
        idx = next((i for i, p in enumerate(products) if i not in used and _matches(lab, p)), None)
        if idx is None:
            r.held.append({"program": name, "why": "not extracted"})
            continue
        used.add(idx)
        got, want = decision_facts(products[idx], state), decision_facts(lab.product, state)
        if got != want:
            r.held.append({"program": name, "why": "differs from the reviewed labels",
                           "extra": sorted(got - want), "missing": sorted(want - got)})
            continue
        r.published.append(products[idx])
    for i, p in enumerate(products):
        if i not in used:
            r.held.append({"program": f"{p.insurer} / {p.program}", "why": "no reviewed label for this program"})
    return r
