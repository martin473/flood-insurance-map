# Roadmap

**Goal:** a resume line backed by numbers that hold up in an interview, for applied-AI roles in fintech and insurance:

> Built a Python/FastAPI service that extracts flood insurers' eligibility rules from PDFs into typed schemas with an LLM, rejects any rule it can't trace to a quote in the source, and scores itself against [N] hand-labeled documents ([X]% precision, [Y]% recall, mean of 3 runs) in CI. Deployed behind a public Rhode Island flood and insurance map, with request logging and per-source error tracking.

Each phase closes one gap from the job-posting review: production Python, an eval harness, operating what you ship, and showing judgment when building with an agent.

## Phase 1: Trustworthy eval numbers

| Step | Owner | Done when |
|---|---|---|
| A dollar limit only counts if its amount appears in its quote | Claude | Computed amounts ("20% of dwelling" written as $450K) are rejected; tested |
| `--runs N`: repeat the eval and report mean, min and max | Claude | CLI and the CI eval workflow report a range, not one noisy number |
| Check every label against its PDF | Sawyer + Claude | Each label file's `status` says "checked against the PDF" |
| Rerun: 3 runs per model, at least 2 models | Sawyer (CI) | Results table in the README |

## Phase 2: A bigger eval set

| Step | Owner | Done when |
|---|---|---|
| Find more public underwriting guides and brochures | Claude | Candidates listed in `evals/sources.json` |
| Label 5 to 7 more documents | Sawyer + Claude | 8 to 10 labeled documents |
| Add ShoreOne / Neptune material if they reply | Sawyer | Included, if it's allowed to be public |

## Phase 3: Run it as a service

| Step | Owner | Done when |
|---|---|---|
| Move the map's FEMA, URI and USFWS lookups into the API, with timeouts, retries and caching | Claude | `GET /property?lat=&lng=` returns the three answers with sources; tested against recorded responses |
| Structured request logs and per-source metrics | Claude | `GET /metrics` shows latency and error counts per data source |
| Dockerfile and DigitalOcean app spec | Claude | `docker build` works; deploy steps in `DEPLOY.md` |
| Deploy | Sawyer | Public API URL; health check green |
| Map uses the API when configured | Claude | Map calls `/property` when given an API URL, and works standalone otherwise |
| Extract and commit verified rules | Sawyer | `data/rules/` has the three documents' verified rules; `/coverage` uses them |

## Phase 4: Tell the story

| Step | Owner | Done when |
|---|---|---|
| Blog post draft | Claude | `docs/blog-draft.md` with placeholders for final numbers |
| Final numbers, edit, publish | Sawyer | Live on sawyersweet.net/blog, linked from the README |
| Resume line | Sawyer | Numbers filled in from Phase 1 |
