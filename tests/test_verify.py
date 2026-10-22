from floodrules.pdftext import Document, read_pdf
from floodrules.schemas import Extraction
from floodrules.verify import Verifier, normalize

from conftest import TOOL_INPUT


def test_normalize_handles_pdf_artifacts():
    assert normalize("Coastal Bar-\nrier  Resources") == normalize("coastal barrier resources")
    assert normalize("“over water” – no") == normalize('"over water" - no')


def test_verifier_keeps_real_quotes_and_rejects_invented_ones(guide_pdf):
    doc = read_pdf(guide_pdf)
    verified, report = Verifier(doc).check(Extraction.model_validate(TOOL_INPUT))
    p = verified.products[0]
    assert [x.kind for x in p.location_exclusions] == ["floodway", "cbrs"]
    assert len(report.rejected) == 1
    rej = report.rejected[0]
    assert rej.field == "location_exclusions" and "island" in rej.rule and rej.reason == "quote not found in document"


def test_verifier_corrects_wrong_page(guide_pdf):
    doc = read_pdf(guide_pdf)
    verified, report = Verifier(doc).check(Extraction.model_validate(TOOL_INPUT))
    floodway = verified.products[0].location_exclusions[0]
    assert floodway.evidence.page == 1
    assert report.page_corrected == 1


def test_quote_spanning_lines_is_found(guide_pdf):
    doc = read_pdf(guide_pdf)
    verified, report = Verifier(doc).check(Extraction.model_validate(TOOL_INPUT))
    zones = verified.products[0].zone_conditions
    assert len(zones) == 2  # the A/V quote spans two lines in the PDF


def test_missing_or_short_evidence_is_rejected():
    doc = Document(name="d", pages=["Floodway risks are ineligible for coverage."])
    data = {"products": [{"insurer": "X", "program": "X", "availability": {"mode": "not_stated"},
                          "location_exclusions": [{"kind": "floodway"},
                                                  {"kind": "cbrs", "evidence": {"quote": "ineligible", "page": 1}}]}]}
    verified, report = Verifier(doc).check(Extraction.model_validate(data))
    assert verified.products[0].location_exclusions == []
    assert {r.reason for r in report.rejected} >= {"no evidence quote", "quote too short to verify"}


def test_unverified_availability_falls_back_to_not_stated():
    doc = Document(name="d", pages=["Nothing about states here at all."])
    data = {"products": [{"insurer": "X", "program": "X",
                          "availability": {"mode": "only_listed", "states": ["RI"],
                                           "evidence": {"quote": "available only in Rhode Island", "page": 1}}}]}
    verified, report = Verifier(doc).check(Extraction.model_validate(data))
    assert verified.products[0].availability.mode == "not_stated"
    assert report.rejected[0].field == "availability"


def test_money_values_reads_common_formats():
    from floodrules.verify import money_values
    assert money_values("Dwelling up to $1,250,000; contents $875,000") == {1_250_000, 875_000}
    assert {2_500_000, 250_000, 30_000} <= money_values("TIV $2.5 million, ALE 250K, ICC 30,000")
    assert 4_000_000 in money_values("limit of $4M per building")


def test_computed_limit_amounts_are_rejected():
    doc = Document(name="d", pages=["Loss of use: 20% of the dwelling limit. Dwelling: up to $2,250,000."])
    data = {"products": [{"insurer": "X", "program": "X", "availability": {"mode": "not_stated"}, "limits": [
        {"kind": "loss_of_use", "amount_usd": 450000, "evidence": {"quote": "Loss of use: 20% of the dwelling limit", "page": 1}},
        {"kind": "dwelling", "amount_usd": 2250000, "evidence": {"quote": "Dwelling: up to $2,250,000", "page": 1}},
    ]}]}
    verified, report = Verifier(doc).check(Extraction.model_validate(data))
    assert [x.kind for x in verified.products[0].limits] == ["dwelling"]
    assert report.rejected[0].reason == "quote doesn't state $450,000"


def test_not_stated_availability_is_not_counted_as_a_rejection():
    doc = Document(name="d", pages=["Floodway risks are ineligible for coverage."])
    data = {"products": [{"insurer": "X", "program": "X", "availability": {"mode": "not_stated"}}]}
    _, report = Verifier(doc).check(Extraction.model_validate(data))
    assert report.rejected == [] and report.kept == 0


def test_rejection_reports_the_closest_passage(guide_pdf):
    doc = read_pdf(guide_pdf)
    v = Verifier(doc)
    near, score = v.closest("homes in a FEMA designated floodway")
    assert near is not None and "floodway" in near and 0.6 < score < 1.0
    assert v.closest("") == (None, None)


def test_table_cells_verify_when_they_name_the_claim():
    # a comparison table: one value per cell, as the real broker sheets are laid out
    doc = Document(name="d", pages=["Ineligible Condominiums Mobile/Manufactured homes Dwelling $1,250,000 Contents $ 875,000 ICC $30K Yes"])
    data = {"products": [{"insurer": "X", "program": "X", "availability": {"mode": "not_stated"},
                          "property_exclusions": [{"kind": "condo", "evidence": {"quote": "Condominiums", "page": 1}},
                                                  {"kind": "manufactured_home", "evidence": {"quote": "Mobile/Manufactured homes", "page": 1}},
                                                  {"kind": "prior_losses", "evidence": {"quote": "Yes", "page": 1}}],
                          "limits": [{"kind": "dwelling", "amount_usd": 1250000, "evidence": {"quote": "$1,250,000", "page": 1}},
                                     {"kind": "contents", "amount_usd": 875000, "evidence": {"quote": "$875,000", "page": 1}},
                                     {"kind": "increased_cost_of_compliance", "amount_usd": 30000, "evidence": {"quote": "ICC $30K", "page": 1}},
                                     {"kind": "other_structures", "amount_usd": 125000, "evidence": {"quote": "$1,250,000", "page": 1}}]}]}
    verified, report = Verifier(doc).check(Extraction.model_validate(data))
    p = verified.products[0]
    assert [x.kind for x in p.property_exclusions] == ["condo", "manufactured_home"]
    assert [x.amount_usd for x in p.limits] == [1250000, 875000, 30000]
    # "Yes" doesn't name a prior-loss rule, and a cell stating a different amount doesn't verify this limit
    assert [r.reason for r in report.rejected] == ["quote too short to verify"] * 2


def test_short_quote_naming_the_claim_must_still_be_in_the_document():
    doc = Document(name="d", pages=["Dwelling $1,000,000"])
    data = {"products": [{"insurer": "X", "program": "X", "availability": {"mode": "not_stated"},
                          "limits": [{"kind": "dwelling", "amount_usd": 4000000, "evidence": {"quote": "$4,000,000", "page": 1}}]}]}
    verified, report = Verifier(doc).check(Extraction.model_validate(data))
    assert verified.products[0].limits == [] and report.rejected[0].reason == "quote not found in document"
