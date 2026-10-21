"""Answer the map's three questions for one point, with the data behind each answer.

  Will it flood here?               URI STORMTOOLS depths, falling back to FEMA's zone where the model has no data
  Can it get flood insurance?       the coverage engine over verified insurer rules, plus the federal program
  Can it get homeowners insurance?  risk signals insurers are known to use; always available via the state FAIR Plan

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
WASHINGTON_COUNTY = {"Charlestown", "Exeter", "Hopkinton", "Narragansett", "New Shoreham", "North Kingstown", "Richmond",
                     "South Kingstown", "Westerly"}
NEWPORT_COUNTY = {"Jamestown", "Little Compton", "Middletown", "Newport", "Portsmouth", "Tiverton"}
YEARS = [label for _, _, label in SCENARIOS]


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


def assess(lat: float, lng: float, sources: Sources, products: list[Product], year: str = "2050") -> PropertyReport:
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
        homeowners_insurance=_homeowners_answer(depths, fema, town),
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
    return "today" if i == 0 else f"by {YEARS[i]}"


def _flood_answer(depths: list[float] | None, fema: dict | None, i: int) -> Answer:
    if depths:
        if depths[i] > 0:
            return Answer(answer="Yes", level="bad", detail=f"About {depths[i]:.1f} ft of water, including storm waves, in a major storm {_when(i)}.")
        first = next((k for k, d in enumerate(depths) if d > 0), None)
        if first is not None:
            start = "today" if first == 0 else f"around {YEARS[first]}"
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
    return Answer(answer="No", level="good", detail="Not in a major storm, even by 2100.")


def _insurance_answer(decisions: list[Decision], sfha: bool | None, cbrs: dict | None, private_rules: bool) -> Answer:
    nfip = next(d for d in decisions if d.insurer == "NFIP")
    private = [d for d in decisions if d.insurer != "NFIP"]
    yes = [d for d in private if d.status == Status.AVAILABLE]
    suffix = " Lenders require it here." if sfha else ""
    if nfip.status == Status.AVAILABLE:
        extra = f" plus {len(yes)} private insurer{'s' if len(yes) != 1 else ''}" if yes else ""
        note = "" if private_rules else " No private insurers' rules are loaded yet."
        return Answer(answer="Yes", level="good", detail=f"Federal flood insurance (NFIP){extra} offer coverage here.{suffix}{note}")
    if yes:
        return Answer(answer="Yes, privately", level="good", detail=f"{len(yes)} private insurer{'s' if len(yes) != 1 else ''} offer coverage here.{suffix}")
    if cbrs:
        when = iso(cbrs["flood_insurance_date"]) or "this area was protected"
        return Answer(answer="Limited", level="warn",
                      detail=f"Coastal barrier area {cbrs['unit']}: federal insurance only for buildings built before {when}. "
                             f"No private insurer we track covers it.{suffix}")
    return Answer(answer="Couldn't check", level="unknown", detail="The coastal barrier map didn't respond, so federal eligibility is unknown." + suffix)


def _homeowners_answer(depths: list[float] | None, fema: dict | None, town: str | None) -> Answer:
    floods_now = bool(depths and depths[0] > 0) or bool(fema and (fema["zone"] or "").upper().startswith("V"))
    if floods_now:
        return Answer(answer="Yes, but harder", level="warn",
                      detail="This spot floods in a major storm today, and insurers limit how many homes like this they cover. "
                             "If private insurers say no, the state's FAIR Plan must offer a policy, typically at 40 to 50% higher cost.")
    if town in WASHINGTON_COUNTY or town in NEWPORT_COUNTY:
        county = "Newport" if town in NEWPORT_COUNTY else "Washington"
        return Answer(answer="Yes", level="good",
                      detail=f"Private insurers write homes here, but {county} County has some of the highest non-renewal rates in the country. "
                             "Get quotes early in your purchase.")
    return Answer(answer="Yes", level="good",
                  detail="Widely available from private insurers. Homeowners insurance doesn't cover flooding; that's a separate policy.")
