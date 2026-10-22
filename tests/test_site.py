"""The combined app the droplet runs: map at /, API at /api."""

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from floodrules.site import app  # noqa: E402


@pytest.fixture
def client():
    return TestClient(app)


def test_map_is_served_at_root(client):
    r = client.get("/")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    assert '<meta name="flood-api" content="/api">' in r.text


def test_api_is_mounted_under_api(client):
    assert client.get("/api/health").json()["ok"] is True
    assert "fema_nfhl" in client.get("/api/metrics").json()["sources"]
    assert client.get("/health").status_code == 404   # the API only lives under /api


def test_map_file_also_served_by_name(client):
    assert client.get("/rhode-island-flood-map.html").status_code == 200
    assert client.get("/missing.html").status_code == 404


def test_status_page_is_served(client):
    r = client.get("/status")
    assert r.status_code == 200 and "<title>Flood Outlook Status</title>" in r.text
    assert "fetch(API+\"/metrics\")" in r.text
