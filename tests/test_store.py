import json

from floodrules import cli, store
from floodrules.claude import Claude
from floodrules.extract import ClaudeExtractor, run_extraction
from floodrules.pdftext import read_pdf


def test_save_and_load_keep_provenance(fake_api, guide_pdf, tmp_path):
    run = run_extraction(read_pdf(guide_pdf), ClaudeExtractor(Claude(transport=fake_api)))
    path = store.save(run, "https://example.com/g.pdf", rules_dir=tmp_path)
    saved = json.loads(path.read_text())
    assert saved["verification"] == {"kept": 7, "rejected": 1, "page_corrected": 1}
    assert saved["extracted"]["model"] == "claude-haiku-5-5"
    (prov, product), = store.load_all(tmp_path)
    assert prov["source"]["url"] == "https://example.com/g.pdf"
    assert [x.kind for x in product.location_exclusions] == ["floodway", "cbrs"]   # only verified rules are stored


def test_newest_extraction_wins(fake_api, guide_pdf, tmp_path):
    run = run_extraction(read_pdf(guide_pdf), ClaudeExtractor(Claude(transport=fake_api)))
    old = json.loads(store.save(run, None, rules_dir=tmp_path).read_text())
    old["extracted"]["at"] = "2020-01-01T00:00:00+00:00"
    old["products"][0]["limits"] = []
    (tmp_path / "older_copy.json").write_text(json.dumps(old))
    (_, product), = store.load_all(tmp_path)
    assert product.limits and product.limits[0].amount_usd == 1500000


def test_cli_extract_prints_and_saves(fake_api, guide_pdf, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "CACHE", tmp_path / "cache")
    real = Claude
    monkeypatch.setattr(cli, "Claude", lambda model: real(model=model, transport=fake_api))
    monkeypatch.setattr(store.save, "__defaults__", (None, tmp_path / "rules"))
    assert cli.main(["extract", str(guide_pdf), "--save", "--url", "https://example.com/g.pdf"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["rejected"][0]["reason"] == "quote not found in document"
    assert list((tmp_path / "rules").glob("*.json"))


def test_cli_fetch_skips_existing_and_reports_failures(tmp_path, monkeypatch, capsys):
    (tmp_path / "data" / "docs").mkdir(parents=True)
    (tmp_path / "data" / "docs" / "have.pdf").write_bytes(b"%PDF-1.4")
    sources = tmp_path / "sources.json"
    sources.write_text(json.dumps({"documents": [
        {"id": "have", "url": "https://x/have.pdf", "file": "data/docs/have.pdf"},
        {"id": "blocked", "url": "https://x/blocked.pdf", "file": "data/docs/blocked.pdf"},
        {"id": "good", "url": "https://x/good.pdf", "file": "data/docs/good.pdf"}]}))
    monkeypatch.setattr(cli, "ROOT", tmp_path)

    class Resp:
        def __init__(self, code, content):
            self.status_code, self.content = code, content

    monkeypatch.setattr(cli.requests, "get", lambda url, timeout: Resp(403, b"") if "blocked" in url else Resp(200, b"%PDF-1.7 data"))
    assert cli.main(["--sources", str(sources), "fetch"]) == 0
    out = capsys.readouterr().out
    assert "have  have.pdf" in out and "FAIL  blocked" in out and "saved good.pdf" in out
    assert (tmp_path / "data" / "docs" / "good.pdf").read_bytes().startswith(b"%PDF")
