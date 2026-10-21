"""Coverage decisions for the five Rhode Island spots checked against live data on 2026-10-21.

The facts below are what FEMA, USFWS (CBRS) and Nominatim returned for each spot.
The rules are the labeled ground truth, so these tests pin the engine's logic
independently of extraction quality.
"""

from datetime import date
from pathlib import Path

import pytest

from floodrules.engine import PropertyFacts, Status, evaluate, evaluate_all, evaluate_nfip, zone_group
from floodrules.evaluation import load_labels

LABELS = Path(__file__).resolve().parent.parent / "evals" / "labels"


def rules():
    neptune = load_labels(LABELS / "rt_specialty_2023.json")[1].product
    hiscox = load_labels(LABELS / "hiscox_floodplus_2024.json")[0].product
    hiscox_rt = load_labels(LABELS / "rt_specialty_2023.json")[0].product
    # Combine the two FloodPlus documents the way the map does: exclusions from 2023, zones from 2024
    hiscox = hiscox.model_copy(update={"location_exclusions": hiscox_rt.location_exclusions})
    msi = load_labels(LABELS / "msi_flood_2026.json")[0].product
    return {"neptune": neptune, "hiscox": hiscox, "msi": msi}


SPOTS = {
    # Misquamicut lot: FEMA AE, inside CBRS Otherwise Protected Area RI-13P (flood insurance date 2025-11-25), Westerly
    "misquamicut": PropertyFacts(fema_zone="AE", in_floodway=False, in_cbrs=True,
                                 cbrs_flood_insurance_date=date(2025, 11, 25), on_island=False),
    # Napatree Point: FEMA VE, CBRS System Unit D08 (1983-10-01)
    "napatree": PropertyFacts(fema_zone="VE", in_floodway=False, in_cbrs=True,
                              cbrs_flood_insurance_date=date(1983, 10, 1), on_island=False),
    # Newport Point: FEMA VE, no CBRS, Aquidneck Island
    "newport_point": PropertyFacts(fema_zone="VE", in_floodway=False, in_cbrs=False, on_island=True),
    # Oakland Beach, Warwick: FEMA X, no CBRS, mainland; floods in a 100-year storm by 2050 (surge exposed)
    "oakland_beach": PropertyFacts(fema_zone="X", in_floodway=False, in_cbrs=False, on_island=False,
                                   on_barrier_island=False, surge_exposed=True),
    # Cranston, inland: FEMA X, no CBRS, not surge exposed
    "cranston": PropertyFacts(fema_zone="X", in_floodway=False, in_cbrs=False, on_island=False,
                              on_barrier_island=False, surge_exposed=False),
}

EXPECTED = {
    "misquamicut":   {"nfip": "maybe",     "neptune": "not_offered", "hiscox": "not_offered", "msi": "not_offered"},
    "napatree":      {"nfip": "maybe",     "neptune": "not_offered", "hiscox": "not_offered", "msi": "not_offered"},
    "newport_point": {"nfip": "available", "neptune": "available",   "hiscox": "maybe",       "msi": "not_offered"},
    "oakland_beach": {"nfip": "available", "neptune": "available",   "hiscox": "not_offered", "msi": "available"},
    "cranston":      {"nfip": "available", "neptune": "available",   "hiscox": "available",   "msi": "available"},
}


@pytest.mark.parametrize("spot", SPOTS)
def test_golden_spots(spot):
    facts, r = SPOTS[spot], rules()
    got = {"nfip": evaluate_nfip(facts).status.value, **{k: evaluate(p, facts).status.value for k, p in r.items()}}
    assert got == EXPECTED[spot]


def test_reasons_quote_the_rule_when_rules_have_evidence():
    from floodrules.schemas import Product
    p = Product.model_validate({"insurer": "X", "program": "X", "availability": {"mode": "all_states"},
                                "location_exclusions": [{"kind": "cbrs", "evidence": {"quote": "CBRA zones are ineligible", "page": 3}}]})
    d = evaluate(p, SPOTS["napatree"])
    assert d.status == Status.NOT_OFFERED
    assert d.reasons[0].quote == "CBRA zones are ineligible" and d.reasons[0].page == 3


def test_unknown_facts_downgrade_to_maybe_not_no():
    d = evaluate(rules()["msi"], PropertyFacts(fema_zone="X"))
    assert d.status == Status.MAYBE
    assert any("hasn't been checked" in r.text for r in d.reasons)


def test_state_availability():
    p = rules()["neptune"]
    assert evaluate(p, PropertyFacts(state="KY", in_cbrs=False)).status == Status.NOT_OFFERED
    assert evaluate(p, PropertyFacts(state="RI", in_cbrs=False)).status == Status.AVAILABLE


def test_building_rules_are_listed_as_not_checked():
    d = evaluate(rules()["msi"], SPOTS["cranston"])
    assert "severe_repetitive_loss" in d.not_checked and "over_water" in d.not_checked


def test_nfip_explains_coastal_barrier_date():
    d = evaluate_nfip(SPOTS["napatree"])
    assert "1983-10-01" in d.reasons[0].text
    assert evaluate_nfip(PropertyFacts()).status == Status.MAYBE


def test_evaluate_all_sorts_best_first():
    out = evaluate_all(list(rules().values()), SPOTS["newport_point"])
    assert [d.status for d in out] == sorted([d.status for d in out], key=["available", "maybe", "not_offered"].index)


@pytest.mark.parametrize("code,group", [("AE", "A"), ("AO", "A"), ("VE", "V"), ("X", "X"), ("", None), (None, None), ("OPEN WATER", None)])
def test_zone_group(code, group):
    g = zone_group(code)
    assert (g.value if g else None) == group
