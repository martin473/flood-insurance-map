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
