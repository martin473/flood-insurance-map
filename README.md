# flood-rules

Private flood insurers publish their eligibility rules in PDFs: underwriting guides, brochures, broker comparison sheets. This service reads those PDFs with Claude, turns the rules into typed data, throws out any rule it can't trace back to a quote in the document, and uses what's left to answer "which insurers might cover this property?"

It powers the insurance answers in the Rhode Island Flood Outlook map, replacing rules I first hard-coded by hand.

```
PDF ──► page text ──► Claude (one strict tool call) ──► typed rules ──► quote check ──► rules store ──► /coverage
                                                             │
                                             labeled ground truth ──► eval: precision, recall, cost, latency
```

## What's in it

| Piece | File | What it does |
|---|---|---|
| Schemas | `floodrules/schemas.py` | Pydantic models for availability by state, location exclusions (coastal barrier areas, floodways, islands...), building exclusions, flood-zone conditions and dollar limits. Every rule carries a quote and page number. |
| Extraction | `floodrules/extract.py`, `claude.py` | One Messages API call with a `strict` tool whose schema is generated from the Pydantic models. The document is marked for prompt caching. Retries on 429/5xx, honoring `retry-after`. |
| Verification | `floodrules/verify.py` | Drops any rule whose quote isn't in the document (after normalizing PDF artifacts like line-break hyphens and curly quotes), and fixes wrong page citations. |
| Coverage engine | `floodrules/engine.py` | Turns rules plus property facts (FEMA zone, floodway, coastal barrier unit, island) into available / maybe / not offered, with the quoted rule behind each answer. Unknown facts give "maybe", never a false "no". |
| API | `floodrules/api.py` | `POST /extract`, `POST /coverage`, `GET /rules` (FastAPI). |
| Evals | `floodrules/evaluation.py`, `cli.py`, `evals/` | Scores extraction against hand-labeled documents, per fact and per category, before and after the quote check, with cost and latency per model. |

## Quick start

```bash
pip install -e ".[dev]"
export ANTHROPIC_API_KEY=...

floodrules fetch                       # downloads the three eval PDFs into data/docs (not committed)
floodrules extract data/docs/msi_flood_2026.pdf --save
uvicorn floodrules.api:app --reload    # then POST /coverage

curl -s localhost:8000/coverage -H 'content-type: application/json' \
  -d '{"state":"RI","fema_zone":"VE","in_cbrs":false,"in_floodway":false,"on_island":true}'
```

## Evals

```bash
floodrules eval --model claude-haiku-5-5 --model claude-sonnet-5-5 --repeat 2
floodrules eval --replay               # rescore saved responses, no API calls
floodrules compare evals/runs/A.json evals/runs/B.json
```

Each product is flattened into atomic facts (`location:cbrs`, `state:all_except:AK`, `limit:dwelling:1250000`, `zone:V:eligible_if_elevated_above_bfe`...). Precision and recall are computed over those facts, overall and by category. Two scores are reported for every run:

- **Raw:** what the model returned.
- **Verified:** after dropping rules whose quote isn't in the document. The gap between the two shows how much the quote check protects precision, and what it costs in recall.

`--repeat 2` runs each document twice. The second run reads the document from the prompt cache, so the report shows the cost with and without caching. Results go to `evals/runs/<timestamp>.json` and `evals/runs/latest.md`.

**Eval set.** Three real documents with four programs: an RT Specialty comparison sheet (Hiscox FloodPlus and Neptune Flood, 2023), the Hiscox FloodPlus brochure (2024) and the MSI Residential Flood quick reference guide (2026). They cover a two-program comparison table, a marketing brochure and a dense one-pager.

> **The labels are drafts.** I wrote them from summaries of each PDF, not a line-by-line read, and each file says so. Check every fact against the document before quoting scores. Disagreements are the interesting part: write down which side was wrong.

**Results:** not run yet. This needs an API key and the downloaded PDFs; paste `evals/runs/latest.md` here after the first run.

## Design decisions

- **Strict tool with `tool_choice: auto`.** Claude Sonnet 5.5 and Opus 5.5 reject a forced `tool_choice`, so the tool is marked `strict` (inputs must match the schema) and the system prompt says to call it. A reply without the tool call raises an error instead of being parsed.
- **Schema from Pydantic, adapted for strict mode.** `schema_export.py` inlines `$ref`s, closes every object, marks every field required, and turns `Optional[X]` into a nullable type. Tests check every object in the schema.
- **Quotes over trust.** The model's output is treated as a claim. A rule only reaches the coverage engine if its quote is found in the document text. Scalar fields (`surplus_lines`, waiting period) aren't quote-checked yet.
- **Replay for CI.** Live runs save the raw tool input and usage, keyed by a hash of the document text and prompt version. `--replay` rescores those without calling the API, so prompt or scorer changes can be checked for free.
- **Documents stay local.** Insurer PDFs are copyrighted and some guidelines are licensed to agents, so `data/docs/` and the raw-response cache are git-ignored. Only facts (the labels and extracted rules) are committed.

## Tests

`pytest`: 56 tests covering schema export, PDF parsing, the quote verifier, the API client (request shape, retries, cost math), extraction end to end against a fake API, the scorer, the CLI, the rules store, and the coverage engine on five Rhode Island spots whose facts were pulled from FEMA and USFWS on 2026-10-21. Fixtures use a made-up insurer guide rendered to a real PDF, including one invented rule the verifier has to catch.

## Limitations

- Broker comparison sheets are tables, and PDF text extraction can scramble cell order, which hurts both extraction and quote matching. If recall is low on those, try `pdfplumber` table extraction or send the PDF to Claude as a document block.
- The engine can't check rules about the building itself (condo, past claims, built-before dates). It lists them under `not_checked`.
- Coverage answers say whether an insurer's published rules allow the location, not whether it will write the policy or at what price.
