"""Make a model's JSON fit the schema before validating it, item by item.

When an endpoint doesn't enforce the JSON schema (Ollama Cloud, some local models), models
write near-misses: zone "AE" instead of "A", limit "building" instead of "dwelling", state
names instead of codes. Rejecting the whole document for one such value throws away
everything else it got right. So:

1. Map known variants to schema values.
2. Validate each rule on its own and drop only the ones that still don't fit,
   recording what was dropped and why so the eval can report it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from .schemas import US_STATES, Availability, Limit, LocationExclusion, Product, PropertyExclusion, ZoneCondition

STATE_NAMES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA", "colorado": "CO",
    "connecticut": "CT", "delaware": "DE", "district of columbia": "DC", "washington dc": "DC", "florida": "FL",
    "georgia": "GA", "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD", "massachusetts": "MA", "michigan": "MI",
    "minnesota": "MN", "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI",
    "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT",
    "virginia": "VA", "washington": "WA", "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
}

LIMIT_KINDS = {
    "dwelling": ["building", "dwelling_coverage", "coverage_a", "structure", "main_building", "building_coverage"],
    "contents": ["personal_property", "coverage_c", "contents_coverage", "belongings"],
    "other_structures": ["coverage_b", "detached_structures", "appurtenant_structures"],
    "loss_of_use": ["coverage_d", "additional_living_expense", "additional_living_expenses", "ale", "living_expenses"],
    "total_insured_value": ["tiv", "maximum_tiv", "max_tiv", "total_limit", "program_tiv"],
    "increased_cost_of_compliance": ["icc", "increased_cost_of_compliance_coverage"],
}
LOCATION_KINDS = {
    "cbrs": ["cbra", "cbra_zone", "coastal_barrier", "coastal_barrier_resources_system", "coastal_barrier_resources_act", "cbrs_unit"],
    "floodway": ["regulatory_floodway", "fema_floodway", "designated_floodway"],
    "over_water": ["partially_over_water", "over_the_water", "built_over_water"],
    "island": ["islands"],
    "barrier_island": ["barrier_islands"],
}
PROPERTY_KINDS = {
    "manufactured_home": ["mobile_home", "mobile_homes", "manufactured_homes", "manufactured_or_mobile_home", "trailer"],
    "condo": ["condominium", "condos", "condominiums", "condo_units"],
    "prior_losses": ["prior_loss", "loss_history", "multiple_losses", "previous_losses"],
    "severe_repetitive_loss": ["srl", "repetitive_loss", "severe_repetitive_losses"],
    "subgrade_contents": ["basement_contents", "below_grade_contents", "subgrade", "contents_below_grade"],
    "nfip_grandfathered": ["grandfathered", "grandfathered_rating", "grandfathered_properties"],
}
ZONE_RULES = {
    "eligible_if_elevated_above_bfe": ["elevated_above_bfe", "eligible_if_elevated", "if_elevated", "eligible_when_elevated", "eligible_if_above_bfe"],
    "eligible_if_not_surge_exposed": ["not_surge_exposed", "eligible_if_non_surge", "non_surge_exposed", "eligible_if_inland"],
    "ineligible": ["not_eligible", "excluded"],
    "eligible": ["allowed", "accepted"],
}
AVAIL_MODES = {"all_except": ["all_states_except", "all_but", "except"], "only_listed": ["listed", "only", "listed_states"],
               "all_states": ["all", "nationwide"], "not_stated": ["unknown", "none", "unspecified"]}


def _snake(v: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(v).lower()).strip("_")


def _lookup(value: Any, table: dict[str, list[str]]) -> Any:
    s = _snake(value)
    for canon, aliases in table.items():
        if s == canon or s in aliases:
            return canon
    return value


def _zone(v: Any) -> str | None:
    s = str(v).strip().upper()
    if s.startswith("V"):
        return "V"
    if s.startswith("A"):
        return "A"
    if s in ("B", "C", "D"):
        return s
    if s.startswith("X") or "SHADED" in s or "0.2" in s or "500" in s:
        return "X"
    return None


def _state(v: Any) -> str | None:
    s = str(v).strip()
    if s.upper() in US_STATES:
        return s.upper()
    return STATE_NAMES.get(s.lower().replace(".", ""))


def _int(v: Any) -> Any:
    if isinstance(v, str):
        digits = re.sub(r"[^\d.]", "", v)
        mult = 1_000_000 if re.search(r"\d\s*(m|million)\b", v.lower()) else 1_000 if re.search(r"\d\s*k\b", v.lower()) else 1
        try:
            return int(float(digits) * mult)
        except ValueError:
            return v
    return v


def _bool(v: Any) -> Any:
    if isinstance(v, str) and v.strip().lower() in ("yes", "true", "required"):
        return True
    if isinstance(v, str) and v.strip().lower() in ("no", "false", "not required"):
        return False
    return v


@dataclass
class Dropped:
    product: str
    field: str
    value: Any
    reason: str


def repair(raw: Any) -> tuple[dict, list[Dropped]]:
    dropped: list[Dropped] = []
    if not isinstance(raw, dict) or not isinstance(raw.get("products"), list):
        return {"products": []}, [Dropped("-", "products", str(raw)[:200], "response has no products list")]
    products = []
    for p in raw["products"]:
        if not isinstance(p, dict) or not p.get("insurer") or not p.get("program"):
            dropped.append(Dropped("-", "product", str(p)[:200], "missing insurer or program name"))
            continue
        label = f"{p['insurer']} / {p['program']}"
        out: dict[str, Any] = {"insurer": str(p["insurer"]), "program": str(p["program"])}

        av = dict(p.get("availability") or {})
        av["mode"] = _lookup(av.get("mode", "not_stated"), AVAIL_MODES)
        states = [_state(s) for s in av.get("states") or []]
        if any(s is None for s in states):
            dropped.append(Dropped(label, "availability.states", av.get("states"), "unrecognized state names removed"))
        av["states"] = [s for s in states if s]
        out["availability"] = _valid(Availability, av, label, "availability", dropped) or {"mode": "not_stated", "states": []}

        for name in ("surplus_lines", "elevation_certificate_required"):
            v = _bool(p.get(name))
            out[name] = v if isinstance(v, bool) else None
        w = _int(p.get("waiting_period_days"))
        out["waiting_period_days"] = w if isinstance(w, int) else None

        out["location_exclusions"] = [x for x in (
            _valid(LocationExclusion, {**i, "kind": _lookup(i.get("kind"), LOCATION_KINDS)}, label, "location_exclusions", dropped)
            for i in _items(p, "location_exclusions")) if x]
        props = []
        for i in _items(p, "property_exclusions"):
            kind = _lookup(i.get("kind"), PROPERTY_KINDS)
            if kind not in PROPERTY_KINDS and kind != "other":   # keep the rule, labeled as other
                i = {**i, "detail": i.get("detail") or str(i.get("kind"))}
                kind = "other"
            x = _valid(PropertyExclusion, {**i, "kind": kind}, label, "property_exclusions", dropped)
            if x:
                props.append(x)
        out["property_exclusions"] = props
        zones = []
        for i in _items(p, "zone_conditions"):
            zs = [_zone(z) for z in (i.get("zones") or [])]
            zs = list(dict.fromkeys(z for z in zs if z))
            x = _valid(ZoneCondition, {**i, "zones": zs, "rule": _lookup(i.get("rule"), ZONE_RULES)}, label, "zone_conditions", dropped) if zs else None
            if not zs:
                dropped.append(Dropped(label, "zone_conditions", i.get("zones"), "no recognizable FEMA zones"))
            if x:
                zones.append(x)
        out["zone_conditions"] = zones
        out["limits"] = [x for x in (
            _valid(Limit, {**i, "kind": _lookup(i.get("kind"), LIMIT_KINDS), "amount_usd": _int(i.get("amount_usd"))}, label, "limits", dropped)
            for i in _items(p, "limits")) if x]
        try:
            products.append(Product.model_validate(out).model_dump(mode="json"))
        except ValidationError as e:
            dropped.append(Dropped(label, "product", label, _why(e)))
    return {"products": products}, dropped


def _items(p: dict, name: str) -> list[dict]:
    return [i for i in (p.get(name) or []) if isinstance(i, dict)]


def _valid(model, data: dict, label: str, field: str, dropped: list[Dropped]) -> dict | None:
    if isinstance(data.get("evidence"), dict):
        ev = data["evidence"]
        data = {**data, "evidence": {"quote": str(ev.get("quote", "")), "page": _int(ev.get("page", 0)) or 0}}
    try:
        return model.model_validate(data).model_dump(mode="json")
    except ValidationError as e:
        shown = {k: v for k, v in data.items() if k != "evidence"}
        dropped.append(Dropped(label, field, shown, _why(e)))
        return None


def _why(e: ValidationError) -> str:
    err = e.errors()[0]
    where = ".".join(str(x) for x in err["loc"])
    return f"{where}: {err['msg']}"
