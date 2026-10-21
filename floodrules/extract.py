"""Extract eligibility rules from an insurer document with one strict tool call."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from .claude import Claude, ModelError, Usage
from .ollama import Ollama
from .pdftext import Document
from .schema_export import tool_schema
from .schemas import Extraction
from .verify import Verifier, VerifyReport

PROMPT_VERSION = "2026-10-21.1"

SYSTEM = """You extract eligibility rules for private flood insurance programs from underwriting guides, \
brochures and broker comparison sheets, for a tool that tells homeowners which insurers might cover a property.

Rules:
- Record only what the document states. Leave a field empty or null when the document is silent. Never fill gaps from general knowledge.
- One product per private flood program. A comparison sheet can describe several. Skip the NFIP; it is not a private program.
- Every rule needs evidence: an exact quote of under 25 words copied character-for-character from the document, and the page number from its <page> tag. Quotes are checked against the document and rules with quotes that don't match are discarded.
- Availability: use all_except with the excluded states when the document says "all states except", only_listed when it lists the states where the program is sold.
- Location exclusions are about where the building is (CBRA or Coastal Barrier zones, floodways, over water, islands, barrier islands). Property exclusions are about the building or its history (condos, manufactured or mobile homes, prior losses, severe repetitive loss, contents below grade, NFIP grandfathered rating).
- Zone conditions describe which FEMA zones are eligible and under what condition. Map AE/AH/AO/AR/A99 to A and VE to V.
- Limits: only fixed dollar maximums. Skip limits expressed as a percentage.
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


class Extractor(Protocol):
    model: str

    def tool_input(self, doc: Document) -> tuple[dict, Usage]: ...


class ClaudeExtractor:
    saves_cache = True

    def __init__(self, client: Claude):
        self.client = client
        self.model = client.model

    def tool_input(self, doc: Document) -> tuple[dict, Usage]:
        res = self.client.call_tool(system=SYSTEM, document=doc.tagged(), instruction=INSTRUCTION, tool_name=TOOL_NAME,
                                    tool_description=TOOL_DESCRIPTION, input_schema=SCHEMA)
        return res.tool_input, res.usage


class OllamaExtractor:
    """Local model via Ollama. Model names carry an `ollama:` prefix, e.g. `ollama:qwen2.5:14b`."""

    saves_cache = True

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

    def __init__(self, cache_dir: str | Path, model: str):
        self.cache_dir = Path(cache_dir)
        self.model = model

    def tool_input(self, doc: Document) -> tuple[dict, Usage]:
        path = cache_path(self.cache_dir, doc, self.model)
        if not path.exists():
            raise FileNotFoundError(f"No saved response for {doc.name} with {self.model}. Run once with the live API first.")
        saved = json.loads(path.read_text())
        return saved["tool_input"], Usage(**saved["usage"])


def cache_path(cache_dir: Path, doc: Document, model: str) -> Path:
    digest = hashlib.sha256((doc.tagged() + PROMPT_VERSION).encode()).hexdigest()[:12]
    safe_model = model.replace(":", "_").replace("/", "_")   # ollama:qwen2.5:14b -> ollama_qwen2.5_14b
    return Path(cache_dir) / f"{Path(doc.name).stem}.{safe_model}.{digest}.json"


def run_extraction(doc: Document, extractor: Extractor, cache_dir: str | Path | None = None) -> ExtractionRun:
    raw, usage = extractor.tool_input(doc)
    try:
        extraction = Extraction.model_validate(raw)
    except ValidationError as e:
        raise ModelError(f"Tool input didn't match the schema: {e.error_count()} errors, first: {e.errors()[0]['msg']}") from e

    verified, report = Verifier(doc).check(extraction)
    if cache_dir is not None and getattr(extractor, "saves_cache", False):
        p = cache_path(Path(cache_dir), doc, extractor.model)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"tool_input": raw, "usage": usage.__dict__}, indent=1))
    return ExtractionRun(document=doc.name, model=extractor.model, prompt_version=PROMPT_VERSION, raw=raw,
                         extraction=extraction, verified=verified, report=report, usage=usage,
                         cost_usd=usage.cost_usd(extractor.model))
