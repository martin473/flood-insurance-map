"""Long documents are extracted a few pages at a time and the results merged."""

import copy
import json

from floodrules.claude import Usage
from floodrules.extract import ReplayExtractor, run_extraction
from floodrules.merge import merge, same_product
from floodrules.pdftext import Document, read_pdf
from floodrules.schemas import Extraction, Product

from conftest import TOOL_INPUT


def p(insurer, program, **kw):
    return Product.model_validate({"insurer": insurer, "program": program, "availability": {"mode": "not_stated"}, **kw})


def test_chunks_keep_real_page_numbers():
    doc = Document(name="g.pdf", pages=["a" * 50, "b" * 50, "c" * 50, "d" * 50, "e" * 50])
    chunks = doc.chunks(110)
    assert [(c.first_page, c.last_page) for c in chunks] == [(1, 2), (3, 4), (5, 5)]
    t = chunks[1].tagged()
    assert t.startswith('<excerpt pages="3-4" of="5"/>') and '<page number="3">' in t and '<page number="1">' not in t


def test_short_document_is_one_chunk():
    doc = Document(name="g.pdf", pages=["short", "pages"])
    assert doc.chunks(10_000) == [doc] and "<excerpt" not in doc.tagged()


def test_oversized_page_stays_alone():
    doc = Document(name="g.pdf", pages=["x" * 500, "y" * 10])
    assert [len(c.pages) for c in doc.chunks(100)] == [1, 1]


class ChunkFake:
    """Answers each chunk with only the rules on its pages, like a real model would."""

    model = "fake"
    saves_cache = False

    def __init__(self):
        self.seen = []

    def tool_input(self, doc):
        self.seen.append((doc.first_page, doc.last_page))
        full = copy.deepcopy(TOOL_INPUT["products"][0])
        on_page = lambda items: [x for x in items if doc.first_page <= x["evidence"]["page"] <= doc.last_page]  # noqa: E731
        prod = {**full, "location_exclusions": on_page(full["location_exclusions"]),
                "property_exclusions": on_page(full["property_exclusions"]),
                "zone_conditions": on_page(full["zone_conditions"]), "limits": on_page(full["limits"])}
        if not (doc.first_page <= 1 <= doc.last_page):
            prod["availability"] = {"mode": "not_stated", "states": [], "evidence": None}
            prod["insurer"], prod["program"] = "Harborline", "Harborline Private Flood Program"   # named a bit differently
        return {"products": [prod]}, Usage(input_tokens=100, output_tokens=50, calls=1)


def test_chunked_run_matches_single_call(guide_pdf):
    doc = read_pdf(guide_pdf)
    fake = ChunkFake()
    run = run_extraction(doc, fake, chunk_chars=len(doc.pages[0]) + 1)
    assert fake.seen == [(1, 1), (2, 2)] and run.chunks == 2
    prod, = run.verified.products
    assert prod.availability.mode == "all_except"                 # from chunk 1
    # floodway came back from chunk 2 citing page 2, but its quote is on page 1; verifying against
    # the whole document (not the chunk) keeps it and corrects the page
    assert {x.kind for x in prod.location_exclusions} == {"cbrs", "floodway"}
    assert next(x for x in prod.location_exclusions if x.kind == "floodway").evidence.page == 1
    assert len(prod.zone_conditions) == 2 and prod.limits[0].amount_usd == 1500000   # from chunk 2
    assert prod.program == "Harborline Private Flood Program"      # fuller name kept
    assert run.usage.calls == 2 and run.usage.input_tokens == 200


def test_duplicate_rules_across_chunks_are_merged():
    rule = {"kind": "cbrs", "evidence": {"quote": "q one two three", "page": 1}}
    again = {"kind": "cbrs", "evidence": {"quote": "q four five six", "page": 4}}
    a = Extraction(products=[p("Hiscox", "FloodPlus", location_exclusions=[rule])])
    b = Extraction(products=[p("Hiscox London Market", "Hiscox FloodPlus", location_exclusions=[again], surplus_lines=True)])
    m = merge([a, b]).products
    assert len(m) == 1 and len(m[0].location_exclusions) == 1
    assert m[0].location_exclusions[0].evidence.page == 1 and m[0].surplus_lines is True


def test_different_insurers_are_not_merged():
    assert not same_product(p("Neptune", "Flood"), p("Hiscox", "Flood"))
    assert same_product(p("Hiscox", "FloodPlus"), p("Hiscox", "Hiscox FloodPlus Residential"))
    m = merge([Extraction(products=[p("Neptune", "Neptune Flood")]), Extraction(products=[p("MSI", "Residential Flood Marketplace")])])
    assert len(m.products) == 2


def test_each_chunk_is_cached_and_replayable(guide_pdf, tmp_path):
    from floodrules.extract import OllamaExtractor
    from floodrules.ollama import Ollama

    def transport(method, url, body, headers):
        return 200, {"message": {"content": json.dumps(TOOL_INPUT)}, "done_reason": "stop",
                     "prompt_eval_count": 2500, "eval_count": 300, "total_duration": 1_000_000_000}, {}
    doc = read_pdf(guide_pdf)
    ext = OllamaExtractor(Ollama("m", api_key="k", transport=transport))
    live = run_extraction(doc, ext, cache_dir=tmp_path, chunk_chars=len(doc.pages[0]) + 1)
    assert len(list(tmp_path.glob("*.json"))) == 2
    replay = run_extraction(doc, ReplayExtractor(tmp_path, "ollama:m"), chunk_chars=len(doc.pages[0]) + 1)
    assert replay.verified == live.verified and replay.chunks == 2
