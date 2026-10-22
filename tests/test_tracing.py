import threading

from test_property import SPOTS, Recorded

from floodrules import tracing
from floodrules.sources import Sources


def test_traceparent_is_continued_or_started():
    t = tracing.Trace("GET /x", "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01")
    assert t.trace_id == "4bf92f3577b34da6a3ce929d0e0e4736" and t.root.parent_id == "00f067aa0ba902b7"
    assert t.traceparent.startswith("00-4bf92f3577b34da6a3ce929d0e0e4736-") and t.traceparent.endswith("-01")
    fresh = tracing.Trace("GET /x", "garbage")
    assert len(fresh.trace_id) == 32 and fresh.root.parent_id is None
    assert tracing.parse_traceparent("00-" + "0" * 32 + "-00f067aa0ba902b7-01") is None


def test_spans_nest_and_record_errors():
    t = tracing.Trace("GET /x")
    with tracing.use(t):
        with tracing.span("outer", k=1) as outer:
            with tracing.span("inner"):
                pass
            try:
                with tracing.span("boom"):
                    raise ValueError("bad")
            except ValueError:
                pass
    d = tracing.finish(t)
    by = {s["name"]: s for s in d["spans"]}
    assert by["inner"]["parent_id"] == outer.span_id and by["outer"]["parent_id"] == t.root.span_id
    assert by["boom"]["error"] == "ValueError: bad" and by["outer"]["attrs"] == {"k": 1}
    assert all(s["ms"] is not None for s in d["spans"])


def test_span_outside_a_trace_is_a_no_op():
    with tracing.span("nothing") as s:
        assert s is None


def test_source_calls_become_spans_with_cache_hits_even_from_worker_threads():
    tracing.clear()
    sources = Sources(Recorded(), sleep=lambda x: None)
    spot = SPOTS["misquamicut"]
    t = tracing.Trace("GET /property")
    with tracing.use(t):
        sources.fema_zone(spot["lat"], spot["lng"])
        th = threading.Thread(target=lambda: sources.fema_zone(spot["lat"], spot["lng"]))   # no trace here
        th.start()
        th.join()
        sources.fema_zone(spot["lat"], spot["lng"])
    d = tracing.finish(t)
    fema = [s for s in d["spans"] if s["name"] == "fema_nfhl"]
    assert [s["attrs"]["cache_hit"] for s in fema] == [False, True]   # the bare thread's call isn't in this trace
    assert fema[0]["attrs"]["attempts"] == 1 and fema[0]["attrs"]["status"] == 200
    assert tracing.recent(5)[0]["trace_id"] == t.trace_id


def test_failed_source_call_records_attempts_and_outcome():
    def down(url, params, headers, timeout):
        return 503, {}
    sources = Sources(down, sleep=lambda x: None)
    t = tracing.Trace("GET /property")
    with tracing.use(t):
        try:
            sources.fema_zone(41.3, -71.8)
        except Exception:
            pass
    d = tracing.finish(t)
    fema = next(s for s in d["spans"] if s["name"] == "fema_nfhl")
    assert fema["attrs"]["attempts"] == 3 and fema["attrs"]["outcome"] == "HTTP 503" and "SourceError" in fema["error"]
    assert sources.fema.stats.as_dict()["p95_ms"] is not None


def test_polling_requests_without_source_calls_are_not_kept():
    tracing.clear()
    tracing.finish(tracing.Trace("GET /metrics"))
    assert tracing.recent() == []


def test_a_property_lookup_traces_every_source_under_one_span():
    from floodrules.property import assess
    spot = SPOTS["misquamicut"]
    t = tracing.Trace("GET /property")
    with tracing.use(t):
        assess(spot["lat"], spot["lng"], Sources(Recorded(), sleep=lambda x: None), [])
    d = tracing.finish(t)
    lookups = next(s for s in d["spans"] if s["name"] == "lookups")
    children = [s["name"] for s in d["spans"] if s["parent_id"] == lookups["span_id"]]
    assert sorted(set(children)) == ["fema_nfhl", "nominatim", "uri_stormtools", "usfws_cbrs"]
    assert children.count("uri_stormtools") == 6   # one call per sea-level scenario


def test_stormtools_scenarios_are_fetched_in_parallel():
    import time

    from floodrules.sources import STORM
    rec = Recorded()

    def slow(url, params, headers, timeout):
        if url == STORM:
            time.sleep(0.2)
        return rec(url, params, headers, timeout)

    spot = SPOTS["misquamicut"]
    start = time.monotonic()
    depths = Sources(slow, sleep=lambda x: None).storm_depths(spot["lat"], spot["lng"])
    assert depths == spot["depths"] and time.monotonic() - start < 0.8   # six 0.2 s calls, not 1.2 s in a row
