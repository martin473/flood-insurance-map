"""HTTP API.

  GET  /property  lat, lng, year -> will it flood, can it get flood / homeowners insurance, with sources
  POST /coverage  property facts -> a decision per insurance program, with quoted reasons
  GET  /rules     the verified insurer rules currently loaded, with their sources
  POST /extract   upload an insurer PDF, get verified rules back (admin token required when set)
  GET  /health    liveness and loaded-rule count
  GET  /metrics   per-data-source calls, errors, cache hits and latency; request counts

Run:  uvicorn floodrules.api:app --reload
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import sys
import threading
import time
import uuid
from collections import defaultdict, deque

from fastapi import FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from . import store
from .claude import ModelError
from .engine import Decision, PropertyFacts, evaluate_all
from .extract import make_extractor, run_extraction
from .pdftext import read_pdf
from .property import YEARS, PropertyReport, assess
from .sources import Sources

MAX_UPLOAD_BYTES = 15 * 1024 * 1024
RATE_LIMIT_PER_MIN = int(os.environ.get("FLOODRULES_RATE_LIMIT", "30"))
RI_BOUNDS = (40.9, 42.3, -72.2, -70.9)   # lat min/max, lng min/max

log = logging.getLogger("floodrules")
if not log.handlers:
    _h = logging.StreamHandler(sys.stdout)   # containers collect stdout as the app log
    _h.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)

app = FastAPI(title="Flood insurance map API", version="0.2.0")
app.add_middleware(CORSMiddleware, allow_origins=os.environ.get("CORS_ORIGINS", "*").split(","),
                   allow_methods=["GET", "POST"], allow_headers=["*"], expose_headers=["x-request-id"])

sources = Sources()
_requests: dict[str, int] = defaultdict(int)
_hits: dict[str, deque] = defaultdict(deque)
_lock = threading.Lock()
STARTED = time.time()


@app.middleware("http")
async def log_requests(request: Request, call_next):
    """One JSON log line per request, with an id the client can quote in a bug report."""
    rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
    start = time.monotonic()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        response.headers["x-request-id"] = rid
        return response
    finally:
        ms = round((time.monotonic() - start) * 1000, 1)
        with _lock:
            _requests[f"{request.url.path} {status}"] += 1
        log.info(json.dumps({"event": "request", "id": rid, "method": request.method, "path": request.url.path,
                             "status": status, "ms": ms}))


def _client_ip(request: Request) -> str:
    # Behind a load balancer every request comes from the proxy, and the real client is the last
    # address the proxy appended to x-forwarded-for. Clients can forge the header, so it's only
    # read when the deployment says a trusted proxy is in front (FLOODRULES_TRUST_PROXY=1).
    if os.environ.get("FLOODRULES_TRUST_PROXY") == "1":
        hops = [h.strip() for h in request.headers.get("x-forwarded-for", "").split(",") if h.strip()]
        if hops:
            return hops[-1]
    return request.client.host if request.client else "unknown"


def _rate_limit(client: str) -> None:
    """Each lookup fans out to four public services, so cap how fast one client can ask."""
    now = time.monotonic()
    with _lock:
        q = _hits[client]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= RATE_LIMIT_PER_MIN:
            raise HTTPException(429, f"More than {RATE_LIMIT_PER_MIN} lookups a minute. Wait a moment and try again.")
        q.append(now)


class CoverageResponse(BaseModel):
    facts: PropertyFacts
    decisions: list[Decision]
    sources: list[dict]


@app.get("/health")
def health() -> dict:
    return {"ok": True, "programs_loaded": len(store.load_all())}


@app.get("/metrics")
def metrics() -> dict:
    with _lock:
        reqs = dict(_requests)
    return {"uptime_s": round(time.time() - STARTED), "requests": reqs,
            "sources": {s.name: s.stats.as_dict() for s in sources.all()}}


@app.get("/property", response_model=PropertyReport)
def property_report(request: Request, lat: float = Query(..., ge=-90, le=90), lng: float = Query(..., ge=-180, le=180),
                    year: str = Query("2056", description=f"one of {', '.join(YEARS)}")) -> PropertyReport:
    if not (RI_BOUNDS[0] <= lat <= RI_BOUNDS[1] and RI_BOUNDS[2] <= lng <= RI_BOUNDS[3]):
        raise HTTPException(422, "This service covers Rhode Island only.")
    if year not in YEARS:
        raise HTTPException(422, f"year must be one of {', '.join(YEARS)}")
    _rate_limit(_client_ip(request))
    report = assess(lat, lng, sources, [p for _, p in store.load_all()], year=year)
    failed = [s.name for s in report.sources if not s.ok]
    if failed:
        log.warning(json.dumps({"event": "source_failed", "sources": failed, "lat": round(lat, 3), "lng": round(lng, 3)}))
    return report


@app.get("/rules")
def rules() -> list[dict]:
    return [{"provenance": prov, "product": p.model_dump(mode="json")} for prov, p in store.load_all()]


@app.post("/coverage", response_model=CoverageResponse)
def coverage(facts: PropertyFacts) -> CoverageResponse:
    loaded = store.load_all()
    srcs = [{"insurer": p.insurer, "program": p.program, **prov} for prov, p in loaded]
    return CoverageResponse(facts=facts, decisions=evaluate_all([p for _, p in loaded], facts), sources=srcs)


@app.post("/extract")
async def extract(file: UploadFile = File(...), model: str = Form(os.environ.get("FLOODRULES_MODEL", "claude-haiku-5-5")),
                  save: bool = Form(False), source_url: str | None = Form(None), authorization: str | None = Header(None)) -> dict:
    # Extraction spends the operator's model quota, so a deployed server sets FLOODRULES_ADMIN_TOKEN.
    token = os.environ.get("FLOODRULES_ADMIN_TOKEN")
    if not token and os.environ.get("FLOODRULES_REQUIRE_ADMIN") == "1":
        # The Docker image sets this, so a server deployed without a token fails closed instead of open.
        raise HTTPException(503, "Extraction is disabled: set FLOODRULES_ADMIN_TOKEN on the server.")
    if token and not hmac.compare_digest(authorization or "", f"Bearer {token}"):
        raise HTTPException(401, "Extraction requires the admin token.")
    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "PDF is larger than 15 MB.")
    if not data.startswith(b"%PDF"):
        raise HTTPException(415, "Upload a PDF file.")
    try:
        doc = read_pdf(data, name=file.filename or "upload.pdf")
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    try:
        run = run_extraction(doc, make_extractor(model))
    except ModelError as e:
        raise HTTPException(502, str(e)) from e
    saved = str(store.save(run, source_url)) if save else None
    return {
        "document": run.document, "model": run.model, "prompt_version": run.prompt_version,
        "products": [p.model_dump(mode="json") for p in run.verified.products],
        "rejected": [r.__dict__ for r in run.report.rejected],
        "verification": {"kept": run.report.kept, "rejected": len(run.report.rejected), "verified_rate": round(run.report.verified_rate, 3)},
        "usage": {**run.usage.__dict__, "cost_usd": run.cost_usd},
        "saved_to": saved,
    }
