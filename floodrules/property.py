"""Answer the map's three questions for one point, with the data behind each answer.

  Will it flood here?               URI STORMTOOLS depths, falling back to FEMA's zone where the model has no data
  Can it get flood insurance?       the coverage engine over verified insurer rules, plus the federal program
  Can it get homeowners insurance?  private market, with the state FAIR Plan as the insurer of last resort

Every answer lists its sources and when they were checked, so a saved answer can be audited later.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel

from .engine import Decision, PropertyFacts, Status, evaluate_all
from .schemas import Product
from .sources import SCENARIOS, SourceError, Sources, iso

ISLAND_TOWNS = {"Newport", "Middletown", "Portsmouth", "Jamestown", "New Shoreham"}
YEARS = [key for _, _, key in SCENARIOS]
# how each scenario reads in a sentence ("... in a major storm by 2056")
WHEN = {"today": "today", "2056": "by 2056", "2068": "by 2068", "2087": "by 2087",
        "after-2100": "after 2100 (7 ft of sea level rise)", "worst-case": "with 10 ft of sea level rise"}
START = {"today": "today", "2056": "around 2056", "2068": "around 2068", "2087": "around 2087",
         "after-2100": "after 2100, once seas rise 7 ft", "worst-case": "only with 10 ft of sea level rise"}


class Answer(BaseModel):
    answer: str          # short verdict shown big on the map
    level: str           # good | warn | bad | unknown
    detail: str


class ScenarioDepth(BaseModel):
    year: str
    sea_level_rise_ft: int
    depth_ft: float | None


class SourceStatus(BaseModel):
    name: str
    ok: bool
    error: str | None = None


class PropertyReport(BaseModel):
    lat: float
    lng: float
    year: str
    checked_at: str
    will_it_flood: Answer
    flood_insurance: Answer
    homeowners_insurance: Answer
    lenders_require_flood_insurance: bool | None
    depths: list[ScenarioDepth]
    insurers: list[Decision]
    facts: dict[str, Any]
    sources: list[SourceStatus]


def _safe(fn, *args):
    try:
        return fn(*args), None
    except SourceError as e:
        return None, str(e)


def assess(lat: float, lng: float, sources: Sources, products: list[Product], year: str = "2056") -> PropertyReport:
    if year not in YEARS:
        raise ValueError(f"year must be one of {', '.join(YEARS)}")
    with ThreadPoolExecutor(max_workers=4) as pool:
        futs = {name: pool.submit(_safe, fn, lat, lng) for name, fn in (
            ("fema", sources.fema_zone), ("cbrs", sources.cbrs_unit), ("storm", sources.storm_depths), ("town", sources.town_name))}
        res = {k: f.result() for k, f in futs.items()}
    fema, fema_err = res["fema"]
    cbrs, cbrs_err = res["cbrs"]
    depths, storm_err = res["storm"]
    town, town_err = res["town"]

    i = YEARS.index(year)
    flood = _flood_answer(depths, fema, i)
    facts = PropertyFacts(
        state="RI",
        fema_zone=fema["zone"] if fema else None,
        in_floodway=fema["floodway"] if fema else None,
        in_cbrs=(cbrs is not None) if cbrs_err is None else None,
        cbrs_flood_insurance_date=cbrs["flood_insurance_date"] if cbrs else None,
        on_island=(town in ISLAND_TOWNS) if town_err is None and town else None,
        surge_exposed=(depths[0] > 0) if depths else None,
    )
    decisions = evaluate_all(products, facts)
    sfha = fema["sfha"] if fema else None
    names = {"fema": sources.fema.name, "cbrs": sources.cbrs.name, "storm": sources.storm.name, "town": sources.town.name}
    errors = {"fema": fema_err, "cbrs": cbrs_err, "storm": storm_err, "town": town_err}
    return PropertyReport(
        lat=lat, lng=lng, year=year, checked_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        will_it_flood=flood,
        flood_insurance=_insurance_answer(decisions, sfha, cbrs, private_rules=bool(products)),
        homeowners_insurance=_homeowners_answer(depths, fema),
        lenders_require_flood_insurance=sfha,
        depths=[ScenarioDepth(year=label, sea_level_rise_ft=ft, depth_ft=(depths[k] if depths else None))
                for k, (_, ft, label) in enumerate(SCENARIOS)],
        insurers=decisions,
        facts={**facts.model_dump(mode="json"), "town": town, "fema_subtype": fema["subtype"] if fema else None,
               "cbrs_unit": cbrs["unit"] if cbrs else None, "cbrs_type": cbrs["type"] if cbrs else None,
               "cbrs_flood_insurance_date": iso(cbrs["flood_insurance_date"]) if cbrs else None},
        sources=[SourceStatus(name=names[k], ok=errors[k] is None, error=errors[k]) for k in names],
    )


def _when(i: int) -> str:
    return WHEN[YEARS[i]]


def _flood_answer(depths: list[float] | None, fema: dict | None, i: int) -> Answer:
    if depths:
        if depths[i] > 0:
            return Answer(answer="Yes", level="bad", detail=f"About {depths[i]:.1f} ft of water, including storm waves, in a major storm {_when(i)}.")
        first = next((k for k, d in enumerate(depths) if d > 0), None)
        if first is not None:
            start = START[YEARS[first]]
            return Answer(answer=f"Not {_when(i)}", level="warn", detail=f"It starts flooding in a major storm {start}.")
    if fema and fema["sfha"]:
        z = fema["zone"] or ""
        if z.upper().startswith("V"):
            return Answer(answer="Likely", level="bad",
                          detail=f"FEMA maps this as coastal high risk with storm waves (zone {z}). URI's storm model has no data at this spot.")
        return Answer(answer="Likely", level="warn",
                      detail=f"FEMA maps this as high flood risk (zone {z}), possibly from rivers or rain. URI's coastal model shows no storm flooding here.")
    if depths is None and fema is None:
        return Answer(answer="Couldn't check", level="unknown", detail="The flood data services didn't respond. Try again in a minute.")
    return Answer(answer="No", level="good", detail="Not in a major storm, even with 10 ft of sea level rise.")


def _insurance_answer(decisions: list[Decision], sfha: bool | None, cbrs: dict | None, private_rules: bool) -> Answer:
    nfip = next(d for d in decisions if d.insurer == "NFIP")
    private = [d for d in decisions if d.insurer != "NFIP"]
    yes = [d for d in private if d.status == Status.AVAILABLE]
    suffix = " Most mortgage lenders require it here." if sfha else ""
    if nfip.status == Status.AVAILABLE:
        extra = f" plus {len(yes)} private insurer{'s' if len(yes) != 1 else ''}" if yes else ""
        note = "" if private_rules else " No private insurers' rules are loaded yet."
        verb = "offer" if yes else "offers"
        return Answer(answer="Yes", level="good", detail=f"Federal flood insurance (NFIP){extra} {verb} coverage here.{suffix}{note}")
    if yes:
        verb = "offer" if len(yes) != 1 else "offers"
        return Answer(answer="Yes, privately", level="good", detail=f"{len(yes)} private insurer{'s' if len(yes) != 1 else ''} {verb} coverage here.{suffix}")
    if cbrs:
        when = iso(cbrs["flood_insurance_date"]) or "this area was protected"
        return Answer(answer="Limited", level="warn",
                      detail=f"Coastal barrier area {cbrs['unit']}: federal insurance only for buildings built before {when}. "
                             f"No private insurer we track covers it.{suffix}")
    return Answer(answer="Couldn't check", level="unknown", detail="The coastal barrier map didn't respond, so federal eligibility is unknown." + suffix)


def _homeowners_answer(depths: list[float] | None, fema: dict | None) -> Answer:
    """No unsourced market claims here: private insurers write homes statewide, and the RI Joint
    Reinsurance Association (the FAIR Plan) insures homes that can't get coverage in the voluntary
    market (RIJRA testimony to the RI House, March 2026). The map adds ZIP-level non-renewal data."""
    fallback = "If private insurers decline, the state's FAIR Plan (RI Joint Reinsurance Association) insures homes that can't get coverage elsewhere."
    floods_now = bool(depths and depths[0] > 0) or bool(fema and (fema["zone"] or "").upper().startswith("V"))
    if floods_now:
        return Answer(answer="Yes", level="good",
                      detail=f"Private insurers write homes across Rhode Island. {fallback} Homeowners insurance doesn't cover flooding, "
                             "and this spot floods in a major storm today, so it needs a separate flood policy.")
    return Answer(answer="Yes", level="good",
                  detail=f"Private insurers write homes across Rhode Island. {fallback} Homeowners insurance doesn't cover flooding; that's a separate policy.")
