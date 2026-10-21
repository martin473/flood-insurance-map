import pytest

from floodrules.claude import Claude, ClaudeError, ModelError, Usage
from floodrules.extract import ClaudeExtractor, ReplayExtractor, cache_path, run_extraction
from floodrules.pdftext import read_pdf

from conftest import FakeAPI


def test_request_shape(fake_api, guide_pdf):
    run_extraction(read_pdf(guide_pdf), ClaudeExtractor(Claude(transport=fake_api)))
    body = fake_api.requests[0]["body"]
    tool = body["tools"][0]
    assert tool["strict"] is True and tool["name"] == "record_eligibility_rules"
    assert body["tool_choice"] == {"type": "auto"}  # Sonnet/Opus 5.5 reject forced tool_choice
    doc_block = body["messages"][0]["content"][0]
    assert doc_block["cache_control"] == {"type": "ephemeral"}
    assert '<page number="2">' in doc_block["text"]
    assert fake_api.requests[0]["headers"]["anthropic-version"] == "2023-06-01"


def test_run_extraction_end_to_end(fake_api, guide_pdf, tmp_path):
    run = run_extraction(read_pdf(guide_pdf), ClaudeExtractor(Claude(transport=fake_api)), cache_dir=tmp_path)
    assert len(run.extraction.products[0].location_exclusions) == 3
    assert len(run.verified.products[0].location_exclusions) == 2
    assert run.report.kept == 7 and len(run.report.rejected) == 1
    assert run.cost_usd == pytest.approx((120 * 0.10 + 900 * 0.50 + 2400 * 0.125) / 1e6)
    assert len(list(tmp_path.glob("*.json"))) == 1


def test_replay_reproduces_saved_run(fake_api, guide_pdf, tmp_path):
    doc = read_pdf(guide_pdf)
    live = run_extraction(doc, ClaudeExtractor(Claude(transport=fake_api)), cache_dir=tmp_path)
    replay = run_extraction(doc, ReplayExtractor(tmp_path, "claude-haiku-5-5"))
    assert replay.verified == live.verified
    assert replay.usage == live.usage


def test_replay_without_cache_says_what_to_do(guide_pdf, tmp_path):
    with pytest.raises(FileNotFoundError, match="live API"):
        run_extraction(read_pdf(guide_pdf), ReplayExtractor(tmp_path, "claude-haiku-5-5"))


def test_cache_key_changes_with_document(guide_pdf, tmp_path):
    doc = read_pdf(guide_pdf)
    other = type(doc)(name=doc.name, pages=doc.pages + ["extra page"])
    assert cache_path(tmp_path, doc, "m") != cache_path(tmp_path, other, "m")


def test_retries_on_rate_limit_then_succeeds(guide_pdf):
    slept = []
    api = FakeAPI()
    api.replies = [(429, {"error": {"message": "rate limited"}}, {"retry-after": "3"}),
                   (529, {"error": {"message": "overloaded"}}, {}), api.ok()]
    run = run_extraction(read_pdf(guide_pdf), ClaudeExtractor(Claude(transport=api, sleep=slept.append)))
    assert slept == [3.0, 2] and len(api.requests) == 3
    assert run.verified.products


def test_client_errors_are_not_retried(guide_pdf):
    api = FakeAPI(replies=[(400, {"error": {"message": "bad schema"}}, {})])
    with pytest.raises(ClaudeError, match="400: bad schema"):
        run_extraction(read_pdf(guide_pdf), ClaudeExtractor(Claude(transport=api, sleep=lambda s: None)))
    assert len(api.requests) == 1


def test_answer_without_tool_call_is_an_error(guide_pdf):
    api = FakeAPI(replies=[(200, {"content": [{"type": "text", "text": "Here are the rules..."}],
                                  "stop_reason": "end_turn", "usage": {}}, {})])
    with pytest.raises(ClaudeError, match="without calling"):
        run_extraction(read_pdf(guide_pdf), ClaudeExtractor(Claude(transport=api)))


def test_schema_violations_are_reported(guide_pdf):
    api = FakeAPI(tool_input={"products": [{"insurer": "X"}]})
    with pytest.raises(ModelError, match="didn't match the schema"):
        run_extraction(read_pdf(guide_pdf), ClaudeExtractor(Claude(transport=api)))


def test_missing_api_key_is_explained(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ClaudeError, match="ANTHROPIC_API_KEY"):
        Claude()


def test_cost_accounting():
    u = Usage(input_tokens=1_000_000, output_tokens=100_000, cache_read_tokens=500_000, calls=1)
    assert u.cost_usd("claude-sonnet-5-5") == pytest.approx(2.0 + 1.0 + 0.05)
    assert u.cost_usd("unknown-model") is None
    long = Usage(input_tokens=150_000, output_tokens=0, calls=1)
    assert long.cost_usd("claude-haiku-5-5") == pytest.approx(150_000 * 0.50 / 1e6)  # >100K prompt rate
    assert Usage(calls=1).add(Usage(input_tokens=5, calls=1)).calls == 2
