"""The map page in a real browser (Chromium via Playwright), against recorded data.

Every request the page makes to the outside world is answered here: Leaflet from local copies
(tests/browser_assets), map tiles and overlay images with a blank PNG, and FEMA, USFWS, URI,
Census TIGERweb and Nominatim from the same recorded responses the API tests use. So the tests
are fast, repeatable and offline, and they exercise the page's own code: the three answers, the
"what neighbors paid" lines, the year slider, address search, and the fallback to direct
lookups when the API is down.

Two servers: a plain static one (the map opened without the API, as on GitHub) and the full
site app (map + API) when FastAPI is installed.

Runs when Playwright and Chromium are available: CI installs them in the browser job; locally,
`pip install playwright && playwright install chromium`, or set FLOODRULES_CHROMIUM to a binary.
"""

from __future__ import annotations

import functools
import json
import os
import socket
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

playwright_api = pytest.importorskip("playwright.sync_api")

from test_property import Recorded  # noqa: E402

from floodrules.sources import CBRS, FEMA, NOMINATIM, STORM, USER_AGENT  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
MAP_DIR = ROOT / "map"
ASSETS = Path(__file__).resolve().parent / "browser_assets"
PNG_1x1 = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                        "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082")
TIGER = "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/tigerWMS_Current/MapServer"

# census tract and ZIP for the recorded spots. Cranston's tract has no cost group of its own,
# so the page has to fall back to the town.
AREAS = {
    "misquamicut": ("44009051000", "02891"),
    "newport_point": ("44005041100", "02840"),
    "cranston": ("44007999999", "02910"),
}
SEARCH = {"cranston": {"lat": "41.7798", "lon": "-71.4950", "display_name": "Cranston City Hall, 869, Park Avenue, Cranston"}}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait(url: str) -> None:
    import urllib.request
    for _ in range(100):
        try:
            urllib.request.urlopen(url, timeout=1)
            return
        except Exception:
            time.sleep(0.1)
    raise RuntimeError(f"server at {url} didn't start")


@pytest.fixture(scope="module")
def browser():
    with playwright_api.sync_playwright() as p:
        exe = os.environ.get("FLOODRULES_CHROMIUM")
        try:
            b = p.chromium.launch(executable_path=exe) if exe else p.chromium.launch()
        except Exception as e:   # no browser installed
            if os.environ.get("FLOODRULES_REQUIRE_BROWSER") == "1":   # CI: a missing browser is a failure, not a skip
                pytest.fail(f"Chromium not available: {e}")
            pytest.skip(f"Chromium not available: {e}")
        yield b
        b.close()


@pytest.fixture(scope="module")
def static_url():
    """The map as a static site, with no API: what GitHub-hosted copies of the page see."""
    port = _free_port()
    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(MAP_DIR))
    handler.log_message = lambda *a: None
    srv = ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}"
    _wait(url + "/rhode-island-flood-map.html")
    yield url + "/rhode-island-flood-map.html"
    srv.shutdown()


@pytest.fixture(scope="module")
def site_url():
    """The full site app (map at /, API at /api), with the API's data sources on recorded responses."""
    pytest.importorskip("fastapi")
    uvicorn = pytest.importorskip("uvicorn")
    from floodrules import api, site
    from floodrules.sources import Sources
    api.sources = Sources(Recorded(), sleep=lambda x: None)
    api.RATE_LIMIT_PER_MIN = 10_000
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(site.app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    url = f"http://127.0.0.1:{port}"
    _wait(url + "/api/health")
    yield url
    server.should_exit = True


class Outside:
    """Answers every request that leaves the test server."""

    def __init__(self):
        self.recorded = Recorded()
        self.seen: list[str] = []

    def _jsonp(self, route, params, data):
        cb = params.get("callback", ["cb"])[0]
        route.fulfill(status=200, content_type="application/javascript", body=f"{cb}({json.dumps(data, default=str)});")

    def _recorded(self, url, params):
        flat = {k: v[0] for k, v in params.items()}
        if url == NOMINATIM:
            flat["lat"], flat["lon"] = float(flat["lat"]), float(flat["lon"])
        status, data = self.recorded(url, flat, {"User-Agent": USER_AGENT}, 5)
        return data

    def __call__(self, route):
        req = route.request
        u = urlsplit(req.url)
        base = f"{u.scheme}://{u.netloc}{u.path}"
        params = parse_qs(u.query)
        self.seen.append(base)
        if u.hostname in ("127.0.0.1", "localhost"):
            return route.continue_()
        if u.hostname == "unpkg.com":
            name = u.path.rsplit("/", 1)[-1]
            ctype = "text/css" if name.endswith(".css") else "application/javascript"
            return route.fulfill(status=200, content_type=ctype, body=(ASSETS / name).read_bytes())
        if u.hostname and u.hostname.startswith("fonts."):
            return route.fulfill(status=200, content_type="text/css", body="")
        if base in (FEMA, CBRS, STORM):
            return self._jsonp(route, params, self._recorded(base, params))
        if base.startswith(TIGER):
            spot = self._spot(params)
            tract, zcta = AREAS.get(spot, ("44000000000", "02999"))
            layer = base.split("/MapServer/")[1].split("/")[0]
            attrs = {"GEOID": tract} if layer == "8" else {"GEOID": zcta, "ZCTA5": zcta}
            return self._jsonp(route, params, {"features": [{"attributes": attrs}]})
        if base == NOMINATIM:
            return route.fulfill(status=200, content_type="application/json", body=json.dumps(self._recorded(base, params)))
        if base == "https://nominatim.openstreetmap.org/search":
            q = params.get("q", [""])[0].lower()
            hits = [v for k, v in SEARCH.items() if k in q]
            return route.fulfill(status=200, content_type="application/json", body=json.dumps(hits))
        # map tiles and overlay images (FEMA, URI, USFWS draw layers as images)
        return route.fulfill(status=200, content_type="image/png", body=PNG_1x1)

    @staticmethod
    def _spot(params):
        from test_property import SPOTS
        g = params.get("geometry", [""])[0]
        try:
            lng, lat = map(float, g.split(","))
        except ValueError:
            return None
        return next((k for k, s in SPOTS.items() if abs(s["lat"] - lat) < 0.01 and abs(s["lng"] - lng) < 0.01), None)


def open_page(browser, url, block_api=False):
    page = browser.new_page(viewport={"width": 1200, "height": 900})
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    outside = Outside()
    page.route("**/*", outside)
    if block_api:
        page.route("**/api/**", lambda route: route.fulfill(status=503, body="down"))
    page.goto(url)
    return page, errors, outside


def report_text(page, contains: str, timeout=15000) -> str:
    page.wait_for_function("t => document.getElementById('report').innerText.includes(t)", arg=contains, timeout=timeout)
    return page.inner_text("#report")


def answers(text: str) -> list[str]:
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    out = []
    for q in ("Will it flood here?", "Can it get flood insurance?", "Can it get homeowners insurance?"):
        out.append(lines[lines.index(q) + 1])
    return out


# ---------- the map without the API (direct lookups) ----------

def test_example_spot_answers_all_three_questions(browser, static_url):
    page, errors, outside = open_page(browser, static_url)
    text = report_text(page, "What neighbors paid")
    assert answers(text) == ["Yes", "Limited", "Yes"]
    assert "About 15.4 ft of water" in text and "by 2056" in text
    assert "median $1,533 a year" in text and "census tract, the high-risk flood zone" in text
    assert "ZIP 02891" in text and "0.72%" in text
    assert "Checked" not in text            # that footer only appears when the API answered
    assert errors == []


def test_year_slider_changes_the_answer(browser, static_url):
    page, errors, _ = open_page(browser, static_url)
    report_text(page, "What neighbors paid")
    page.eval_on_selector("#yr", "el => { el.value = 0; el.dispatchEvent(new Event('input')); }")
    text = report_text(page, "today.")
    assert page.inner_text("#year") == "Today" and page.inner_text("#rise") == "today's sea level"
    assert "About 12.0 ft of water" in text
    page.eval_on_selector("#yr", "el => { el.value = 5; el.dispatchEvent(new Event('input')); }")
    text = report_text(page, "10 ft of sea level rise")
    assert page.inner_text("#year") == "Worst case" and "About 29.1 ft" in text
    assert errors == []


def test_address_search_and_town_fallback_for_costs(browser, static_url):
    page, errors, _ = open_page(browser, static_url)
    report_text(page, "What neighbors paid")
    page.fill("#q", "Cranston City Hall, Cranston")
    page.click("#search button")
    text = report_text(page, "lower-risk flood zones")
    assert answers(text)[:2] == ["No", "Yes"]
    assert "in Cranston, lower-risk flood zones" in text      # no tract group, so the town's
    assert "ZIP 02910" in text
    assert errors == []


# ---------- the full site (map + API) ----------

def test_site_answers_through_the_api(browser, site_url):
    page, errors, outside = open_page(browser, site_url + "/")
    text = report_text(page, "Checked")
    assert answers(text) == ["Yes", "Limited", "Yes"]
    assert "median $1,533 a year" in text
    # the lookups went through the API, not straight from the browser to FEMA
    assert FEMA not in outside.seen
    assert errors == []


def test_site_falls_back_to_direct_lookups_when_the_api_fails(browser, site_url):
    page = browser.new_page()
    outside = Outside()
    page.route("**/*", outside)
    page.route("**/api/property*", lambda route: route.fulfill(status=503, body="down"))
    page.goto(site_url + "/")
    text = report_text(page, "What neighbors paid")
    assert answers(text) == ["Yes", "Limited", "Yes"] and "Checked" not in text
    assert FEMA in outside.seen


def test_status_page_shows_sources_and_recent_lookups(browser, site_url):
    page, errors, _ = open_page(browser, site_url + "/")
    report_text(page, "Checked")                     # one lookup through the API
    page.goto(site_url + "/status")
    page.wait_for_function("document.getElementById('banner').innerText.includes('answering')", timeout=15000)
    assert page.inner_text("#banner") == "All data sources are answering."
    assert page.locator("#lookups .track").count() >= 1
    assert page.locator("#lookups .bar").count() >= 9         # FEMA, CBRS, Nominatim and six STORMTOOLS calls
    assert "FEMA policy snapshot" in page.inner_text("#fresh")
    assert errors == []
