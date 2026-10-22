# Roadmap

**Goal:** a resume line backed by numbers that hold up in an interview, for applied-AI roles in fintech and insurance:

> Built a Python/FastAPI service that extracts flood insurers' eligibility rules from PDFs into typed schemas with an LLM, rejects any rule it can't trace to a quote in the source, and scores itself against 10 hand-labeled documents (97% precision, 96% recall, mean of 3 runs) in CI. Deployed behind a public Rhode Island flood and insurance map, with request logging and per-source error tracking.

Each phase closes one gap from the job-posting review: production Python, an eval harness, operating what you ship, and showing judgment when building with an agent.

**Status, October 21, 2026:** Phases 1 to 3 are done and deployed. The final eval ([run](https://github.com/martin473/flood-insurance-map/actions/runs/39705882557), 10 documents): gemma4 31B 97% precision, 96% recall; gpt-oss 120B 84%, 64%. The next steps are in the design doc: saved reports, contract and browser tests, a status page, alerts.

## Phase 1: Trustworthy eval numbers

| Step | Owner | Status |
|---|---|---|
| A dollar limit only counts if its amount appears in its quote | Claude | Done |
| `--runs N`: repeat the eval and report mean, min and max | Claude | Done, in the CLI and the CI eval workflow |
| Check every label against its PDF | Sawyer + Claude | Done: all 9 label files checked against the document text |
| Rerun: 3 runs per model, at least 2 models | Claude (CI) | Done: [run 39705882557](https://github.com/martin473/flood-insurance-map/actions/runs/39705882557), 10 documents |

## Phase 2: A bigger eval set

| Step | Owner | Status |
|---|---|---|
| Find more public underwriting guides and brochures | Claude | Done: 5 added, 8 documents in `evals/sources.json` |
| Label the new documents | Sawyer + Claude | Draft labels written; checked together with Phase 1 |
| Add ShoreOne / Neptune material if they reply | Sawyer | Waiting on replies |

## Phase 3: Run it as a service

| Step | Owner | Status |
|---|---|---|
| Move the map's FEMA, URI and USFWS lookups into the API, with timeouts, retries and caching | Claude | Done: `GET /property` |
| Structured request logs and per-source metrics | Claude | Done: JSON request logs, `GET /metrics` |
| Dockerfile and DigitalOcean app spec | Claude | Done. CI builds the image and checks it starts; steps in `DEPLOY.md` |
| Map uses the API when served with it | Claude | Done; falls back to the public services when opened as a file |
| Deploy | Sawyer | **To do:** follow `DEPLOY.md` |
| Extract and commit verified rules | Sawyer | **To do,** after the label check: `floodrules extract data/docs/<doc>.pdf --model ollama:<model> --save` for each document, then commit `data/rules/` |

## Phase 4: Tell the story

| Step | Owner | Status |
|---|---|---|
| Blog post draft | Claude | Done: `docs/blog-draft.md` |
| Final numbers, edit, publish | Sawyer | **To do,** after Phase 1 |
| Resume line | Sawyer | **To do,** after Phase 1 |

## Sawyer's remaining steps, in order

1. `git pull` in `~/flood-rules`, then `floodrules fetch` to download the 5 new PDFs.
2. Attach all 8 PDFs from `data/docs/` in chat for the label check.
3. Run the eval workflow (Actions > eval) with 3 runs and two models.
4. Deploy with `DEPLOY.md`.
5. Extract the rules, commit them, and push. The live map then uses them.
6. Fill in the numbers in the blog draft and the resume line.
