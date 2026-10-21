"""Extract eligibility rules from an insurer document with one strict tool call."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from .claude import Claude, ModelError, Usage
from .merge import merge
from .ollama import Ollama
from .pdftext import Document
from .repair import Dropped, repair
from .schema_export import tool_schema
from .schemas import Extraction
from .verify import Verifier, VerifyReport

PROMPT_VERSION = "2026-10-21.4"

SYSTEM = """You extract eligibility rules for private flood insurance programs from underwriting guides, \
brochures and broker comparison sheets, for a tool that tells homeowners which insurers might cover a property.

Rules:
- Record only what the document states. Leave a field empty or null when the document is silent. Never fill gaps from general knowledge.
- One product per private residential flood program. A comparison sheet can describe several. Skip the NFIP; it is not a private program. Skip commercial-only programs; when a document covers both, record only the residential program and its rules.
- Every rule needs evidence: an exact quote of under 25 words copied character-for-character from the document, and the page number from its <page> tag. Quotes are checked against the document and rules with quotes that don't match are discarded.
- Availability: use all_except with the excluded states when the document says "all states except", only_listed when it lists the states where the program is sold.
- Location exclusions are about where the building is (CBRA or Coastal Barrier zones, floodways, over water, islands, barrier islands). Property exclusions are about the building or its history (condos, manufactured or mobile homes, prior losses, severe repetitive loss, contents below grade, NFIP grandfathered rating).
- Zone conditions describe which FEMA zones are eligible and under what condition. Use only the letters A, V, B, C, X, D: map AE/AH/AO/AR/A99 to A, VE to V, and shaded or unshaded X to X.
- Limits: only fixed dollar maximums, as whole numbers of dollars (1250000, not "$1.25M"). Skip limits expressed as a percentage, and never calculate a dollar amount from a percentage; the quote must contain the amount. Kinds: dwelling (building coverage), contents (personal property), other_structures, loss_of_use (additional living expenses), total_insured_value, increased_cost_of_compliance.
- States are two-letter postal codes.
- If the text starts with an <excerpt> tag, it is part of a longer document. Record only what these pages state, and name each program as the pages do; results from all excerpts are merged afterwards.
Always answer by calling the record_eligibility_rules tool exactly once."""

INSTRUCTION = "Extract the eligibility rules for every private flood program in the document above by calling record_eligibility_rules."

TOOL_NAME = "record_eligibility_rules"
TOOL_DESCRIPTION = "Record the eligibility rules found in the document, one entry per private flood program."
SCHEMA = tool_schema(Extraction)


@dataclass
class ExtractionRun:
    document: str
    model: str
    prompt_version: str
    raw: dict
    extraction: Extraction      # as returned by the model, schema-validated
    verified: Extraction        # only rules whose quotes were found in the document
    report: VerifyReport
    usage: Usage
    cost_usd: float | None
    chunks: int = 1
    schema_dropped: list[Dropped] = field(default_factory=list)   # items that couldn't be made to fit the schema


class Extractor(Protocol):
    model: str

    def tool_input(self, doc: Document) -> tuple[dict, Usage]: ...


class ClaudeExtractor:
    saves_cache = True
    chunk_chars = int(os.environ.get("FLOODRULES_CHUNK_CHARS", "400000"))   # Claude reads whole guides in one call

    def __init__(self, client: Claude):
        self.client = client
        self.model = client.model

    def tool_input(self, doc: Document) -> tuple[dict, Usage]:
        res = self.client.call_tool(system=SYSTEM, document=doc.tagged(), instruction=INSTRUCTION, tool_name=TOOL_NAME,
                                    tool_description=TOOL_DESCRIPTION, input_schema=SCHEMA)
        return res.tool_input, res.usage


class OllamaExtractor:
    """Ollama Cloud or local model. Model names carry an `ollama:` prefix, e.g. `ollama:gemma4:31b`."""

    saves_cache = True
    # About 6-8K tokens of text per request keeps both the prompt and the JSON answer well inside limits.
    chunk_chars = int(os.environ.get("FLOODRULES_CHUNK_CHARS", "24000"))

    def __init__(self, client: Ollama):
        self.client = client
        self.model = f"ollama:{client.model}"

    def tool_input(self, doc: Document) -> tuple[dict, Usage]:
        return self.client.json_call(system=SYSTEM.replace("by calling the record_eligibility_rules tool exactly once", "with a single JSON object"),
                                     document=doc.tagged(), instruction=INSTRUCTION.replace("by calling record_eligibility_rules", "as JSON"),
                                     schema=SCHEMA)


def make_extractor(model: str) -> Extractor:
    """`ollama:<name>` runs locally through Ollama; anything else goes to the Claude API."""
    if model.startswith("ollama:"):
        return OllamaExtractor(Ollama(model.split(":", 1)[1]))
    return ClaudeExtractor(Claude(model=model))


class ReplayExtractor:
    """Replays saved model outputs so evals and CI run offline and for free."""

    def __init__(self, cache_dir: str | Path, model: str, run_index: int = 1):
        self.cache_dir = Path(cache_dir)
        self.model = model
        self.run_index = run_index

    def tool_input(self, doc: Document) -> tuple[dict, Usage]:
        path = cache_path(self.cache_dir, doc, self.model, self.run_index)
        if not path.exists():
            run = f" (run {self.run_index})" if self.run_index > 1 else ""
            raise FileNotFoundError(f"No saved response for {doc.name}{run} with {self.model}. Run once with the live API first.")
        saved = json.loads(path.read_text())
        return saved["tool_input"], Usage(**saved["usage"])


def cache_path(cache_dir: Path, doc: Document, model: str, run_index: int = 1) -> Path:
    digest = hashlib.sha256((doc.tagged() + PROMPT_VERSION).encode()).hexdigest()[:12]
    safe_model = model.replace(":", "_").replace("/", "_")   # ollama:qwen2.5:14b -> ollama_qwen2.5_14b
    run = f".run{run_index}" if run_index > 1 else ""        # repeated eval runs keep separate answers
    return Path(cache_dir) / f"{Path(doc.name).stem}.{safe_model}.{digest}{run}.json"


_COMMERCIAL = re.compile(r"\bcommercial\b", re.I)
_RESIDENTIAL = re.compile(r"\b(residential|home|homeowners?|dwelling|personal)\b", re.I)


def residential_only(extraction: Extraction) -> tuple[Extraction, list[Dropped]]:
    """Drop commercial-only programs. This tool answers questions about homes, and models don't
    reliably follow the prompt's instruction to skip them, so it's enforced in code."""
    kept, dropped = [], []
    for p in extraction.products:
        name = f"{p.insurer} {p.program}"
        if _COMMERCIAL.search(name) and not _RESIDENTIAL.search(name):
            dropped.append(Dropped(f"{p.insurer} / {p.program}", "product", p.program, "commercial-only program; this tool covers homes"))
        else:
            kept.append(p)
    return Extraction(products=kept), dropped


def run_extraction(doc: Document, extractor: Extractor, cache_dir: str | Path | None = None,
                   chunk_chars: int | None = None) -> ExtractionRun:
    """Extract one document, a few pages at a time when it's long.

    Each chunk is its own request (and its own cache entry), so a long guide can't overflow
    the context window or the answer's token budget. Chunk results are merged, then every
    rule is checked against the full document.
    """
    size = chunk_chars or getattr(extractor, "chunk_chars", None) or 10**9
    chunks = doc.chunks(size)
    raws, parts, usage = [], [], Usage()
    dropped: list[Dropped] = []
    for chunk in chunks:
        raw, u = extractor.tool_input(chunk)
        fixed, d = repair(raw)
        dropped += d
        try:
            parts.append(Extraction.model_validate(fixed))
        except ValidationError as e:   # repair() validates item by item, so this would be a bug in repair
            raise ModelError(f"Tool input didn't match the schema: {e.error_count()} errors, first: {e.errors()[0]['msg']}") from e
        raws.append(raw)
        usage = usage.add(u)
        if cache_dir is not None and getattr(extractor, "saves_cache", False):
            p = cache_path(Path(cache_dir), chunk, extractor.model, getattr(extractor, "run_index", 1))
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({"tool_input": raw, "usage": u.__dict__}, indent=1))

    extraction, filtered = residential_only(merge(parts))
    dropped += filtered
    verified, report = Verifier(doc).check(extraction)
    return ExtractionRun(document=doc.name, model=extractor.model, prompt_version=PROMPT_VERSION,
                         raw=raws[0] if len(raws) == 1 else {"chunks": raws},
                         extraction=extraction, verified=verified, report=report, usage=usage,
                         cost_usd=usage.cost_usd(extractor.model), chunks=len(chunks), schema_dropped=dropped)
