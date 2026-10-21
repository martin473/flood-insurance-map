# flood-rules

Private flood insurers publish their eligibility rules in PDFs: underwriting guides, brochures, broker comparison sheets. This service reads those PDFs with a language model (through the Ollama API, or Claude), turns the rules into typed data, throws out any rule it can't trace back to a quote in the document, and uses what's left to answer "which insurers might cover this property?"

It powers the insurance answers in the Rhode Island Flood Outlook map, replacing rules I first hard-coded by hand.

```
PDF ──► page text ──► model (JSON schema) ──► typed rules ──► quote check ──► rules store ──► /coverage
                                                             │
                                             labeled ground truth ──► eval: precision, recall, cost, latency
```

## What's in it

| Piece | File | What it does |
|---|---|---|
| Schemas | `floodrules/schemas.py` | Pydantic models for availability by state, location exclusions (coastal barrier areas, floodways, islands...), building exclusions, flood-zone conditions and dollar limits. Every rule carries a quote and page number. |
| Extraction | `floodrules/extract.py`, `ollama.py`, `claude.py` | One model call constrained to a JSON Schema generated from the Pydantic models. **Ollama (cloud or local):** the schema goes in `format` and in the prompt, rate limits are retried, and a truncated prompt raises an error instead of producing a misleading score. **Claude:** a `strict` tool, with the document marked for prompt caching and retries on 429/5xx. |
| Verification | `floodrules/verify.py` | Drops any rule whose quote isn't in the document (after normalizing PDF artifacts like line-break hyphens and curly quotes), and fixes wrong page citations. |
| Coverage engine | `floodrules/engine.py` | Turns rules plus property facts (FEMA zone, floodway, coastal barrier unit, island) into available / maybe / not offered, with the quoted rule behind each answer. Unknown facts give "maybe", never a false "no". |
| API | `floodrules/api.py` | `POST /extract`, `POST /coverage`, `GET /rules` (FastAPI). |
| Evals | `floodrules/evaluation.py`, `cli.py`, `evals/` | Scores extraction against hand-labeled documents, per fact and per category, before and after the quote check, with cost and latency per model. |

## Quick start

Uses the [Ollama Cloud API](https://docs.ollama.com/cloud) by default. Model names with an `ollama:` prefix go to Ollama; anything else goes to the Claude API.

```bash
pip install -e ".[dev]"
export OLLAMA_API_KEY=...              # from your ollama.com account
floodrules models                      # lists the model names your key can use
export FLOODRULES_MODEL=ollama:gemma4:31b   # pick one from that list
# OLLAMA_HOST=http://localhost:11434   # to use a local Ollama server instead

floodrules fetch                       # downloads the three eval PDFs into data/docs (not committed)
floodrules extract data/docs/msi_flood_2026.pdf --save
uvicorn floodrules.api:app --reload    # then POST /coverage

curl -s localhost:8000/coverage -H 'content-type: application/json' \
  -d '{"state":"RI","fema_zone":"VE","in_cbrs":false,"in_floodway":false,"on_island":true}'
```

## Evals

```bash
floodrules eval --model ollama:gemma4:31b --model ollama:<another model>   # compare models
floodrules eval --model claude-haiku-5-5 --repeat 2                       # Claude, if you have a key
floodrules eval --replay               # rescore saved responses, no API calls
floodrules compare evals/runs/A.json evals/runs/B.json
```

Each product is flattened into atomic facts (`location:cbrs`, `state:all_except:AK`, `limit:dwelling:1250000`, `zone:V:eligible_if_elevated_above_bfe`...). Precision and recall are computed over those facts, overall and by category. Two scores are reported for every run:

- **Raw:** what the model returned.
- **Verified:** after dropping rules whose quote isn't in the document. The gap between the two shows how much the quote check protects precision, and what it costs in recall.

For Ollama models the report shows latency and token counts; there's no per-token cost. The comparison worth making is size against accuracy: does a bigger model beat a smaller one by enough to justify the slower run, and how many invented quotes does the verifier catch from each? Check your plan's usage limits on ollama.com before a large run. For Claude, `--repeat 2` runs each document twice; the second run reads the document from the prompt cache, so the report shows the cost with and without caching. Results go to `evals/runs/<timestamp>.json` and `evals/runs/latest.md`.

**Eval set.** Three real documents with four programs: an RT Specialty comparison sheet (Hiscox FloodPlus and Neptune Flood, 2023), the Hiscox FloodPlus brochure (2024) and the MSI Residential Flood quick reference guide (2026). They cover a two-program comparison table, a marketing brochure and a dense one-pager.

> **The labels are drafts.** I wrote them from summaries of each PDF, not a line-by-line read, and each file says so. Check every fact against the document before quoting scores. Disagreements are the interesting part: write down which side was wrong.

**Results:** not run yet. This needs an API key and the downloaded PDFs; paste `evals/runs/latest.md` here after the first run.

## Design decisions

- **One schema, two backends.** The Pydantic models generate one strict-mode JSON Schema. Ollama gets it as `format`, which constrains decoding; Claude gets it as a `strict` tool. Both paths go through the same validation, quote check and scoring, so results are comparable.
- **Long documents go a few pages at a time.** Each document is split into chunks of consecutive pages (about 24,000 characters each for Ollama; set `FLOODRULES_CHUNK_CHARS` to change it), each chunk is its own request with its real page numbers, and the results are merged by program. Every rule is then checked against the whole document, so a rule cited on the wrong page in one chunk is still verified. Reasoning models are run with thinking turned off, since hidden thinking tokens otherwise eat the answer's token budget.
- **Repair, then validate item by item.** Endpoints that don't enforce the schema get near-misses (zone `AE` for `A`, limit `building` for `dwelling`, state names for codes). `repair.py` maps known variants, then validates each rule separately and drops only the ones that still don't fit. Drops are listed in the eval report, so a model that writes sloppy JSON loses those rules, not the whole document.
- **No silent truncation.** A local Ollama drops the start of prompts longer than its context window without an error, so the client sizes `num_ctx` to the document. On any host, if the reported prompt tokens are far fewer than the document needs, the call fails loudly instead of scoring a model that never saw part of the guide.
- **`format` with a fallback.** Ollama's cloud docs don't say whether every endpoint and model honors `format`. If a request is rejected because of it, the client retries with the schema only in the prompt. The reply still has to parse and validate against the schema.
- **Strict tool with `tool_choice: auto` (Claude).** Claude Sonnet 5.5 and Opus 5.5 reject a forced `tool_choice`, so the tool is marked `strict` (inputs must match the schema) and the system prompt says to call it. A reply without the tool call raises an error instead of being parsed.
- **Schema from Pydantic, adapted for strict mode.** `schema_export.py` inlines `$ref`s, closes every object, marks every field required, and turns `Optional[X]` into a nullable type. Tests check every object in the schema.
- **Quotes over trust.** The model's output is treated as a claim. A rule only reaches the coverage engine if its quote is found in the document text. Scalar fields (`surplus_lines`, waiting period) aren't quote-checked yet.
- **Replay for CI.** Live runs save the raw tool input and usage, keyed by a hash of the document text and prompt version. `--replay` rescores those without calling the API, so prompt or scorer changes can be checked for free.
- **Documents stay local.** Insurer PDFs are copyrighted and some guidelines are licensed to agents, so `data/docs/` and the raw-response cache are git-ignored. Only facts (the labels and extracted rules) are committed.

## Tests

`pytest`: 93 tests covering schema export, PDF parsing, the quote verifier, both model clients (auth, host selection, `format` fallback, retries and truncation for Ollama; retries and cost math for Claude), extraction end to end against a fake API, the scorer, the CLI, the rules store, and the coverage engine on five Rhode Island spots whose facts were pulled from FEMA and USFWS on 2026-10-21. Fixtures use a made-up insurer guide rendered to a real PDF, including one invented rule the verifier has to catch.

## Limitations

- Broker comparison sheets are tables, and PDF text extraction can scramble cell order, which hurts both extraction and quote matching. If recall is low on those, try `pdfplumber` table extraction or send the PDF to Claude as a document block.
- The engine can't check rules about the building itself (condo, past claims, built-before dates). It lists them under `not_checked`.
- Coverage answers say whether an insurer's published rules allow the location, not whether it will write the policy or at what price.
