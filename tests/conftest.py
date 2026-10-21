"""Shared fixtures: a made-up insurer guide rendered as a real PDF, and a fake Claude API."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

ROOT = Path(__file__).resolve().parent.parent

# A fictional program, written for these tests (no real insurer's text).
PAGE_1 = [
    "Harborline Private Flood - Underwriting Guide",
    "Availability: offered in all states except Alaska, Hawaii and Louisiana.",
    "Harborline is written on a surplus lines basis through appointed agents.",
    "No elevation certificate is required to quote.",
    "Ineligible risks: buildings located in a FEMA designated floodway.",
    "Ineligible risks: properties within the Coastal Barrier Resources System.",
]
PAGE_2 = [
    "Flood zones: A and V zone risks are eligible only when elevated above the",
    "base flood elevation. B, C and X zones are eligible.",
    "Maximum dwelling limit is $1,500,000 per building.",
    "Manufactured homes are not eligible for this program.",
    "Waiting period: 10 days from the application date.",
]


def make_pdf(path: Path, pages: list[list[str]]) -> Path:
    c = canvas.Canvas(str(path), pagesize=letter)
    for lines in pages:
        y = 720
        for line in lines:
            c.drawString(72, y, line)
            y -= 18
        c.showPage()
    c.save()
    return path


@pytest.fixture
def guide_pdf(tmp_path) -> Path:
    return make_pdf(tmp_path / "harborline_2026.pdf", [PAGE_1, PAGE_2])


def ev(quote: str, page: int) -> dict:
    return {"quote": quote, "page": page}


# What a good model answer looks like, plus two deliberate faults:
#  - an invented island exclusion whose quote isn't in the document (must be rejected)
#  - a correct floodway rule cited on the wrong page (must be kept, page corrected)
TOOL_INPUT = {"products": [{
    "insurer": "Harborline", "program": "Harborline Private Flood",
    "availability": {"mode": "all_except", "states": ["AK", "HI", "LA"],
                     "evidence": ev("offered in all states except Alaska, Hawaii and Louisiana", 1)},
    "surplus_lines": True, "elevation_certificate_required": False, "waiting_period_days": 10,
    "location_exclusions": [
        {"kind": "floodway", "evidence": ev("buildings located in a FEMA designated floodway", 2)},
        {"kind": "cbrs", "evidence": ev("properties within the Coastal Barrier Resources System", 1)},
        {"kind": "island", "evidence": ev("homes on islands are not eligible", 2)},
    ],
    "property_exclusions": [{"kind": "manufactured_home", "detail": None,
                             "evidence": ev("Manufactured homes are not eligible for this program", 2)}],
    "zone_conditions": [
        {"zones": ["A", "V"], "rule": "eligible_if_elevated_above_bfe",
         "evidence": ev("A and V zone risks are eligible only when elevated above the base flood elevation", 2)},
        {"zones": ["B", "C", "X"], "rule": "eligible", "evidence": ev("B, C and X zones are eligible", 2)},
    ],
    "limits": [{"kind": "dwelling", "amount_usd": 1500000, "evidence": ev("Maximum dwelling limit is $1,500,000 per building", 2)}],
}]}

LABELS = {"document": "harborline_2026", "status": "test fixture", "products": [{
    "match": ["harborline"],
    "product": {
        "insurer": "Harborline", "program": "Harborline Private Flood",
        "availability": {"mode": "all_except", "states": ["AK", "HI", "LA"]},
        "surplus_lines": True, "elevation_certificate_required": False, "waiting_period_days": 10,
        "location_exclusions": [{"kind": "floodway"}, {"kind": "cbrs"}],
        "property_exclusions": [{"kind": "manufactured_home"}],
        "zone_conditions": [{"zones": ["A", "V"], "rule": "eligible_if_elevated_above_bfe"},
                            {"zones": ["B", "C", "X"], "rule": "eligible"}],
        "limits": [{"kind": "dwelling", "amount_usd": 1500000}],
    }}]}


class FakeAPI:
    """Stands in for the Messages API. Records requests; replies from a script."""

    def __init__(self, replies: list[tuple[int, dict, dict]] | None = None, tool_input: dict | None = None):
        self.requests: list[dict] = []
        self.replies = replies
        self.tool_input = tool_input or TOOL_INPUT

    def ok(self, cached: bool = False) -> tuple[int, dict, dict]:
        usage = {"input_tokens": 120, "output_tokens": 900,
                 "cache_creation_input_tokens": 0 if cached else 2400, "cache_read_input_tokens": 2400 if cached else 0}
        return 200, {"model": "claude-haiku-5-5", "stop_reason": "tool_use", "usage": usage,
                     "content": [{"type": "tool_use", "id": "t1", "name": "record_eligibility_rules",
                                  "input": copy.deepcopy(self.tool_input)}]}, {}

    def __call__(self, body: dict, headers: dict):
        self.requests.append({"body": body, "headers": headers})
        if self.replies:
            return self.replies.pop(0)
        return self.ok(cached=len(self.requests) > 1)


@pytest.fixture
def fake_api() -> FakeAPI:
    return FakeAPI()


@pytest.fixture
def labels_file(tmp_path) -> Path:
    p = tmp_path / "labels.json"
    p.write_text(json.dumps(LABELS))
    return p
