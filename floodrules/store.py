"""Verified rules on disk: one JSON file per source document, with provenance."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from .extract import ExtractionRun
from .schemas import Product

# In a checkout this is <repo>/data/rules. An installed package (the Docker image) sets FLOODRULES_RULES_DIR.
DEFAULT_DIR = Path(os.environ.get("FLOODRULES_RULES_DIR") or Path(__file__).resolve().parent.parent / "data" / "rules")


def save(run: ExtractionRun, source_url: str | None = None, rules_dir: Path = DEFAULT_DIR) -> Path:
    rules_dir.mkdir(parents=True, exist_ok=True)
    out = rules_dir / f"{Path(run.document).stem}.json"
    out.write_text(json.dumps({
        "source": {"document": run.document, "url": source_url},
        "extracted": {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "model": run.model,
                      "prompt_version": run.prompt_version},
        "verification": {"kept": run.report.kept, "rejected": len(run.report.rejected),
                         "page_corrected": run.report.page_corrected},
        "products": [p.model_dump(mode="json") for p in run.verified.products],
    }, indent=1))
    return out


def load_all(rules_dir: Path = DEFAULT_DIR) -> list[tuple[dict, Product]]:
    """Returns (provenance, product) pairs. When two documents describe the same program,
    the most recently extracted one wins."""
    best: dict[tuple[str, str], tuple[str, dict, Product]] = {}
    for f in sorted(Path(rules_dir).glob("*.json")):
        data = json.loads(f.read_text())
        prov = {"source": data["source"], "extracted": data["extracted"], "file": f.name}
        for raw in data["products"]:
            p = Product.model_validate(raw)
            key = (p.insurer.strip().lower(), p.program.strip().lower())
            stamp = data["extracted"]["at"]
            if key not in best or stamp > best[key][0]:
                best[key] = (stamp, prov, p)
    return [(prov, p) for _, prov, p in best.values()]
