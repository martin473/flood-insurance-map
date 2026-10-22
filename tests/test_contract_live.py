"""Contract tests against the live data services.

The rest of the suite runs on recorded responses, so it can't notice when FEMA renames a field,
URI reorders its rasters or the Census Bureau renumbers a layer. These tests call the real
services and fail when the formats this project reads change, or when the five recorded spots
stop matching what the services say today.

They only run with FLOODRULES_LIVE=1 (the nightly contract workflow sets it), never in the
normal test run: they need the network and the services are sometimes down for maintenance.
"""

import os
from datetime import datetime, timezone

import pytest
import requests
from test_property import SPOTS

from floodrules import costs
from floodrules.sources import CBRS, FEMA, STORM, USER_AGENT, Sources

pytestmark = [pytest.mark.live,
              pytest.mark.skipif(os.environ.get("FLOODRULES_LIVE") != "1", reason="live services: set FLOODRULES_LIVE=1")]

HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/json"}
FEMA_LAYER = FEMA.rsplit("/query", 1)[0]
CBRS_LAYER = CBRS.rsplit("/query", 1)[0]
STORM_SERVICE = STORM.rsplit("/getSamples", 1)[0]
TIGER = "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/tigerWMS_Current/MapServer"
OPENFEMA = "https://www.fema.gov/api/open"


def get(url, **params):
    r = requests.get(url, params=params, headers=HEADERS, timeout=60)
    r.raise_for_status()
    data = r.json()
    assert not (isinstance(data, dict) and data.get("error")), f"{url} answered with an error: {data['error']}"
    return data


def field_names(layer_url):
    return {f["name"] for f in get(layer_url, f="json").get("fields", [])}


def test_stormtools_raster_ids_still_mean_the_same_scenarios():
    data = get(f"{STORM_SERVICE}/query", where="OBJECTID<=6", outFields="OBJECTID,Name", returnGeometry="false", f="json")
    names = {f["attributes"]["OBJECTID"]: f["attributes"]["Name"] for f in data["features"]}
    assert names == {1: "All_TOTALDEPTH_SLR0", 2: "All_TOTALDEPTH_SLR2", 3: "All_TOTALDEPTH_SLR3",
                     4: "All_TOTALDEPTH_SLR5", 5: "All_TOTALDEPTH_SLR7", 6: "All_TOTALDEPTH_SLR10"}


def test_fema_flood_zone_layer_has_the_fields_we_read():
    assert {"FLD_ZONE", "SFHA_TF", "ZONE_SUBTY", "STATIC_BFE"} <= field_names(FEMA_LAYER)


def test_cbrs_layer_has_the_fields_we_read():
    assert {"Unit", "CBRS_Type", "FI_Date"} <= field_names(CBRS_LAYER)


def test_tigerweb_layer_numbers_the_map_uses():
    layers = {layer["id"]: layer["name"] for layer in get(TIGER, f="json")["layers"]}
    assert layers[8] == "Census Tracts"
    assert "ZIP Code Tabulation Areas" in layers[2]


def test_openfema_policy_fields_exist_and_the_snapshot_is_recent():
    flt = "openFemaDataSet eq 'NfipPolicies' and datasetVersion eq 3"
    fields = {f["name"] for f in get(f"{OPENFEMA}/v1/OpenFemaDataSetFields", **{"$filter": flt, "$top": 200})["OpenFemaDataSetFields"]}
    assert set(costs.FIELDS) <= fields, f"missing: {set(costs.FIELDS) - fields}"
    row = get(f"{OPENFEMA}/v3/NfipPolicies", **{"$filter": "propertyState eq 'RI'", "$select": "asOfDate", "$top": 1})["NfipPolicies"][0]
    snapshot = datetime.fromisoformat(row["asOfDate"].replace("Z", "+00:00"))
    age = (datetime.now(timezone.utc) - snapshot).days
    assert age <= 60, f"FEMA's policy data is {age} days old (snapshot {snapshot.date()}); the monthly refresh may have stopped"


def test_treasury_homeowners_spreadsheet_still_has_its_columns():
    pytest.importorskip("openpyxl")
    rows = costs.fetch_homeowners_rows()   # raises if the columns changed
    ri = costs.summarize_homeowners(rows, 2801, 2940)
    assert len(ri["zip"]) >= 50


@pytest.fixture(scope="module")
def live_sources():
    return Sources()


@pytest.mark.parametrize("name", SPOTS)
def test_recorded_spots_still_match_the_live_services(name, live_sources):
    """The fixtures the offline tests rely on, compared with today's answers. A flood map revision or
    a STORMTOOLS update shows up here first; re-record the fixture after checking the change is real."""
    s = SPOTS[name]
    zone = live_sources.fema_zone(s["lat"], s["lng"])
    assert zone["zone"] == s["fema"][0] and zone["sfha"] == (s["fema"][1] == "T")
    unit = live_sources.cbrs_unit(s["lat"], s["lng"])
    assert (unit["unit"] if unit else None) == (s["cbrs"][0] if s["cbrs"] else None)
    if unit:
        assert unit["flood_insurance_date"] == s["cbrs"][2]
    depths = live_sources.storm_depths(s["lat"], s["lng"])
    assert all(abs(a - b) <= 0.5 for a, b in zip(depths, s["depths"], strict=True)), f"live {depths} vs recorded {s['depths']}"
