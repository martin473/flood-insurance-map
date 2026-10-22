import pytest

fastapi = pytest.importorskip("fastapi")
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from floodrules import api, store  # noqa: E402
from floodrules.claude import Claude  # noqa: E402
from floodrules.extract import ClaudeExtractor  # noqa: E402

from conftest import FakeAPI  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent))  # lets the fixture import test_property


@pytest.fixture
def client(tmp_path, monkeypatch):
    rules_dir = tmp_path / "rules"
    monkeypatch.setattr(store, "DEFAULT_DIR", rules_dir)
    monkeypatch.setattr(store.load_all, "__defaults__", (rules_dir,))
    monkeypatch.setattr(store.save, "__defaults__", (None, rules_dir, None, None, None))
    monkeypatch.setattr(api, "make_extractor", lambda model: ClaudeExtractor(Claude(model=model, transport=FakeAPI())))
    return TestClient(api.app)


def test_extract_save_then_coverage(client, guide_pdf):
    r = client.post("/extract", files={"file": ("harborline.pdf", guide_pdf.read_bytes(), "application/pdf")},
                    data={"save": "true", "source_url": "https://example.com/guide.pdf"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["verification"] == {"kept": 7, "rejected": 1, "verified_rate": 0.875}
    assert body["saved_to"]

    assert client.get("/health").json() == {"ok": True, "programs_loaded": 1}
    rules = client.get("/rules").json()
    assert rules[0]["provenance"]["source"]["url"] == "https://example.com/guide.pdf"

    r = client.post("/coverage", json={"state": "RI", "fema_zone": "VE", "in_cbrs": False, "in_floodway": False})
    decisions = {d["insurer"]: d for d in r.json()["decisions"]}
    assert decisions["NFIP"]["status"] == "available"
    assert decisions["Harborline"]["status"] == "maybe"   # V zone: only if elevated above BFE
    assert decisions["Harborline"]["reasons"][0]["page"] == 2


def test_extract_rejects_non_pdf(client):
    r = client.post("/extract", files={"file": ("x.txt", b"hello", "text/plain")})
    assert r.status_code == 415


def test_coverage_validates_input(client):
    r = client.post("/coverage", json={"in_cbrs": "sometimes"})
    assert r.status_code == 422


@pytest.fixture
def live(client, monkeypatch):
    from test_property import Recorded

    from floodrules.sources import Sources
    monkeypatch.setattr(api, "sources", Sources(Recorded(), sleep=lambda x: None))
    api._hits.clear()
    return client


def test_property_endpoint(live):
    r = live.get("/property", params={"lat": 41.3260, "lng": -71.7920, "year": "2056"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["will_it_flood"]["answer"] == "Yes" and body["flood_insurance"]["answer"] == "Limited"
    assert body["lenders_require_flood_insurance"] is True
    assert [s["name"] for s in body["sources"]] == ["fema_nfhl", "usfws_cbrs", "uri_stormtools", "nominatim"]
    assert r.headers["x-request-id"]


def test_property_validates_input(live):
    assert live.get("/property", params={"lat": 40.7, "lng": -74.0}).status_code == 422      # New York
    assert live.get("/property", params={"lat": 41.5, "lng": -71.4, "year": "2060"}).status_code == 422
    assert live.get("/property", params={"lat": "abc", "lng": -71.4}).status_code == 422


def test_property_rate_limit(live, monkeypatch):
    monkeypatch.setattr(api, "RATE_LIMIT_PER_MIN", 2)
    p = {"lat": 41.7798, "lng": -71.4950}
    assert [live.get("/property", params=p).status_code for _ in range(3)] == [200, 200, 429]


def test_forwarded_for_only_trusted_behind_a_proxy(live, monkeypatch):
    monkeypatch.setattr(api, "RATE_LIMIT_PER_MIN", 1)
    p = {"lat": 41.7798, "lng": -71.4950}
    # without the proxy setting, a forged header doesn't buy a fresh quota
    assert live.get("/property", params=p, headers={"x-forwarded-for": "1.1.1.1"}).status_code == 200
    assert live.get("/property", params=p, headers={"x-forwarded-for": "2.2.2.2"}).status_code == 429
    monkeypatch.setenv("FLOODRULES_TRUST_PROXY", "1")
    api._hits.clear()
    assert live.get("/property", params=p, headers={"x-forwarded-for": "9.9.9.9, 3.3.3.3"}).status_code == 200
    assert live.get("/property", params=p, headers={"x-forwarded-for": "9.9.9.9, 4.4.4.4"}).status_code == 200


def test_metrics_endpoint(live):
    live.get("/property", params={"lat": 41.7798, "lng": -71.4950})
    m = live.get("/metrics").json()
    assert m["sources"]["fema_nfhl"]["calls"] >= 1 and m["sources"]["fema_nfhl"]["errors"] == 0
    assert any(k.startswith("/property 200") for k in m["requests"])


def test_extract_requires_admin_token_when_set(client, guide_pdf, monkeypatch):
    monkeypatch.setenv("FLOODRULES_ADMIN_TOKEN", "s3cret")
    files = {"file": ("g.pdf", guide_pdf.read_bytes(), "application/pdf")}
    assert client.post("/extract", files=files).status_code == 401
    assert client.post("/extract", files=files, headers={"authorization": "Bearer wrong"}).status_code == 401
    assert client.post("/extract", files=files, headers={"authorization": "Bearer s3cret"}).status_code == 200


def test_extract_fails_closed_when_token_required_but_unset(client, guide_pdf, monkeypatch):
    monkeypatch.delenv("FLOODRULES_ADMIN_TOKEN", raising=False)
    monkeypatch.setenv("FLOODRULES_REQUIRE_ADMIN", "1")
    r = client.post("/extract", files={"file": ("g.pdf", guide_pdf.read_bytes(), "application/pdf")})
    assert r.status_code == 503 and "FLOODRULES_ADMIN_TOKEN" in r.json()["detail"]


def test_traceparent_is_returned_and_continued(live):
    from floodrules import tracing
    tracing.clear()
    parent = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    r = live.get("/property", params={"lat": 41.3260, "lng": -71.7920}, headers={"traceparent": parent})
    assert r.status_code == 200
    assert r.headers["traceparent"].startswith("00-4bf92f3577b34da6a3ce929d0e0e4736-")
    traces = live.get("/traces", params={"path": "/property"}).json()
    assert traces[0]["trace_id"] == "4bf92f3577b34da6a3ce929d0e0e4736" and traces[0]["attrs"]["status"] == 200
    names = {s["name"] for s in traces[0]["spans"]}
    assert {"lookups", "fema_nfhl", "usfws_cbrs", "uri_stormtools", "nominatim"} <= names
    # polling the status endpoints doesn't push lookups out of the recent list
    live.get("/metrics")
    live.get("/traces")
    assert all(t["attrs"]["path"] == "/property" for t in live.get("/traces").json())


def test_metrics_report_percentiles_and_data_freshness(live):
    live.get("/property", params={"lat": 41.3260, "lng": -71.7920})
    m = live.get("/metrics").json()
    fema = m["sources"]["fema_nfhl"]
    assert fema["p50_ms"] is not None and fema["p95_ms"] is not None and fema["cache_hit_rate"] is not None
    assert m["data"]["costs"]["fema_snapshot"] and m["data"]["costs"]["window"]["from"]
    assert m["data"]["rules"] == []   # the test's rules folder is empty


def test_cached_lookup_meets_the_latency_budget(live):
    """With the sources answering from cache, /property must answer in under 2 seconds, so a slow
    change in our own code shows up in CI rather than on the map."""
    import time
    params = {"lat": 41.3260, "lng": -71.7920}
    assert live.get("/property", params=params).status_code == 200      # warms the cache
    start = time.monotonic()
    r = live.get("/property", params=params)
    elapsed = time.monotonic() - start
    assert r.status_code == 200 and elapsed < 2.0, f"cached lookup took {elapsed:.2f}s"
