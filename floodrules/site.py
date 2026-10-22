"""One app that serves both the map (at /) and the API (at /api), for running on a plain server.

  uvicorn floodrules.site:app --port 8080

/status is a page showing each data source's health, recent lookups and data freshness.
The map looks for the API at /api on the same address, so this is all a server needs:
no separate web server. FLOODRULES_MAP_DIR points at the map folder (default: <repo>/map).
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .api import app as api_app

MAP_DIR = Path(os.environ.get("FLOODRULES_MAP_DIR") or Path(__file__).resolve().parent.parent / "map")
MAP_PAGE = "rhode-island-flood-map.html"

app = FastAPI(title="Rhode Island Flood Outlook", docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/", include_in_schema=False)
def map_page() -> FileResponse:
    return FileResponse(MAP_DIR / MAP_PAGE, media_type="text/html")


@app.get("/status", include_in_schema=False)
def status_page() -> FileResponse:
    return FileResponse(MAP_DIR / "status.html", media_type="text/html")


@app.get("/api", include_in_schema=False)
def api_root() -> RedirectResponse:
    return RedirectResponse("/api/docs")


app.mount("/api", api_app)
app.mount("/", StaticFiles(directory=MAP_DIR, check_dir=False), name="map")
