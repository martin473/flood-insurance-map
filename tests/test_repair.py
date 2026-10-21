"""Near-miss values from models that don't enforce the schema are mapped or dropped one item at a time."""

from floodrules.extract import run_extraction
from floodrules.pdftext import read_pdf
from floodrules.repair import repair
from floodrules.schemas import Extraction

from conftest import TOOL_INPUT


def ev(q="quote words here", page=1):
    return {"quote": q, "page": page}


def test_values_seen_in_a_real_run_are_mapped():
    raw = {"products": [{
        "insurer": "MSI", "program": "Residential Flood",
        "availability": {"mode": "all_states_except", "states": ["Alaska", "hi", "KY", "Louisiana"], "evidence": ev()},
        "elevation_certificate_required": "no", "waiting_period_days": "5 days",
        "location_exclusions": [{"kind": "CBRA zone", "evidence": ev()}, {"kind": "Barrier Islands", "evidence": ev()}],
        "property_exclusions": [{"kind": "SRL", "evidence": ev()}, {"kind": "log cabins", "evidence": ev()}],
        "zone_conditions": [{"zones": ["AE", "VE", "Shaded X"], "rule": "eligible if elevated", "evidence": ev()}],
        "limits": [{"kind": "Building", "amount_usd": "$2.5 million", "evidence": ev()},
                   {"kind": "ICC", "amount_usd": "$30,000", "evidence": ev()}],
    }]}
    fixed, dropped = repair(raw)
    p = Extraction.model_validate(fixed).products[0]
    assert p.availability.mode == "all_except" and p.availability.states == ["AK", "HI", "KY", "LA"]
    assert p.elevation_certificate_required is False and p.waiting_period_days == 5
    assert [x.kind for x in p.location_exclusions] == ["cbrs", "barrier_island"]
    assert [x.kind for x in p.property_exclusions] == ["severe_repetitive_loss", "other"]
    assert p.property_exclusions[1].detail == "log cabins"
    assert p.zone_conditions[0].zones == ["A", "V", "X"] and p.zone_conditions[0].rule == "eligible_if_elevated_above_bfe"
    assert [(x.kind, x.amount_usd) for x in p.limits] == [("dwelling", 2_500_000), ("increased_cost_of_compliance", 30_000)]
    assert dropped == []


def test_only_the_bad_item_is_dropped():
    raw = {"products": [{
        "insurer": "X", "program": "Y", "availability": {"mode": "all_states"},
        "limits": [{"kind": "dwelling", "amount_usd": 1000000, "evidence": ev()},
                   {"kind": "boat", "amount_usd": 5, "evidence": ev()}],
        "zone_conditions": [{"zones": ["Q"], "rule": "eligible", "evidence": ev()}],
    }]}
    fixed, dropped = repair(raw)
    p = Extraction.model_validate(fixed).products[0]
    assert [x.kind for x in p.limits] == ["dwelling"]
    assert {d.field for d in dropped} == {"limits", "zone_conditions"}
    assert any("kind" in d.reason for d in dropped if d.field == "limits")


def test_unknown_states_are_removed_and_reported():
    fixed, dropped = repair({"products": [{"insurer": "X", "program": "Y",
                                           "availability": {"mode": "only_listed", "states": ["RI", "Narnia"]}}]})
    assert Extraction.model_validate(fixed).products[0].availability.states == ["RI"]
    assert dropped[0].field == "availability.states"


def test_products_without_names_and_non_lists():
    fixed, dropped = repair({"products": [{"insurer": "X"}, "junk"]})
    assert fixed == {"products": []} and len(dropped) == 2
    fixed, dropped = repair(["not", "an", "object"])
    assert fixed == {"products": []} and dropped[0].reason == "response has no products list"


def test_clean_input_passes_through_unchanged():
    fixed, dropped = repair(TOOL_INPUT)
    assert dropped == []
    assert Extraction.model_validate(fixed) == Extraction.model_validate(TOOL_INPUT)


def test_run_extraction_records_drops(guide_pdf, fake_api):
    from floodrules.claude import Claude
    from floodrules.extract import ClaudeExtractor
    bad = {"products": [{**TOOL_INPUT["products"][0], "limits": [{"kind": "yacht", "amount_usd": 1, "evidence": ev()}]}]}
    fake_api.tool_input = bad
    run = run_extraction(read_pdf(guide_pdf), ClaudeExtractor(Claude(transport=fake_api)))
    assert run.verified.products[0].limits == []
    assert run.schema_dropped[0].field == "limits"
    assert run.report.kept == 6   # everything else survived


def test_commercial_programs_are_filtered_out(guide_pdf):
    from floodrules.extract import residential_only

    def prod(program):
        return {"insurer": "Hiscox", "program": program, "availability": {"mode": "all_states"}}
    ext = Extraction.model_validate({"products": [prod("FloodPlus Commercial"), prod("FloodPlus"),
                                                  prod("FloodPlus Residential & Commercial"), prod("Commercial Home Flood")]})
    kept, dropped = residential_only(ext)
    assert [p.program for p in kept.products] == ["FloodPlus", "FloodPlus Residential & Commercial", "Commercial Home Flood"]
    assert dropped[0].value == "FloodPlus Commercial"
    assert "covers homes" in dropped[0].reason


def test_filter_applies_on_replay(guide_pdf, tmp_path, fake_api):
    import copy

    from floodrules.claude import Claude
    from floodrules.extract import ClaudeExtractor, ReplayExtractor
    commercial = copy.deepcopy(TOOL_INPUT["products"][0])
    commercial["program"] = "Harborline Commercial Flood"
    fake_api.tool_input = {"products": [TOOL_INPUT["products"][0], commercial]}
    doc = read_pdf(guide_pdf)
    run_extraction(doc, ClaudeExtractor(Claude(transport=fake_api)), cache_dir=tmp_path)
    replay = run_extraction(doc, ReplayExtractor(tmp_path, "claude-haiku-5-5"))
    assert [p.program for p in replay.verified.products] == ["Harborline Private Flood"]
    assert any(d.reason.startswith("commercial-only") for d in replay.schema_dropped)
