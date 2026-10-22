"""Property lookups against recorded responses.

The values below are what FEMA NFHL, USFWS CBRS, URI STORMTOOLS and Nominatim returned
for five Rhode Island spots on 2026-10-21 (depths re-read that evening with the point-first method).
The nightly contract check (tests/contract) compares them with the live services. The fake transport serves them in the same
shapes the real services use, so these tests pin the full lookup-to-answer path offline.
"""

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
import requests

from floodrules.engine import Status
from floodrules.evaluation import load_labels
from floodrules.property import assess
from floodrules.sources import CBRS, FEMA, NOMINATIM, STORM, Sources

LABELS = Path(__file__).resolve().parent.parent / "evals" / "labels"


def ms(d):
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)


SPOTS = {
    "misquamicut": dict(lat=41.3260, lng=-71.7920, fema=("AE", "T", None), cbrs=("RI-13P", "Otherwise Protected Area", date(2025, 11, 25)),
                        depths=[12.0, 15.4, 17.1, 20.6, 24.0, 29.1], town="Westerly"),
    "napatree": dict(lat=41.3113, lng=-71.8826, fema=("VE", "T", None), cbrs=("D08", "System Unit", date(1983, 10, 1)),
                     depths=[0, 0, 0, 0, 0, 0], town="Westerly"),
    "newport_point": dict(lat=41.4925, lng=-71.3215, fema=("VE", "T", None), cbrs=None,
                          depths=[12.4, 14.8, 15.9, 18.0, 20.1, 23.2], town="Newport"),
    "oakland_beach": dict(lat=41.6980, lng=-71.4040, fema=("X", "F", "0.2 PCT ANNUAL CHANCE FLOOD HAZARD"), cbrs=None,
                          depths=[0, 1.5, 1.5, 3.3, 5.6, 9.8], town="Warwick"),
    "cranston": dict(lat=41.7798, lng=-71.4950, fema=("X", "F", "AREA OF MINIMAL FLOOD HAZARD"), cbrs=None, depths=[0, 0, 0, 0, 0, 0],
                     town="Cranston"),
}


class Recorded:
    """Serves the recorded responses. `fail` makes one source return HTTP 503."""

    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail

    def spot(self, lng, lat):
        return next(s for s in SPOTS.values() if abs(s["lat"] - lat) < 0.01 and abs(s["lng"] - lng) < 0.01)

    def __call__(self, url, params, headers, timeout):
        self.calls.append(url)
        assert "flood-insurance-map" in headers["User-Agent"]
        if self.fail and self.fail in url:
            return 503, {"error": {"message": "Service Unavailable"}}
        if url in (FEMA, CBRS):
            lng, lat = map(float, params["geometry"].split(","))
            s = self.spot(lng, lat)
            if url == FEMA:
                zone, sfha, sub = s["fema"]
                return 200, {"features": [{"attributes": {"FLD_ZONE": zone, "SFHA_TF": sfha, "ZONE_SUBTY": sub, "STATIC_BFE": -9999}}]}
            if not s["cbrs"]:
                return 200, {"features": []}
            unit, kind, fi = s["cbrs"]
            return 200, {"features": [{"attributes": {"Unit": unit, "CBRS_Type": kind, "FI_Date": ms(fi)}}]}
        if url == STORM:
            pts = json.loads(params["geometry"])["points"]
            s = self.spot(*pts[0])
            rid = json.loads(params["mosaicRule"])["lockRasterIds"][0]
            depth = s["depths"][rid - 1]
            if not depth:
                return 200, {"error": {"code": 400, "message": "Invalid or missing input parameters."}}
            # neighbors vary (one is deeper, next to the water); the point's own value should win
            return 200, {"samples": [{"locationId": 1, "value": str(depth + 1.5)}, {"locationId": 0, "value": str(depth)},
                                     {"locationId": 2, "value": "NoData"}]}
        if url == NOMINATIM:
            s = self.spot(params["lon"], params["lat"])
            return 200, {"address": {"town" if s["town"] != "Newport" else "city": s["town"], "state": "Rhode Island"}}
        raise AssertionError(url)


def rules():
    neptune = load_labels(LABELS / "rt_specialty_2023.json")[1].product
    hiscox_rt = load_labels(LABELS / "rt_specialty_2023.json")[0].product
    hiscox = load_labels(LABELS / "hiscox_floodplus_2024.json")[0].product.model_copy(update={"location_exclusions": hiscox_rt.location_exclusions})
    msi = load_labels(LABELS / "msi_flood_2026.json")[0].product
    return [neptune, hiscox, msi]


def report(name, products=None, transport=None, year="2056"):
    s = SPOTS[name]
    return assess(s["lat"], s["lng"], Sources(transport or Recorded(), sleep=lambda x: None), products or [], year=year)


EXPECTED = {
    "misquamicut":   ("Yes", "Limited", "Yes", True),
    "napatree":      ("Likely", "Limited", "Yes", True),
    "newport_point": ("Yes", "Yes", "Yes", True),
    "oakland_beach": ("Yes", "Yes", "Yes", False),
    "cranston":      ("No", "Yes", "Yes", False),
}


@pytest.mark.parametrize("name", SPOTS)
def test_three_answers_for_recorded_spots(name):
    r = report(name)
    assert (r.will_it_flood.answer, r.flood_insurance.answer, r.homeowners_insurance.answer,
            r.lenders_require_flood_insurance) == EXPECTED[name]
    assert all(s.ok for s in r.sources)


def test_depths_use_the_point_itself_and_follow_the_year():
    r = report("misquamicut")
    assert [d.depth_ft for d in r.depths] == SPOTS["misquamicut"]["depths"]
    assert "15.4 ft" in r.will_it_flood.detail and "by 2056" in r.will_it_flood.detail
    assert "12.0 ft" in report("misquamicut", year="today").will_it_flood.detail
    assert "7 ft of sea level rise" in report("misquamicut", year="after-2100").will_it_flood.detail


def test_depth_falls_back_to_the_median_nearby_when_the_point_has_no_data():
    class Gap(Recorded):
        def __call__(self, url, params, headers, timeout):
            if url == STORM:
                return 200, {"samples": [{"locationId": 0, "value": "NoData"}, {"locationId": 3, "value": "9.0"},
                                         {"locationId": 4, "value": "10.0"}, {"locationId": 5, "value": "16.0"}]}
            return super().__call__(url, params, headers, timeout)
    r = report("misquamicut", transport=Gap())
    assert [d.depth_ft for d in r.depths] == [10.0] * 6     # median, not the 16 ft next to open water


def test_years_follow_noaa_2022_high_at_newport():
    from floodrules.sources import SCENARIOS
    assert [(ft, key) for _, ft, key in SCENARIOS] == [(0, "today"), (2, "2056"), (3, "2068"), (5, "2087"), (7, "after-2100"), (10, "worst-case")]


def test_flooding_that_starts_later():
    r = report("oakland_beach", year="today")
    assert r.will_it_flood.answer == "Not today" and "around 2056" in r.will_it_flood.detail


def test_coastal_barrier_details_are_kept_for_audit():
    r = report("napatree")
    assert r.facts["cbrs_unit"] == "D08" and r.facts["cbrs_flood_insurance_date"] == "1983-10-01"
    assert "1983-10-01" in r.flood_insurance.detail
    assert r.checked_at.endswith("+00:00")


def test_insurer_decisions_with_rules_loaded():
    r = report("newport_point", products=rules())
    got = {d.insurer: d.status for d in r.insurers}
    assert got == {"NFIP": Status.AVAILABLE, "Neptune Flood": Status.AVAILABLE, "Hiscox": Status.MAYBE, "MSI": Status.NOT_OFFERED}
    assert "plus 1 private insurer" in r.flood_insurance.detail
    assert r.facts["on_island"] is True


def test_one_failing_source_degrades_instead_of_failing():
    t = Recorded(fail="hazards.fema.gov")
    r = report("cranston", transport=t)
    status = {s.name: s.ok for s in r.sources}
    assert status == {"fema_nfhl": False, "usfws_cbrs": True, "uri_stormtools": True, "nominatim": True}
    assert r.lenders_require_flood_insurance is None
    assert r.will_it_flood.answer == "No"           # storm data still answers
    assert t.calls.count(FEMA) == 3                 # first try + 2 retries


def test_everything_down_says_so():
    class Down:
        def __call__(self, url, params, headers, timeout):
            raise requests.ConnectionError("down")
    r = report("cranston", transport=Down())
    assert r.will_it_flood.answer == "Couldn't check" and r.flood_insurance.answer == "Couldn't check"
    assert not any(s.ok for s in r.sources)


def test_cache_and_metrics():
    t = Recorded()
    src = Sources(t, sleep=lambda x: None)
    s = SPOTS["cranston"]
    assess(s["lat"], s["lng"], src, [])
    first = len(t.calls)
    assess(s["lat"], s["lng"], src, [])
    assert len(t.calls) == first                     # second lookup served from cache
    m = src.fema.stats.as_dict()
    assert m["calls"] == 2 and m["cache_hits"] == 1 and m["errors"] == 0 and m["avg_ms"] is not None


def test_metrics_count_errors_and_retries():
    src = Sources(Recorded(fail="gis1.wim.usgs.gov"), sleep=lambda x: None)
    s = SPOTS["cranston"]
    assess(s["lat"], s["lng"], src, [])
    m = src.cbrs.stats.as_dict()
    assert m["errors"] == 1 and m["retries"] == 2 and m["last_error"] == "HTTP 503" and m["error_rate"] == 1.0


def test_unknown_year_is_rejected():
    with pytest.raises(ValueError, match="year must be one of"):
        report("cranston", year="2060")
