import io
from datetime import date

import pytest

from floodrules import costs


def pol(cost, zone="AE", geo="440090501001", day="2026-06-01", occ=11, condo="N", term=1, town="WESTERLY, TOWN OF", cancelled=None):
    return {"policyEffectiveDate": f"{day}T00:00:00.000Z", "policyCost": cost, "totalInsurancePremiumOfThePolicy": cost - 300,
            "ratedFloodZone": zone, "censusGeoid": geo, "occupancyType": occ, "condominiumCoverageTypeCode": condo,
            "policyTermIndicator": term, "nfipCommunityName": town, "cancellationDateOfFloodPolicy": cancelled,
            "asOfDate": "2026-07-10T00:00:00.000Z"}


def test_zone_group():
    assert costs.zone_group("AE") == "high"
    assert costs.zone_group("V14") == "high"
    assert costs.zone_group("X") == "other"
    assert costs.zone_group("A99") == "other"
    assert costs.zone_group("") is None


def test_town_name():
    assert costs.town_name("SOUTH KINGSTOWN, TOWN OF") == "south kingstown"
    assert costs.town_name("PROVIDENCE, CITY OF") == "providence"
    assert costs.town_name(None) is None


def test_keep_filters_non_single_family_condos_and_refunds():
    assert costs.keep(pol(1000))
    assert not costs.keep(pol(1000, occ=18))
    assert not costs.keep(pol(1000, condo="U"))
    assert not costs.keep(pol(1000, term=9))
    assert not costs.keep(pol(-50))
    assert not costs.keep(pol(1000, cancelled="2026-02-01"))


def test_stats():
    s = costs.stats([100, 200, 300, 400, 1000])
    assert s["n"] == 5 and s["median"] == 300 and s["mean"] == 400
    assert s["p25"] <= s["median"] <= s["p75"]
    assert costs.stats([500]) == {"n": 1, "median": 500, "mean": 500, "p25": 500, "p75": 500}


def test_summarize_flood_groups_and_hides_small_groups():
    rows = [pol(1000 + i * 100) for i in range(12)]                      # tract 44009050100, high: 12 policies
    rows += [pol(800, zone="X") for _ in range(3)]                        # same tract, other zone: too few
    rows += [pol(5000, day="2024-01-01") for _ in range(20)]              # older than the 12-month window
    rows += [pol(900, occ=18) for _ in range(20)]                         # not single-family
    rows += [pol(9000, day="2026-07-15") for _ in range(20)]              # snapshot month (partial) and renewals booked ahead
    rows += [pol(9000, cancelled="2026-06-20") for _ in range(20)]        # cancelled
    out = costs.summarize_flood(rows, min_n=10, today=date(2026, 10, 21))
    # FEMA's snapshot is 2026-07-10, so the window is the 12 complete months before July 2026
    assert out["window"] == {"from": "2025-07-01", "to": "2026-06-30"} and out["snapshot"] == "2026-07-10"
    assert out["policies"] == 15
    assert out["tract"]["44009050100|high"]["n"] == 12
    assert out["tract"]["44009050100|high"]["median"] == 1550
    assert "44009050100|other" not in out["tract"]
    assert out["town"]["westerly|high"]["n"] == 12
    assert out["county"]["44009|high"]["n"] == 12
    assert out["statewide"]["other"]["n"] == 3


def test_summarize_flood_fails_loudly_on_empty():
    with pytest.raises(ValueError):
        costs.summarize_flood([pol(1000, occ=18)])


def test_summarize_homeowners_takes_latest_year_and_pads_zip():
    rows = [(2891, 2021, 2900.4, 0.01), (2891, 2022, 3041.2, 0.0072), (1001, 2022, 1366, 0.01), (2840, 2022, None, None), ("ZIP", "x", 1, 1)]
    out = costs.summarize_homeowners(rows, 2801, 2940)
    assert out["year"] == 2022
    assert out["zip"] == {"02891": {"year": 2022, "avg": 3041, "nonrenewal_pct": 0.72}}


class FakeResp:
    def __init__(self, payload=None, content=b""):
        self._p, self.content = payload, content

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


class FakeSession:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def get(self, url, params=None, timeout=None):
        self.calls.append(params)
        return FakeResp({"NfipPolicies": self.pages[len(self.calls) - 1]})


def test_fetch_policies_pages_until_short_page():
    s = FakeSession([[{"id": 1}, {"id": 2}], [{"id": 3}]])
    got = list(costs.fetch_policies("RI", date(2025, 1, 1), session=s, page=2))
    assert [g["id"] for g in got] == [1, 2, 3]
    assert [c["$skip"] for c in s.calls] == [0, 2]
    assert "propertyState eq 'RI'" in s.calls[0]["$filter"] and "2025-01-01" in s.calls[0]["$filter"]


def test_fetch_homeowners_rows_reads_the_sheet():
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Supporting Underlying Metrics"
    ws.append(["ZIP Code", "Year", "Policy Decile Grouping", "Premiums Per Policy", "Nonrenewal Rate"])
    ws.append([2891, 2022, 5, 3041.2, 0.0072])
    buf = io.BytesIO()
    wb.save(buf)

    class S:
        def get(self, url, timeout=None):
            return FakeResp(content=buf.getvalue())

    assert costs.fetch_homeowners_rows(S()) == [(2891, 2022, 3041.2, 0.0072)]


def test_fetch_homeowners_rows_rejects_changed_layout():
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    wb.active.title = "Supporting Underlying Metrics"
    wb.active.append(["Zip", "Year"])
    buf = io.BytesIO()
    wb.save(buf)

    class S:
        def get(self, url, timeout=None):
            return FakeResp(content=buf.getvalue())

    with pytest.raises(ValueError, match="columns changed"):
        costs.fetch_homeowners_rows(S())


def test_check_flags_thin_or_implausible_data():
    good = {"flood": {"policies": 7000, "tract": {str(i): {} for i in range(100)}, "statewide": {"high": {"median": 1400}}},
            "homeowners": {"zip": {str(i): {} for i in range(70)}}}
    assert costs.check(good) == []
    bad = {"flood": {"policies": 50, "tract": {}, "statewide": {"high": {"median": 5}}}, "homeowners": {"zip": {}}}
    assert len(costs.check(bad)) == 4


def test_cli_costs_writes_file_or_refuses(tmp_path, monkeypatch, capsys):
    from floodrules import cli
    data = {"built_at": "x", "flood": {"policies": 7000, "window": {"from": "2025-10-22", "to": "2026-10-21"}, "tract": {str(i): {} for i in range(100)},
            "town": {}, "statewide": {"high": {"median": 1400, "mean": 1600, "n": 4500}}}, "homeowners": {"year": 2022, "zip": {str(i): {} for i in range(70)}}}
    monkeypatch.setattr(costs, "build", lambda state: data)
    out = tmp_path / "c.json"
    assert cli.main(["costs", "--out", str(out)]) == 0
    assert out.exists() and "statewide high: median $1,400" in capsys.readouterr().out
    data["flood"]["policies"] = 10
    out.unlink()
    assert cli.main(["costs", "--out", str(out)]) == 1
    assert not out.exists()


def test_summarize_homeowners_reports_the_national_nonrenewal_spread():
    rows = [(1000 + i, 2022, 1500, i / 1000) for i in range(100)] + [(2891, 2022, 3041, 0.0072), (2891, 2021, 2900, 0.5)]
    out = costs.summarize_homeowners(rows, 2801, 2940)
    nat = out["national_nonrenewal_pct"]
    assert nat["zips"] == 101 and 4.0 <= nat["median"] <= 5.5 and 8.5 <= nat["p90"] <= 9.5
