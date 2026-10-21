import copy
import json

import pytest

from floodrules import cli
from floodrules.claude import ModelError
from floodrules.extract import INSTRUCTION, SYSTEM, OllamaExtractor, ReplayExtractor, make_extractor, run_extraction
from floodrules.ollama import MAX_CTX, Ollama, OllamaError, context_size
from floodrules.pdftext import read_pdf

from conftest import TOOL_INPUT


class FakeOllama:
    def __init__(self, reply=None, status=200):
        self.calls = []
        self.reply, self.status = reply, status

    def __call__(self, url, body):
        self.calls.append((url, body))
        if self.reply is not None:
            return self.status, self.reply
        return 200, {"message": {"role": "assistant", "content": json.dumps(copy.deepcopy(TOOL_INPUT))},
                     "done_reason": "stop", "prompt_eval_count": 3100, "eval_count": 850, "total_duration": 41_500_000_000}


def extractor(fake, **kw):
    return OllamaExtractor(Ollama("qwen2.5:14b", transport=fake, **kw))


def test_request_uses_schema_format_and_sized_context(guide_pdf):
    fake = FakeOllama()
    run = run_extraction(read_pdf(guide_pdf), extractor(fake))
    url, body = fake.calls[0]
    assert url == "http://localhost:11434/api/chat"
    assert body["stream"] is False and body["format"]["type"] == "object"
    assert body["options"]["temperature"] == 0
    assert body["options"]["num_ctx"] >= 8192
    assert '<page number="2">' in body["messages"][1]["content"]
    assert "record_eligibility_rules" not in body["messages"][0]["content"]   # no tool in this path
    assert run.model == "ollama:qwen2.5:14b"
    assert run.report.kept == 7 and len(run.report.rejected) == 1
    assert run.cost_usd == 0.0 and run.usage.latency_s == 41.5


def test_prompt_wording_was_adapted():
    assert "tool exactly once" in SYSTEM and "by calling record_eligibility_rules" in INSTRUCTION


def test_context_grows_with_document():
    assert context_size(1_000) == 16384   # 8K output reservation + prompt
    assert context_size(100_000) == 65536
    assert context_size(10_000_000) == MAX_CTX


def test_truncated_prompt_is_an_error_not_a_silent_bad_score(guide_pdf):
    fake = FakeOllama(reply={"message": {"content": "{}"}, "done_reason": "stop", "prompt_eval_count": 4096})
    with pytest.raises(OllamaError, match="truncated"):
        run_extraction(read_pdf(guide_pdf), extractor(fake, num_ctx=4096))


def test_output_cut_off_is_reported(guide_pdf):
    fake = FakeOllama(reply={"message": {"content": '{"products": ['}, "done_reason": "length", "prompt_eval_count": 3000})
    with pytest.raises(OllamaError, match="token limit"):
        run_extraction(read_pdf(guide_pdf), extractor(fake))


def test_missing_model_says_how_to_pull(guide_pdf):
    fake = FakeOllama(reply={"error": "model 'qwen2.5:14b' not found"}, status=404)
    with pytest.raises(OllamaError, match="ollama pull qwen2.5:14b"):
        run_extraction(read_pdf(guide_pdf), extractor(fake))


def test_invalid_json_is_reported(guide_pdf):
    fake = FakeOllama(reply={"message": {"content": "Sure! Here are the rules"}, "done_reason": "stop", "prompt_eval_count": 3000})
    with pytest.raises(OllamaError, match="invalid JSON"):
        run_extraction(read_pdf(guide_pdf), extractor(fake))


def test_schema_violation_is_a_model_error(guide_pdf):
    fake = FakeOllama(reply={"message": {"content": '{"products": [{"insurer": "X"}]}'}, "done_reason": "stop", "prompt_eval_count": 3000})
    with pytest.raises(ModelError, match="didn't match the schema"):
        run_extraction(read_pdf(guide_pdf), extractor(fake))


def test_server_not_running(guide_pdf, monkeypatch):
    import requests
    monkeypatch.setattr(requests, "post", lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError("refused")))
    with pytest.raises(OllamaError, match="ollama serve"):
        run_extraction(read_pdf(guide_pdf), OllamaExtractor(Ollama("llama3.1:8b")))


def test_host_from_environment(monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "192.168.1.20:11434")
    assert Ollama("m").host == "http://192.168.1.20:11434"


def test_make_extractor_routes_by_prefix(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    assert make_extractor("ollama:llama3.1:8b").model == "ollama:llama3.1:8b"
    assert make_extractor("claude-haiku-5-5").model == "claude-haiku-5-5"


def test_replay_cache_works_for_local_models(guide_pdf, tmp_path):
    doc = read_pdf(guide_pdf)
    live = run_extraction(doc, extractor(FakeOllama()), cache_dir=tmp_path)
    saved = list(tmp_path.glob("*.json"))
    assert len(saved) == 1 and ":" not in saved[0].name
    replay = run_extraction(doc, ReplayExtractor(tmp_path, "ollama:qwen2.5:14b"))
    assert replay.verified == live.verified


def test_eval_report_with_local_model(tmp_path, monkeypatch, guide_pdf, capsys):
    from conftest import LABELS
    (tmp_path / "data" / "docs").mkdir(parents=True)
    (tmp_path / "evals" / "labels").mkdir(parents=True)
    (tmp_path / "data" / "docs" / "h.pdf").write_bytes(guide_pdf.read_bytes())
    (tmp_path / "evals" / "labels" / "h.json").write_text(json.dumps(LABELS))
    sources = tmp_path / "sources.json"
    sources.write_text(json.dumps({"documents": [{"id": "h", "url": "u", "file": "data/docs/h.pdf", "labels": "evals/labels/h.json"}]}))
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    monkeypatch.setattr(cli, "CACHE", tmp_path / "cache")
    monkeypatch.setattr(cli, "RUNS", tmp_path / "runs")
    fake = FakeOllama()
    monkeypatch.setattr(cli, "make_extractor", lambda m: OllamaExtractor(Ollama(m.split(":", 1)[1], transport=fake)))
    assert cli.main(["--sources", str(sources), "eval", "--model", "ollama:qwen2.5:14b"]) == 0
    out = capsys.readouterr().out
    assert "| ollama:qwen2.5:14b |" in out and "$0 (local)" in out and "41.5s" in out
