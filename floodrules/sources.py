"""Public data sources behind a property lookup, with timeouts, retries, caching and metrics.

  FEMA NFHL        flood zone, whether lenders require insurance (SFHA), floodway
  USFWS CBRS       coastal barrier units, where federal flood insurance is restricted
  URI STORMTOOLS   100-year storm water depth under six sea level rise scenarios
  Nominatim (OSM)  town, for rules that depend on islands

Each source counts calls, errors, cache hits and latency, which GET /metrics reports.
These government services go down for maintenance, so one failing source degrades
the answer (marked unknown) instead of failing the whole request.
"""

from __future__ import annotations

import json
import os
import statistics
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Callable

import requests

from . import tracing

FEMA = "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/28/query"
CBRS = "https://gis1.wim.usgs.gov/server/rest/services/CBRSMapper/CoastalBarrierResourcesSystem/MapServer/1/query"
STORM = "https://maps.edc.uri.edu/stormtools/rest/services/Stormtools2020/Mosaic_100yr_SDE_TOTALDEPTH_All/ImageServer/getSamples"
NOMINATIM = "https://nominatim.openstreetmap.org/reverse"
USER_AGENT = os.environ.get("FLOODRULES_USER_AGENT", "flood-insurance-map/0.1 (github.com/martin473/flood-insurance-map)")

# STORMTOOLS raster IDs 1..6 = 0, 2, 3, 5, 7, 10 ft of sea level rise (per the service's own description).
# Years are when each rise is reached at the Newport tide gauge under NOAA's 2022 High scenario, the
# projection the RI Coastal Resources Management Council requires in its coastal hazard worksheets:
# 2050 1.60 ft, 2060 2.29, 2070 3.17, 2080 4.19, 2090 5.35, 2100 6.47 (linear in between).
# 7 and 10 ft aren't reached by 2100 under that scenario.
SCENARIOS = [(1, 0, "today"), (2, 2, "2056"), (3, 3, "2068"), (4, 5, "2087"), (5, 7, "after-2100"), (6, 10, "worst-case")]

Transport = Callable[[str, dict[str, Any], dict[str, str], float], tuple[int, Any]]


class SourceError(RuntimeError):
    pass


def _http(url: str, params: dict[str, Any], headers: dict[str, str], timeout: float) -> tuple[int, Any]:
    r = requests.get(url, params=params, headers=headers, timeout=timeout)
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, {"error": {"message": r.text[:200]}}


@dataclass
class Stats:
    calls: int = 0
    errors: int = 0
    cache_hits: int = 0
    retries: int = 0
    total_ms: float = 0.0
    max_ms: float = 0.0
    last_error: str | None = None
    last_error_at: str | None = None
    recent_ms: deque = field(default_factory=lambda: deque(maxlen=200))   # latency of the last 200 network calls

    def as_dict(self) -> dict:
        made = self.calls - self.cache_hits
        lat = sorted(self.recent_ms)

        def pct(q: float) -> float | None:
            return round(lat[min(len(lat) - 1, int(q * len(lat)))], 1) if lat else None

        return {"calls": self.calls, "cache_hits": self.cache_hits, "errors": self.errors, "retries": self.retries,
                "error_rate": round(self.errors / made, 3) if made else 0.0,
                "cache_hit_rate": round(self.cache_hits / self.calls, 3) if self.calls else None,
                "avg_ms": round(self.total_ms / made, 1) if made else None, "max_ms": round(self.max_ms, 1),
                "p50_ms": pct(0.5), "p95_ms": pct(0.95),
                "last_error": self.last_error, "last_error_at": self.last_error_at}


@dataclass
class Source:
    name: str
    url: str
    transport: Transport = _http
    timeout: float = 8.0
    retries: int = 2
    ttl_s: float = 24 * 3600
    sleep: Callable[[float], None] = time.sleep
    stats: Stats = field(default_factory=Stats)
    _cache: dict[str, tuple[float, Any]] = field(default_factory=dict, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def get(self, params: dict[str, Any]) -> Any:
        """A cached, retried call, traced as a child span of the current request when there is one."""
        with tracing.span(self.name) as sp:
            return self._get(params, sp)

    def _get(self, params: dict[str, Any], sp: tracing.Span | None) -> Any:
        key = json.dumps(params, sort_keys=True)
        with self._lock:
            self.stats.calls += 1
            hit = self._cache.get(key)
            if hit and hit[0] > time.monotonic():
                self.stats.cache_hits += 1
                if sp:
                    sp.attrs.update(cache_hit=True)
                return hit[1]
        if sp:
            sp.attrs.update(cache_hit=False)
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        last = ""
        for attempt in range(self.retries + 1):
            start = time.monotonic()
            try:
                status, data = self.transport(self.url, params, headers, self.timeout)
                ms = (time.monotonic() - start) * 1000
                self._timing(ms)
                if status == 200:
                    with self._lock:
                        self._cache[key] = (time.monotonic() + self.ttl_s, data)
                    if sp:
                        sp.attrs.update(attempts=attempt + 1, status=200)
                    return data
                last = f"HTTP {status}"
                if status not in (429, 500, 502, 503, 504):
                    break
            except requests.RequestException as e:
                self._timing((time.monotonic() - start) * 1000)
                last = type(e).__name__
            if attempt < self.retries:
                with self._lock:
                    self.stats.retries += 1
                self.sleep(0.5 * 2 ** attempt)
        with self._lock:
            self.stats.errors += 1
            self.stats.last_error = last
            self.stats.last_error_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if sp:
            sp.attrs.update(attempts=attempt + 1, outcome=last)
        raise SourceError(f"{self.name}: {last}")

    def _timing(self, ms: float) -> None:
        with self._lock:
            self.stats.total_ms += ms
            self.stats.max_ms = max(self.stats.max_ms, ms)
            self.stats.recent_ms.append(ms)


def _point(lat: float, lng: float) -> dict[str, Any]:
    return {"geometry": f"{lng},{lat}", "geometryType": "esriGeometryPoint", "inSR": 4326,
            "spatialRel": "esriSpatialRelIntersects", "returnGeometry": "false", "f": "json"}


class Sources:
    def __init__(self, transport: Transport | None = None, sleep: Callable[[float], None] = time.sleep):
        t = transport or _http
        self.fema = Source("fema_nfhl", FEMA, t, sleep=sleep)
        self.cbrs = Source("usfws_cbrs", CBRS, t, sleep=sleep)
        self.storm = Source("uri_stormtools", STORM, t, timeout=12.0, sleep=sleep)
        self.town = Source("nominatim", NOMINATIM, t, retries=1, sleep=sleep)

    def all(self) -> list[Source]:
        return [self.fema, self.cbrs, self.storm, self.town]

    def fema_zone(self, lat: float, lng: float) -> dict:
        data = self.fema.get({**_point(lat, lng), "outFields": "FLD_ZONE,ZONE_SUBTY,SFHA_TF,STATIC_BFE"})
        if isinstance(data, dict) and data.get("error"):
            raise SourceError(f"fema_nfhl: {data['error'].get('message', 'error')}")
        attrs = [f["attributes"] for f in data.get("features", [])]
        high = next((a for a in attrs if a.get("SFHA_TF") == "T"), None)
        pick = high or (attrs[0] if attrs else None)
        return {"zone": pick.get("FLD_ZONE") if pick else None,
                "subtype": pick.get("ZONE_SUBTY") if pick else None,
                "sfha": high is not None,
                "floodway": any("FLOODWAY" in (a.get("ZONE_SUBTY") or "").upper() for a in attrs),
                "mapped": bool(attrs)}

    def cbrs_unit(self, lat: float, lng: float) -> dict | None:
        data = self.cbrs.get({**_point(lat, lng), "outFields": "Unit,CBRS_Type,FI_Date"})
        if isinstance(data, dict) and data.get("error"):
            raise SourceError(f"usfws_cbrs: {data['error'].get('message', 'error')}")
        feats = data.get("features", [])
        if not feats:
            return None
        a = feats[0]["attributes"]
        fi = a.get("FI_Date")
        fi_date = datetime.fromtimestamp(fi / 1000, timezone.utc).date() if isinstance(fi, (int, float)) else None
        return {"unit": a.get("Unit"), "type": a.get("CBRS_Type"), "flood_insurance_date": fi_date}

    def storm_depths(self, lat: float, lng: float) -> list[float]:
        """Water depth at the point for each scenario. The raster has no-data gaps on beaches and dunes,
        so when the point itself has no value, the median of eight samples ~20 m around it is used
        (the median, not the deepest, so a lot next to open water isn't given the water's depth)."""
        dl, dg = 0.00018, 0.00024
        offsets = [(0, 0), (dl, 0), (-dl, 0), (0, dg), (0, -dg), (dl, dg), (-dl, -dg), (dl, -dg), (-dl, dg)]
        geometry = json.dumps({"points": [[lng + o[1], lat + o[0]] for o in offsets], "spatialReference": {"wkid": 4326}})
        def one(raster_id: int) -> float:
            data = self.storm.get({"geometry": geometry, "geometryType": "esriGeometryMultipoint", "returnFirstValueOnly": "true",
                                   "mosaicRule": json.dumps({"mosaicMethod": "esriMosaicLockRaster", "lockRasterIds": [raster_id]}),
                                   "f": "json"})
            if isinstance(data, dict) and data.get("error"):
                return 0.0   # STORMTOOLS answers with an error when every sample is no-data: dry
            center, around = None, []
            for s in data.get("samples", []):
                try:
                    v = float(s.get("value"))
                except (TypeError, ValueError):
                    continue
                if v <= 0:
                    continue
                if s.get("locationId") == 0:
                    center = v
                else:
                    around.append(v)
            value = center if center is not None else (statistics.median(around) if around else 0.0)
            return round(value, 1)

        # the six scenarios are independent, so ask for them in parallel; traces showed them running
        # one after another and making up most of an uncached lookup's time
        cur = tracing.current()

        def traced(raster_id: int) -> float:
            with tracing.use(cur[0] if cur else None, cur[1] if cur else None):
                return one(raster_id)

        with ThreadPoolExecutor(max_workers=len(SCENARIOS)) as pool:
            return list(pool.map(traced, [raster_id for raster_id, _, _ in SCENARIOS]))

    def town_name(self, lat: float, lng: float) -> str:
        data = self.town.get({"format": "json", "lat": round(lat, 5), "lon": round(lng, 5), "zoom": 12, "addressdetails": 1})
        a = data.get("address", {}) if isinstance(data, dict) else {}
        return a.get("town") or a.get("city") or a.get("village") or a.get("municipality") or ""


def iso(d: date | None) -> str | None:
    return d.isoformat() if d else None
