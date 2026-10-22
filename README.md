# Flood insurance map

[![ci](https://github.com/martin473/flood-insurance-map/actions/workflows/ci.yml/badge.svg)](https://github.com/martin473/flood-insurance-map/actions/workflows/ci.yml)

Two parts:

- **`map/`**: the Rhode Island Flood Outlook, a single HTML page. Tap a property to see whether a major storm floods it as seas rise (URI STORMTOOLS), whether flood insurance is available and required, and whether homeowners insurance will be harder to get. Open it in a browser; it loads FEMA, URI, USFWS and NOAA data live.
- **`floodrules/`**: the Python service below, which extracts insurers' eligibility rules from their PDFs and powers the map's insurance answers.

## floodrules

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

**In CI:** Actions > eval > Run workflow, then enter one or more model names. It downloads the PDFs, runs the eval against Ollama Cloud, posts the results table on the run page, and saves the report and raw model answers as an artifact. Download the artifact into `evals/` to rescore it locally with `--replay`. It needs an `OLLAMA_API_KEY` repository secret (Settings > Secrets and variables > Actions).

**Locally:**

```bash
floodrules eval --model ollama:gemma4:31b --model ollama:<another model>   # compare models
floodrules eval --model claude-haiku-5-5 --repeat 2                       # Claude, if you have a key
floodrules eval --model ollama:nemotron-3-nano:30b --runs 3   # mean and range over 3 runs
floodrules eval --replay               # rescore saved responses, no API calls
floodrules compare evals/runs/A.json evals/runs/B.json
```

Each product is flattened into atomic facts (`location:cbrs`, `state:all_except:AK`, `limit:dwelling:1250000`, `zone:V:eligible_if_elevated_above_bfe`...). Precision and recall are computed over those facts, overall and by category. Two scores are reported for every run:

- **Raw:** what the model returned.
- **Verified:** after dropping rules whose quote isn't in the document. The gap between the two shows how much the quote check protects precision, and what it costs in recall.

For Ollama models the report shows latency and token counts; there's no per-token cost. The comparison worth making is size against accuracy: does a bigger model beat a smaller one by enough to justify the slower run, and how many invented quotes does the verifier catch from each? Check your plan's usage limits on ollama.com before a large run. For Claude, `--repeat 2` runs each document twice; the second run reads the document from the prompt cache, so the report shows the cost with and without caching. Results go to `evals/runs/<timestamp>.json` and `evals/runs/latest.md`.

**Eval set.** Nine public insurer documents in `evals/sources.json`, labeled in `evals/labels/` and checked line by line against each document's text: carrier brochures and guides (Hiscox 2020 and 2024, MSI 2025, Neptune 2023), broker comparison sheets (RT Specialty 2022 and 2023, Burns & Wilcox 2025, QuickHome) and an MSI comparison with the NFIP. Neptune's sheet refuses downloads from GitHub's servers (HTTP 403), so CI scores the other eight.

**Results** ([run 39703488854](https://github.com/martin473/flood-insurance-map/actions/runs/39703488854), October 21, 2026; 8 documents, 3 runs, mean with lowest–highest):

| Model | Precision (verified) | Recall (verified) | F1 (verified) | Seconds per document | Tokens per document (in / out) |
|---|---|---|---|---|---|
| gemma4 31B | 96% | 87% | 91% | 3.2 | 2,855 / 685 |
| gpt-oss 120B | 89% (88–90%) | 73% (66–86%) | 80% (76–88%) | 6.8 | 2,733 / 2,382 |

Both run on Ollama Cloud's free plan, so there's no per-document cost. The smaller model is more accurate and gave the same answer in all three runs. Where it misses: dollar limits (55% recall), flood-zone rules (67%) and building exclusions (73%); it finds every state rule and availability statement.

On this set the quote check doesn't raise precision (gemma4: 96% before and after) but costs 9 points of recall: it rejects some correct rules, mostly limits quoted from comparison tables whose extracted text order doesn't match the quote. Matching quotes inside tables more loosely is the next improvement.

## What neighbors paid

Each answer card shows what homes near the tapped spot actually paid, from published records rather than quotes:

- **Flood:** median, mean and middle half of the total yearly cost of federal flood policies on single-family homes, from FEMA's [NFIP policy records](https://www.fema.gov/openfema-data-page/nfip-redacted-policies-v3). Grouped by census tract and flood zone (high-risk vs. lower-risk), falling back to the town, then the county, when a tract has fewer than 10 policies. One year of policies, so each home counts about once.
- **Homeowners:** average premium per policy in the ZIP code, from the U.S. Treasury's 2018–2022 ZIP-level data (the latest published; [re-hosted by the Consumer Federation of America](https://consumerfed.org/news/blogs/download-analyses-of-us-homeowners-insurance-markets-2018-2022-climate-related-risks-and-other-factors-report-and-data-from-federal-insurance-office-fio/) after Treasury removed it).

`floodrules costs` downloads both, summarizes them and writes `map/data/ri-costs.json` (about 18 KB). The `data` workflow runs it monthly and commits the result. It refuses to write when the download looks truncated or a field changed, so a bad pull fails the run and the map keeps the previous file. The map gets the tract and ZIP for a point from the Census Bureau's TIGERweb service.

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

`pytest`: 95 tests covering schema export, PDF parsing, the quote verifier, both model clients (auth, host selection, `format` fallback, retries and truncation for Ollama; retries and cost math for Claude), extraction end to end against a fake API, the scorer, the CLI, the rules store, and the coverage engine on five Rhode Island spots whose facts were pulled from FEMA and USFWS on 2026-10-21. Fixtures use a made-up insurer guide rendered to a real PDF, including one invented rule the verifier has to catch.

## Limitations

- Broker comparison sheets are tables, and PDF text extraction can scramble cell order, which hurts both extraction and quote matching. If recall is low on those, try `pdfplumber` table extraction or send the PDF to Claude as a document block.
- The engine can't check rules about the building itself (condo, past claims, built-before dates). It lists them under `not_checked`.
- Coverage answers say whether an insurer's published rules allow the location, not whether it will write the policy or at what price.
