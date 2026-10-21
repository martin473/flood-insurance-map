"""Decide which programs might cover a property, from extracted rules.

Facts come from map lookups (FEMA zone, floodway, coastal barrier unit, town).
Unknown facts stay None. A rule that depends on an unknown fact can't rule a program
out; it downgrades the answer to "maybe" and says what to check.
"""

from __future__ import annotations

from datetime import date
from enum import Enum

from pydantic import BaseModel, Field

from .schemas import FloodZone, LocationKind, Product, ZoneRule


class Status(str, Enum):
    AVAILABLE = "available"
    MAYBE = "maybe"
    NOT_OFFERED = "not_offered"


class PropertyFacts(BaseModel):
    state: str = "RI"
    fema_zone: str | None = Field(default=None, description="FEMA zone code at the point, e.g. AE, VE, X")
    in_floodway: bool | None = None
    in_cbrs: bool | None = None
    cbrs_flood_insurance_date: date | None = Field(default=None, description="Date federal flood insurance was prohibited for the CBRS unit")
    on_island: bool | None = None
    on_barrier_island: bool | None = None
    over_water: bool | None = None
    elevated_above_bfe: bool | None = None
    surge_exposed: bool | None = None


class Reason(BaseModel):
    effect: Status
    text: str
    quote: str | None = None
    page: int | None = None


class Decision(BaseModel):
    insurer: str
    program: str
    status: Status
    reasons: list[Reason]
    not_checked: list[str] = Field(default_factory=list, description="Rules about the building itself that a map can't check")


def zone_group(code: str | None) -> FloodZone | None:
    if not code:
        return None
    c = code.strip().upper()
    if c.startswith("V"):
        return FloodZone.V
    if c.startswith("A"):
        return FloodZone.A
    if c in ("B", "C", "X", "D"):
        return FloodZone(c)
    if c.startswith("X") or c.startswith("SHADED X"):
        return FloodZone.X
    return None


_LOCATION_FACT = {
    LocationKind.CBRS.value: ("in_cbrs", "a coastal barrier (CBRA) area"),
    LocationKind.FLOODWAY.value: ("in_floodway", "a FEMA floodway"),
    LocationKind.OVER_WATER.value: ("over_water", "a building over water"),
    LocationKind.ISLAND.value: ("on_island", "an island"),
    LocationKind.BARRIER_ISLAND.value: ("on_barrier_island", "a barrier island"),
}

_RANK = {Status.AVAILABLE: 0, Status.MAYBE: 1, Status.NOT_OFFERED: 2}


def evaluate(product: Product, facts: PropertyFacts) -> Decision:
    reasons: list[Reason] = []

    av = product.availability
    st = facts.state.upper()
    if av.mode == "all_except" and st in av.states:
        reasons.append(_r(Status.NOT_OFFERED, f"Not sold in {st}.", av.evidence))
    elif av.mode == "only_listed" and st not in av.states:
        reasons.append(_r(Status.NOT_OFFERED, f"Not sold in {st}.", av.evidence))
    elif av.mode == "not_stated":
        reasons.append(Reason(effect=Status.MAYBE, text=f"The document doesn't say whether it's sold in {st}."))

    for ex in product.location_exclusions:
        fact_name, label = _LOCATION_FACT[ex.kind]
        value = getattr(facts, fact_name)
        if value is True:
            reasons.append(_r(Status.NOT_OFFERED, f"Doesn't cover {label}.", ex.evidence))
        elif value is None and fact_name not in ("over_water",):
            reasons.append(_r(Status.MAYBE, f"Doesn't cover {label}, and this property hasn't been checked for it.", ex.evidence))

    zg = zone_group(facts.fema_zone)
    for zc in product.zone_conditions:
        if zg is None or zg.value not in zc.zones:
            continue
        if zc.rule == ZoneRule.INELIGIBLE.value:
            reasons.append(_r(Status.NOT_OFFERED, f"Not offered in FEMA zone {facts.fema_zone}.", zc.evidence))
        elif zc.rule == ZoneRule.ELIGIBLE_IF_ELEVATED_ABOVE_BFE.value:
            if facts.elevated_above_bfe is False:
                reasons.append(_r(Status.NOT_OFFERED, f"In zone {facts.fema_zone}, only homes above the flood elevation qualify.", zc.evidence))
            elif facts.elevated_above_bfe is None:
                reasons.append(_r(Status.MAYBE, f"In zone {facts.fema_zone}, only if the home sits above the flood elevation.", zc.evidence))
        elif zc.rule == ZoneRule.ELIGIBLE_IF_NOT_SURGE_EXPOSED.value:
            if facts.surge_exposed is True:
                reasons.append(_r(Status.NOT_OFFERED, f"In zone {facts.fema_zone}, storm-surge-exposed homes don't qualify.", zc.evidence))
            elif facts.surge_exposed is None:
                reasons.append(_r(Status.MAYBE, f"In zone {facts.fema_zone}, only if the home isn't exposed to storm surge.", zc.evidence))

    status = max((r.effect for r in reasons), key=_RANK.__getitem__, default=Status.AVAILABLE)
    if status == Status.AVAILABLE:
        reasons.append(Reason(effect=Status.AVAILABLE, text="No published rule excludes this location."))
    not_checked = [f"{p.kind}" + (f" ({p.detail})" if p.detail else "") for p in product.property_exclusions]
    if any(ex.kind == LocationKind.OVER_WATER.value for ex in product.location_exclusions) and facts.over_water is None:
        not_checked.append("over_water")
    return Decision(insurer=product.insurer, program=product.program, status=status, reasons=reasons, not_checked=not_checked)


def evaluate_nfip(facts: PropertyFacts) -> Decision:
    """The federal program, from the Coastal Barrier Resources Act rather than a document."""
    if facts.in_cbrs:
        when = facts.cbrs_flood_insurance_date.isoformat() if facts.cbrs_flood_insurance_date else "the area's designation date"
        return Decision(insurer="NFIP", program="National Flood Insurance Program", status=Status.MAYBE, reasons=[Reason(
            effect=Status.MAYBE, text=f"Coastal barrier area: only buildings built or permitted before {when} can get federal flood insurance.")])
    if facts.in_cbrs is None:
        return Decision(insurer="NFIP", program="National Flood Insurance Program", status=Status.MAYBE, reasons=[Reason(
            effect=Status.MAYBE, text="Coastal barrier status unknown. Federal insurance is available everywhere else in participating communities.")])
    return Decision(insurer="NFIP", program="National Flood Insurance Program", status=Status.AVAILABLE, reasons=[Reason(
        effect=Status.AVAILABLE, text="Available in every participating community, which includes all of Rhode Island.")])


def evaluate_all(products: list[Product], facts: PropertyFacts) -> list[Decision]:
    out = [evaluate_nfip(facts)] + [evaluate(p, facts) for p in products]
    return sorted(out, key=lambda d: _RANK[d.status])


def _r(effect: Status, text: str, ev) -> Reason:
    return Reason(effect=effect, text=text, quote=ev.quote if ev else None, page=ev.page if ev else None)
