import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from floodrules import api, store  # noqa: E402
from floodrules.claude import Claude  # noqa: E402

from conftest import FakeAPI  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    rules_dir = tmp_path / "rules"
    monkeypatch.setattr(store, "DEFAULT_DIR", rules_dir)
    monkeypatch.setattr(store.load_all, "__defaults__", (rules_dir,))
    monkeypatch.setattr(store.save, "__defaults__", (None, rules_dir))
    real = Claude
    monkeypatch.setattr(api, "Claude", lambda model: real(model=model, transport=FakeAPI()))
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
