import json

import pytest

from floodrules import cli
from floodrules.claude import Claude
from floodrules.evaluation import combine, facts, load_labels, score
from floodrules.extract import ClaudeExtractor
from floodrules.schemas import Extraction

from conftest import LABELS, TOOL_INPUT


def test_facts_flatten_every_rule_type(labels_file):
    f = facts(load_labels(labels_file)[0].product)
    assert {"availability:all_except", "state:all_except:LA", "location:cbrs", "property:manufactured_home",
            "zone:V:eligible_if_elevated_above_bfe", "limit:dwelling:1500000", "scalar:waiting_period_days:10"} <= f


def test_perfect_prediction_scores_one(labels_file):
    labels = load_labels(labels_file)
    s = score([labels[0].product], labels)
    assert s.overall.precision == 1 and s.overall.recall == 1 and s.products_matched == 1


def test_extra_and_missing_facts(labels_file):
    labels = load_labels(labels_file)
    pred = Extraction.model_validate(TOOL_INPUT).products  # includes the invented island rule
    s = score(pred, labels)
    assert s.overall.fp == 1 and s.overall.fn == 0
    assert s.false_positives == ["harborline: location:island"]

    trimmed = pred[0].model_copy(update={"limits": []})
    s2 = score([trimmed], labels)
    assert "harborline: limit:dwelling:1500000" in s2.misses
    assert s2.by_category["limit"].recall == 0


def test_unmatched_products_count_against_both_sides(labels_file):
    labels = load_labels(labels_file)
    wrong = labels[0].product.model_copy(update={"insurer": "Other Co", "program": "Other"})
    s = score([wrong], labels)
    assert s.products_matched == 0 and s.unmatched_predictions == ["Other Co / Other"]
    assert s.overall.tp == 0 and s.overall.fp == s.overall.fn > 0


def test_combine_adds_counts(labels_file):
    labels = load_labels(labels_file)
    a = score([labels[0].product], labels)
    t = combine([a, a])
    assert t.overall.tp == 2 * a.overall.tp and t.products_expected == 2


def test_repo_labels_parse():
    for name in ("rt_specialty_2023", "hiscox_floodplus_2024", "msi_flood_2026"):
        assert load_labels(cli.ROOT / "evals" / "labels" / f"{name}.json")


@pytest.fixture
def eval_env(tmp_path, monkeypatch, guide_pdf, fake_api):
    """A temporary repo layout with one document, so `floodrules eval` runs end to end."""
    (tmp_path / "data" / "docs").mkdir(parents=True)
    (tmp_path / "evals" / "labels").mkdir(parents=True)
    (tmp_path / "data" / "docs" / "harborline_2026.pdf").write_bytes(guide_pdf.read_bytes())
    (tmp_path / "evals" / "labels" / "harborline_2026.json").write_text(json.dumps(LABELS))
    sources = tmp_path / "evals" / "sources.json"
    sources.write_text(json.dumps({"documents": [{"id": "harborline_2026", "title": "t", "url": "u",
                                                  "file": "data/docs/harborline_2026.pdf",
                                                  "labels": "evals/labels/harborline_2026.json"}]}))
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    monkeypatch.setattr(cli, "CACHE", tmp_path / "evals" / "cache")
    monkeypatch.setattr(cli, "RUNS", tmp_path / "evals" / "runs")
    monkeypatch.setattr(cli, "make_extractor", lambda model: ClaudeExtractor(Claude(model=model, transport=fake_api)))
    return tmp_path, sources, fake_api


def test_eval_live_then_replay(eval_env, capsys):
    root, sources, api = eval_env
    assert cli.main(["--sources", str(sources), "eval", "--repeat", "2"]) == 0
    out = capsys.readouterr().out
    assert "| claude-haiku-5-5 |" in out and "missed" not in out
    run = json.loads(next((root / "evals" / "runs").glob("*.json")).read_text())[0]
    doc = run["documents"][0]
    assert doc["verified"]["overall"]["precision"] == 1.0   # verifier removed the invented rule
    assert doc["raw"]["overall"]["precision"] < 1.0
    assert run["cost"]["repeat_run"]["cache_read_tokens"] > 0
    assert run["cost"]["repeat_run"]["cost_usd"] < run["cost"]["first_run"]["cost_usd"]
    assert len(api.requests) == 2

    # Replay rescoring makes no API calls
    assert cli.main(["--sources", str(sources), "eval", "--replay"]) == 0
    assert len(api.requests) == 2
    assert "(replay)" in capsys.readouterr().out


def test_eval_skips_missing_documents(eval_env, capsys):
    root, sources, _ = eval_env
    (root / "data" / "docs" / "harborline_2026.pdf").unlink()
    assert cli.main(["--sources", str(sources), "eval"]) == 0
    assert "skipped" in capsys.readouterr().out


def test_compare_runs(eval_env, capsys, tmp_path):
    root, sources, _ = eval_env
    cli.main(["--sources", str(sources), "eval"])
    run = next((root / "evals" / "runs").glob("*.json"))
    capsys.readouterr()
    assert cli.main(["compare", str(run), str(run)]) == 0
    assert "precision 1.00 -> 1.00 (+0.00)" in capsys.readouterr().out


def test_repeated_runs_report_mean_and_range(eval_env, capsys, monkeypatch):
    import copy

    from floodrules.claude import Claude
    from floodrules.extract import ClaudeExtractor

    from conftest import FakeAPI
    root, sources, _ = eval_env
    full = copy.deepcopy(LABELS["products"][0]["product"])
    calls = {"n": 0}

    def answer():
        # run 2 misses the dwelling limit, so recall differs between runs
        calls["n"] += 1
        good = copy.deepcopy(TOOL_INPUT)
        if calls["n"] == 2:
            good["products"][0]["limits"] = []
        return good

    class VaryingAPI(FakeAPI):
        def ok(self, cached=False):
            self.tool_input = answer()
            return super().ok(cached)

    api = VaryingAPI()
    monkeypatch.setattr(cli, "make_extractor", lambda model: ClaudeExtractor(Claude(model=model, transport=api)))
    assert cli.main(["--sources", str(sources), "eval", "--runs", "3"]) == 0
    out = capsys.readouterr().out
    assert "over 3 runs" in out
    run = json.loads(next((root / "evals" / "runs").glob("*.json")).read_text())[0]
    rec = run["summary"]["verified_recall"]
    assert run["runs"] == 3 and rec["min"] < rec["max"] == 1.0
    assert rec["mean"] == round((1 + 1 + rec["min"]) / 3, 3)
    assert len(list((root / "evals" / "cache").glob("*.json"))) == 3   # one saved answer per run
    assert full  # labels untouched

    # each run's saved answers replay separately, reproducing the same spread for free
    capsys.readouterr()
    assert cli.main(["--sources", str(sources), "eval", "--runs", "3", "--replay"]) == 0
    replayed = json.loads(sorted((root / "evals" / "runs").glob("*.json"))[-1].read_text())[0]
    assert replayed["summary"]["verified_recall"] == rec and calls["n"] == 3
