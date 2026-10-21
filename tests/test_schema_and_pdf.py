import pytest
from pydantic import ValidationError

from floodrules.pdftext import read_pdf
from floodrules.schema_export import tool_schema
from floodrules.schemas import Extraction, Product

from conftest import make_pdf


def walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from walk(v)


def test_tool_schema_is_strict_ready():
    s = tool_schema(Extraction)
    nodes = list(walk(s))
    assert not any("$ref" in n or "$defs" in n or "anyOf" in n for n in nodes)
    for n in nodes:
        if n.get("type") == "object" or (isinstance(n.get("type"), list) and "object" in n["type"]):
            assert n["additionalProperties"] is False
            assert set(n["required"]) == set(n.get("properties", {}))


def test_optional_fields_become_nullable():
    props = tool_schema(Extraction)["properties"]["products"]["items"]["properties"]
    assert props["surplus_lines"]["type"] == ["boolean", "null"]
    assert props["waiting_period_days"]["type"] == ["integer", "null"]
    ev = props["availability"]["properties"]["evidence"]
    assert ev["type"] == ["object", "null"]
    assert set(ev["properties"]) == {"quote", "page"}


def test_state_and_kind_enums_survive():
    item = tool_schema(Extraction)["properties"]["products"]["items"]
    states = item["properties"]["availability"]["properties"]["states"]["items"]["enum"]
    assert "RI" in states and len(states) == 51
    kinds = item["properties"]["location_exclusions"]["items"]["properties"]["kind"]["enum"]
    assert set(kinds) == {"cbrs", "floodway", "over_water", "island", "barrier_island"}


def test_product_rejects_unknown_fields_and_states():
    with pytest.raises(ValidationError):
        Product.model_validate({"insurer": "X", "program": "Y", "availability": {"mode": "all_states"}, "bogus": 1})
    with pytest.raises(ValidationError):
        Product.model_validate({"insurer": "X", "program": "Y", "availability": {"mode": "all_except", "states": ["ZZ"]}})


def test_read_pdf_pages_and_tags(guide_pdf):
    doc = read_pdf(guide_pdf)
    assert len(doc.pages) == 2
    assert "Coastal Barrier Resources System" in doc.pages[0]
    tagged = doc.tagged()
    assert '<page number="1">' in tagged and '<page number="2">' in tagged


def test_read_pdf_from_bytes(guide_pdf):
    doc = read_pdf(guide_pdf.read_bytes(), name="up.pdf")
    assert doc.name == "up.pdf" and len(doc.pages) == 2


def test_scanned_pdf_is_reported(tmp_path):
    blank = make_pdf(tmp_path / "scan.pdf", [[]])
    with pytest.raises(ValueError, match="OCR"):
        read_pdf(blank)
