import copy
import json

import pytest

from floodrules import cli
from floodrules.claude import ModelError
from floodrules.extract import INSTRUCTION, SYSTEM, OllamaExtractor, ReplayExtractor, make_extractor, run_extraction
from floodrules.ollama import CLOUD_HOST, LOCAL_HOST, MAX_CTX, MAX_OUTPUT_TOKENS, Ollama, OllamaError, context_size
from floodrules.pdftext import read_pdf

from conftest import TOOL_INPUT


def good_reply(content=None, prompt_tokens=3100):
    return 200, {"message": {"role": "assistant", "content": content if content is not None else json.dumps(copy.deepcopy(TOOL_INPUT))},
                 "done_reason": "stop", "prompt_eval_count": prompt_tokens, "eval_count": 850, "total_duration": 41_500_000_000}, {}


class FakeOllama:
    """Stands in for /api/chat and /api/tags. Replies from a script, then with a good answer."""

    def __init__(self, replies=None):
        self.calls = []
        self.replies = list(replies or [])

    def __call__(self, method, url, body, headers):
        self.calls.append({"method": method, "url": url, "body": copy.deepcopy(body), "headers": headers})
        if self.replies:
            return self.replies.pop(0)
        if url.endswith("/api/tags"):
            return 200, {"models": [{"name": "gpt-oss:120b"}, {"name": "gemma4:31b"}]}, {}
        return good_reply()


def cloud(fake, **kw):
    return OllamaExtractor(Ollama("gemma4:31b", api_key="k-test", transport=fake, sleep=lambda s: None, **kw))


@pytest.fixture(autouse=True)
def no_env(monkeypatch):
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_HOST", raising=False)


def test_cloud_request(guide_pdf):
    fake = FakeOllama()
    run = run_extraction(read_pdf(guide_pdf), cloud(fake))
    call = fake.calls[0]
    assert call["url"] == "https://ollama.com/api/chat" and call["method"] == "POST"
    assert call["headers"]["authorization"] == "Bearer k-test"
    body = call["body"]
    assert body["stream"] is False and body["format"]["type"] == "object"
    assert body["options"]["temperature"] == 0
    assert "num_ctx" not in body["options"]                      # the cloud sets its own context
    assert '<page number="2">' in body["messages"][1]["content"]
    assert "JSON Schema" in body["messages"][1]["content"]       # schema is in the prompt too
    assert "record_eligibility_rules" not in body["messages"][0]["content"]
    assert run.model == "ollama:gemma4:31b"
    assert run.report.kept == 7 and len(run.report.rejected) == 1
    assert run.cost_usd == 0.0 and run.usage.latency_s == 41.5


def test_host_selection(monkeypatch):
    assert Ollama("m", api_key="k").host == CLOUD_HOST
    assert Ollama("m", api_key="").host == LOCAL_HOST
    monkeypatch.setenv("OLLAMA_API_KEY", "from-env")
    assert Ollama("m").host == CLOUD_HOST and Ollama("m").api_key == "from-env"
    monkeypatch.setenv("OLLAMA_HOST", "192.168.1.20:11434")
    assert Ollama("m").host == "http://192.168.1.20:11434"


def test_local_server_gets_sized_context_and_no_auth(guide_pdf):
    fake = FakeOllama()
    run_extraction(read_pdf(guide_pdf), OllamaExtractor(Ollama("llama3.1:8b", api_key="", transport=fake)))
    call = fake.calls[0]
    assert call["url"].startswith(LOCAL_HOST) and "authorization" not in call["headers"]
    assert call["body"]["options"]["num_ctx"] >= 16384


def test_prompt_wording_was_adapted():
    assert "tool exactly once" in SYSTEM and "by calling record_eligibility_rules" in INSTRUCTION


def test_context_grows_with_document():
    small = context_size(1_000)
    assert small >= 1_000 // 3 + MAX_OUTPUT_TOKENS and small & (small - 1) == 0   # room for output, power of two
    assert context_size(100_000) > small
    assert context_size(10_000_000) == MAX_CTX


def test_falls_back_when_format_is_rejected(guide_pdf):
    fenced = "```json\n" + json.dumps(TOOL_INPUT) + "\n```"
    fake = FakeOllama([(400, {"error": "unsupported field: format"}, {}), good_reply(content=fenced)])
    ext = cloud(fake)
    run = run_extraction(read_pdf(guide_pdf), ext)
    assert "format" in fake.calls[0]["body"] and "format" not in fake.calls[1]["body"]
    assert ext.client.used_format is False
    assert run.report.kept == 7   # same validation and quote check either way


def test_rate_limits_are_retried(guide_pdf):
    slept = []
    fake = FakeOllama([(429, {"error": "too many requests"}, {"retry-after": "4"}), (503, {"error": "busy"}, {})])
    ext = OllamaExtractor(Ollama("gemma4:31b", api_key="k", transport=fake, sleep=slept.append))
    run_extraction(read_pdf(guide_pdf), ext)
    assert slept == [4.0, 2] and len(fake.calls) == 3


def test_bad_key(guide_pdf):
    fake = FakeOllama([(401, {"error": "unauthorized"}, {})])
    with pytest.raises(OllamaError, match="OLLAMA_API_KEY"):
        run_extraction(read_pdf(guide_pdf), cloud(fake))


def test_unknown_cloud_model_points_to_models_command(guide_pdf):
    fake = FakeOllama([(404, {"error": "model 'qwen9' not found"}, {})])
    with pytest.raises(OllamaError, match="floodrules models"):
        run_extraction(read_pdf(guide_pdf), cloud(fake))


def test_unknown_local_model_says_how_to_pull(guide_pdf):
    fake = FakeOllama([(404, {"error": "model not found"}, {})])
    with pytest.raises(OllamaError, match="ollama pull llama3.1:8b"):
        run_extraction(read_pdf(guide_pdf), OllamaExtractor(Ollama("llama3.1:8b", api_key="", transport=fake)))


def test_truncation_is_an_error_not_a_silent_bad_score(guide_pdf):
    # cloud: far too few tokens counted for the prompt size
    with pytest.raises(OllamaError, match="truncated"):
        run_extraction(read_pdf(guide_pdf), cloud(FakeOllama([good_reply(prompt_tokens=300)])))
    # local: prompt filled the context window
    fake = FakeOllama([good_reply(prompt_tokens=8192)])
    with pytest.raises(OllamaError, match="truncated"):
        run_extraction(read_pdf(guide_pdf), OllamaExtractor(Ollama("m", api_key="", transport=fake, num_ctx=8192)))


def test_output_cut_off_is_reported(guide_pdf):
    reply = (200, {"message": {"content": '{"products": ['}, "done_reason": "length", "prompt_eval_count": 3000}, {})
    with pytest.raises(OllamaError, match="token limit"):
        run_extraction(read_pdf(guide_pdf), cloud(FakeOllama([reply])))


def test_invalid_json_and_schema_violations(guide_pdf):
    with pytest.raises(OllamaError, match="invalid JSON"):
        run_extraction(read_pdf(guide_pdf), cloud(FakeOllama([good_reply(content="Sure! Here are the rules")])))
    with pytest.raises(ModelError, match="didn't match the schema"):
        run_extraction(read_pdf(guide_pdf), cloud(FakeOllama([good_reply(content='{"products": [{"insurer": "X"}]}')])))


def test_server_errors_after_retries(guide_pdf):
    fake = FakeOllama([(500, {"error": "boom"}, {})] * 5)
    with pytest.raises(OllamaError, match="500: boom"):
        run_extraction(read_pdf(guide_pdf), cloud(fake))


def test_network_down(guide_pdf, monkeypatch):
    import requests

    def refuse(*a, **k):
        raise requests.ConnectionError("refused")
    monkeypatch.setattr(requests, "request", refuse)
    with pytest.raises(OllamaError, match="network connection"):
        run_extraction(read_pdf(guide_pdf), OllamaExtractor(Ollama("gemma4:31b", api_key="k")))
    with pytest.raises(OllamaError, match="ollama serve"):
        run_extraction(read_pdf(guide_pdf), OllamaExtractor(Ollama("llama3.1:8b", api_key="")))


def test_models_command(monkeypatch, capsys):
    fake = FakeOllama()
    import floodrules.ollama as ol
    monkeypatch.setattr(ol, "_requests_transport", fake)
    monkeypatch.setenv("OLLAMA_API_KEY", "k")
    assert cli.main(["models"]) == 0
    out = capsys.readouterr().out
    assert "2 models at https://ollama.com" in out and "ollama:gemma4:31b" in out
    assert fake.calls[0]["method"] == "GET" and fake.calls[0]["url"].endswith("/api/tags")


def test_make_extractor_routes_by_prefix(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    assert make_extractor("ollama:gemma4:31b").model == "ollama:gemma4:31b"
    assert make_extractor("claude-haiku-5-5").model == "claude-haiku-5-5"


def test_replay_cache_works_for_ollama_models(guide_pdf, tmp_path):
    doc = read_pdf(guide_pdf)
    live = run_extraction(doc, cloud(FakeOllama()), cache_dir=tmp_path)
    saved = list(tmp_path.glob("*.json"))
    assert len(saved) == 1 and ":" not in saved[0].name
    replay = run_extraction(doc, ReplayExtractor(tmp_path, "ollama:gemma4:31b"))
    assert replay.verified == live.verified


def test_eval_report_with_ollama_model(tmp_path, monkeypatch, guide_pdf, capsys):
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
    monkeypatch.setattr(cli, "make_extractor", lambda m: OllamaExtractor(Ollama(m.split(":", 1)[1], api_key="k", transport=fake)))
    assert cli.main(["--sources", str(sources), "eval", "--model", "ollama:gemma4:31b"]) == 0
    out = capsys.readouterr().out
    assert "| ollama:gemma4:31b |" in out and "no per-token cost" in out and "41.5s" in out


def test_thinking_is_turned_off(guide_pdf):
    fake = FakeOllama()
    ext = cloud(fake)
    run_extraction(read_pdf(guide_pdf), ext)
    assert fake.calls[0]["body"]["think"] is False and ext.client.used_think_off is True


def test_models_without_think_support_fall_back(guide_pdf):
    fake = FakeOllama([(400, {"error": '"gemma4:31b" does not support thinking'}, {})])
    ext = cloud(fake)
    run = run_extraction(read_pdf(guide_pdf), ext)
    assert "think" in fake.calls[0]["body"] and "think" not in fake.calls[1]["body"]
    assert "format" in fake.calls[1]["body"]          # only the rejected field is dropped
    assert ext.client.used_think_off is False and run.report.kept == 7


def test_both_fields_rejected_one_after_another(guide_pdf):
    fake = FakeOllama([(400, {"error": "think not supported"}, {}), (400, {"error": "format not supported"}, {})])
    run_extraction(read_pdf(guide_pdf), cloud(fake))
    assert len(fake.calls) == 3 and not {"think", "format"} & set(fake.calls[2]["body"])


def test_length_error_explains_thinking(guide_pdf):
    reply = (200, {"message": {"content": "", "thinking": "Let me consider each page... " * 3000},
                   "done_reason": "length", "prompt_eval_count": 3000}, {})
    with pytest.raises(OllamaError, match="tokens thinking"):
        run_extraction(read_pdf(guide_pdf), cloud(FakeOllama([reply])))


def test_eval_continues_past_a_failed_document(tmp_path, monkeypatch, guide_pdf, capsys):
    from conftest import LABELS
    (tmp_path / "data" / "docs").mkdir(parents=True)
    (tmp_path / "evals" / "labels").mkdir(parents=True)
    for name in ("a", "b"):
        (tmp_path / "data" / "docs" / f"{name}.pdf").write_bytes(guide_pdf.read_bytes())
    (tmp_path / "evals" / "labels" / "h.json").write_text(json.dumps(LABELS))
    sources = tmp_path / "sources.json"
    sources.write_text(json.dumps({"documents": [
        {"id": n, "url": "u", "file": f"data/docs/{n}.pdf", "labels": "evals/labels/h.json"} for n in ("a", "b")]}))
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    monkeypatch.setattr(cli, "CACHE", tmp_path / "cache")
    monkeypatch.setattr(cli, "RUNS", tmp_path / "runs")
    cut = (200, {"message": {"content": '{"products": ['}, "done_reason": "length", "prompt_eval_count": 3000}, {})
    fake = FakeOllama([cut])   # first document fails, second succeeds
    monkeypatch.setattr(cli, "make_extractor", lambda m: OllamaExtractor(Ollama(m.split(":", 1)[1], api_key="k", transport=fake)))
    assert cli.main(["--sources", str(sources), "eval", "--model", "ollama:gemma4:31b"]) == 0
    captured = capsys.readouterr()
    assert "a: failed" in captured.err and "**a** (2 pages): failed" in captured.out
    run = json.loads(next((tmp_path / "runs").glob("*.json")).read_text())[0]
    assert run["documents"][1]["verified"]["overall"]["recall"] == 1.0
    assert run["totals"]["verified"]["overall"]["recall"] == 0.5   # the failure counts as misses
