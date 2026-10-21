import copy
import json

from floodrules import cli, store
from floodrules.claude import Claude
from floodrules.evaluation import load_labels
from floodrules.extract import ClaudeExtractor, run_extraction
from floodrules.pdftext import read_pdf
from floodrules.publish import decision_facts, review
from floodrules.schemas import Product

from conftest import LABELS


def extracted(fake_api, guide_pdf):
    return run_extraction(read_pdf(guide_pdf), ClaudeExtractor(Claude(transport=fake_api)))


def test_decision_facts():
    p = Product.model_validate(LABELS["products"][0]["product"])
    # plain "eligible" zone rules restrict nothing, so they're left out
    assert decision_facts(p, "RI") == {"sold_in_RI:yes", "location:floodway", "location:cbrs", "zone:A:eligible_if_elevated_above_bfe",
                                       "zone:V:eligible_if_elevated_above_bfe"}
    assert "sold_in_HI:no" in decision_facts(p, "HI")


def test_review_publishes_a_program_that_matches_the_labels(fake_api, guide_pdf, labels_file):
    # the invented island exclusion was already dropped by the verifier, so what's left matches
    r = review(extracted(fake_api, guide_pdf).verified.products, load_labels(labels_file))
    assert [p.program for p in r.published] == ["Harborline Private Flood"] and r.held == []


def test_review_holds_back_a_program_missing_an_exclusion(fake_api, guide_pdf, tmp_path):
    labels = copy.deepcopy(LABELS)
    labels["products"][0]["product"]["location_exclusions"].append({"kind": "over_water"})
    (tmp_path / "l.json").write_text(json.dumps(labels))
    r = review(extracted(fake_api, guide_pdf).verified.products, load_labels(tmp_path / "l.json"))
    assert r.published == []
    assert r.held[0]["missing"] == ["location:over_water"] and r.held[0]["extra"] == []


def test_review_never_publishes_unlabeled_programs(fake_api, guide_pdf, tmp_path):
    labels = copy.deepcopy(LABELS)
    labels["products"][0]["match"] = ["neptune"]
    (tmp_path / "l.json").write_text(json.dumps(labels))
    r = review(extracted(fake_api, guide_pdf).verified.products, load_labels(tmp_path / "l.json"))
    assert r.published == []
    assert {h["why"] for h in r.held} == {"not extracted", "no reviewed label for this program"}


def test_store_merges_a_program_across_documents(fake_api, guide_pdf, tmp_path):
    run = extracted(fake_api, guide_pdf)
    old = json.loads(store.save(run, "https://x/old.pdf", rules_dir=tmp_path, published="2023-04").read_text())
    new = copy.deepcopy(old)
    new["source"] = {"document": "harborline_2025.pdf", "url": "https://x/new.pdf", "published": "2025-01"}
    new["products"][0]["waiting_period_days"] = 7
    new["products"][0]["location_exclusions"] = [{"kind": "over_water", "evidence": {"quote": "q", "page": 1}}]
    (tmp_path / "new.json").write_text(json.dumps(new))
    (prov, p), = store.load_all(tmp_path)
    assert prov["source"]["url"] == "https://x/new.pdf" and prov["also"][0]["url"] == "https://x/old.pdf"
    assert p.waiting_period_days == 7                                                     # newer document wins
    assert sorted(x.kind for x in p.location_exclusions) == ["cbrs", "floodway", "over_water"]   # exclusions from both


def test_cli_publish_rules_saves_reviewed_programs(fake_api, guide_pdf, labels_file, tmp_path, monkeypatch, capsys):
    sources = tmp_path / "sources.json"
    sources.write_text(json.dumps({"documents": [
        {"id": "harborline", "file": str(guide_pdf), "labels": str(labels_file), "url": "https://x/g.pdf", "published": "2026-01", "publish": True},
        {"id": "not_for_the_map", "file": str(guide_pdf), "labels": str(labels_file)}]}))
    monkeypatch.setattr(cli, "CACHE", tmp_path / "cache")
    monkeypatch.setattr(cli, "make_extractor", lambda model: ClaudeExtractor(Claude(model=model, transport=fake_api)))
    rules, report = tmp_path / "rules", tmp_path / "report.md"
    assert cli.main(["--sources", str(sources), "publish-rules", "--rules-dir", str(rules), "--report", str(report)]) == 0
    saved = json.loads((rules / "harborline.json").read_text())
    assert saved["source"]["published"] == "2026-01" and saved["review"]["published"] == ["Harborline / Harborline Private Flood"]
    assert "| harborline | Harborline Private Flood | none | claude-haiku-5-5 |" in report.read_text()


def test_cli_publish_rules_fails_when_nothing_passes(fake_api, guide_pdf, tmp_path, monkeypatch):
    labels = copy.deepcopy(LABELS)
    labels["products"][0]["product"]["availability"]["states"].append("RI")
    (tmp_path / "l.json").write_text(json.dumps(labels))
    sources = tmp_path / "sources.json"
    sources.write_text(json.dumps({"documents": [{"id": "h", "file": str(guide_pdf), "labels": str(tmp_path / "l.json"), "publish": True}]}))
    monkeypatch.setattr(cli, "CACHE", tmp_path / "cache")
    monkeypatch.setattr(cli, "make_extractor", lambda model: ClaudeExtractor(Claude(model=model, transport=fake_api)))
    assert cli.main(["--sources", str(sources), "publish-rules", "--rules-dir", str(tmp_path / "rules"), "--attempts", "2"]) == 1
    assert not (tmp_path / "rules").exists()


def test_brand_in_insurer_field_still_merges():
    from floodrules.merge import same_product
    base = {"availability": {"mode": "all_except", "states": ["AK", "HI"]}}
    a = Product.model_validate({**base, "insurer": "FloodPlus", "program": "FloodPlus"})
    b = Product.model_validate({**base, "insurer": "Hiscox", "program": "Hiscox FloodPlus Residential"})
    n = Product.model_validate({**base, "insurer": "Neptune Flood", "program": "Neptune Flood"})
    assert same_product(a, b) and same_product(b, a)
    assert not same_product(a, n) and not same_product(b, n)
