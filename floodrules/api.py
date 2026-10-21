"""HTTP API.

  POST /extract   upload an insurer PDF, get verified rules back (and optionally save them)
  POST /coverage  send property facts, get a decision per program with quoted reasons
  GET  /rules     the verified rules currently loaded, with their sources

Run:  uvicorn floodrules.api:app --reload
"""

from __future__ import annotations

import os

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from . import store
from .claude import ModelError
from .engine import Decision, PropertyFacts, evaluate_all
from .extract import make_extractor, run_extraction
from .pdftext import read_pdf

MAX_UPLOAD_BYTES = 15 * 1024 * 1024

app = FastAPI(title="Flood insurer rules", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=os.environ.get("CORS_ORIGINS", "*").split(","),
                   allow_methods=["GET", "POST"], allow_headers=["*"])


class CoverageResponse(BaseModel):
    facts: PropertyFacts
    decisions: list[Decision]
    sources: list[dict]


@app.get("/health")
def health() -> dict:
    return {"ok": True, "programs_loaded": len(store.load_all())}


@app.get("/rules")
def rules() -> list[dict]:
    return [{"provenance": prov, "product": p.model_dump(mode="json")} for prov, p in store.load_all()]


@app.post("/coverage", response_model=CoverageResponse)
def coverage(facts: PropertyFacts) -> CoverageResponse:
    loaded = store.load_all()
    sources = [{"insurer": p.insurer, "program": p.program, **prov} for prov, p in loaded]
    return CoverageResponse(facts=facts, decisions=evaluate_all([p for _, p in loaded], facts), sources=sources)


@app.post("/extract")
async def extract(file: UploadFile = File(...), model: str = Form(os.environ.get("FLOODRULES_MODEL", "claude-haiku-5-5")),
                  save: bool = Form(False), source_url: str | None = Form(None)) -> dict:
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
