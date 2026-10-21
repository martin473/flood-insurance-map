"""Merge extractions from several chunks of one document into one result.

The same program can show up in several chunks, sometimes named a little differently
("FloodPlus" on one page, "Hiscox FloodPlus" on another). Products are merged when their
names match after normalizing, or when one name contains the other. Rules are de-duplicated
by what they say, keeping the first chunk's evidence quote.
"""

from __future__ import annotations

import re

from .schemas import Extraction, Product


def _norm(s: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", s.lower()))


def _names(p: Product) -> set[str]:
    return {n for n in (_norm(p.program), _norm(f"{p.insurer} {p.program}")) if n}


def _same_insurer(a: Product, b: Product) -> bool:
    ia, ib = _norm(a.insurer), _norm(b.insurer)
    if not ia or not ib:
        return True
    if ia in ib or ib in ia or bool(set(ia.split()) & set(ib.split())):
        return True
    # a broker sheet may put the brand in the insurer field ("FloodPlus" / "FloodPlus") where the
    # insurer's own brochure says "Hiscox" / "Hiscox FloodPlus"
    fa, fb = _norm(f"{a.insurer} {a.program}"), _norm(f"{b.insurer} {b.program}")
    return (len(ia) >= 4 and ia in fb) or (len(ib) >= 4 and ib in fa)


def same_product(a: Product, b: Product) -> bool:
    if not _same_insurer(a, b):
        return False
    na, nb = _names(a), _names(b)
    if na & nb:
        return True
    return any(x in y or y in x for x in na for y in nb if min(len(x), len(y)) >= 4)


def _key(item) -> tuple:
    d = item.model_dump(exclude={"evidence", "detail"})
    return tuple((k, tuple(sorted(v)) if isinstance(v, list) else v) for k, v in sorted(d.items()))


def merge_two(a: Product, b: Product) -> Product:
    out = a.model_copy(deep=True)
    if out.availability.mode == "not_stated" and b.availability.mode != "not_stated":
        out.availability = b.availability.model_copy(deep=True)
    for name in ("surplus_lines", "elevation_certificate_required", "waiting_period_days"):
        if getattr(out, name) is None and getattr(b, name) is not None:
            setattr(out, name, getattr(b, name))
    for name in ("location_exclusions", "property_exclusions", "zone_conditions", "limits"):
        items = getattr(out, name)
        seen = {_key(x) for x in items}
        for x in getattr(b, name):
            if _key(x) not in seen:
                items.append(x.model_copy(deep=True))
                seen.add(_key(x))
    if len(_norm(b.program)) > len(_norm(out.program)):   # keep the fuller name
        out.insurer, out.program = b.insurer, b.program
    return out


def merge(parts: list[Extraction]) -> Extraction:
    if len(parts) == 1:
        return parts[0]
    products: list[Product] = []
    for part in parts:
        for p in part.products:
            idx = next((i for i, q in enumerate(products) if same_product(p, q)), None)
            if idx is None:
                products.append(p.model_copy(deep=True))
            else:
                products[idx] = merge_two(products[idx], p)
    return Extraction(products=products)
