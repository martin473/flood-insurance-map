"""Verified rules on disk: one JSON file per source document, with provenance."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from .extract import ExtractionRun
from .merge import merge_two, same_product
from .schemas import Product

# In a checkout this is <repo>/data/rules. An installed package (the Docker image) sets FLOODRULES_RULES_DIR.
DEFAULT_DIR = Path(os.environ.get("FLOODRULES_RULES_DIR") or Path(__file__).resolve().parent.parent / "data" / "rules")


def save(run: ExtractionRun, source_url: str | None = None, rules_dir: Path = DEFAULT_DIR,
         products: list[Product] | None = None, published: str | None = None, review: dict | None = None) -> Path:
    """Write verified rules. `products` narrows what's saved (e.g. only the programs that passed review),
    `published` is the source document's date, used to let newer documents win when they disagree."""
    rules_dir.mkdir(parents=True, exist_ok=True)
    out = rules_dir / f"{Path(run.document).stem}.json"
    keep = run.verified.products if products is None else products
    data = {
        "source": {"document": run.document, "url": source_url, "published": published},
        "extracted": {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "model": run.model,
                      "prompt_version": run.prompt_version},
        "verification": {"kept": run.report.kept, "rejected": len(run.report.rejected),
                         "page_corrected": run.report.page_corrected},
        "products": [p.model_dump(mode="json") for p in keep],
    }
    if review is not None:
        data["review"] = review
    out.write_text(json.dumps(data, indent=1) + "\n")
    return out


def load_all(rules_dir: Path = DEFAULT_DIR) -> list[tuple[dict, Product]]:
    """Returns (provenance, product) pairs.

    If a document was extracted more than once, its most recent extraction wins. A program described
    in several documents (an insurer's brochure and a broker's comparison sheet) is merged into one,
    with the most recently published document taking precedence on anything they disagree about and
    exclusions from all of them kept."""
    latest: dict[str, tuple[str, dict, list[Product]]] = {}
    for f in sorted(Path(rules_dir).glob("*.json")):
        data = json.loads(f.read_text())
        stamp = data["extracted"]["at"]
        doc = data["source"].get("document") or f.name
        if doc in latest and latest[doc][0] >= stamp:
            continue
        prov = {"source": data["source"], "extracted": data["extracted"], "file": f.name}
        latest[doc] = (stamp, prov, [Product.model_validate(raw) for raw in data["products"]])

    def newest_first(item: tuple[str, dict, list[Product]]) -> tuple[str, str]:
        return (item[1]["source"].get("published") or "", item[0])

    merged: list[tuple[dict, Product]] = []
    for _, prov, products in sorted(latest.values(), key=newest_first, reverse=True):
        for p in products:
            idx = next((i for i, (_, q) in enumerate(merged) if same_product(p, q)), None)
            if idx is None:
                merged.append(({**prov, "also": []}, p))
            else:
                first, q = merged[idx]
                first["also"].append(prov["source"])
                merged[idx] = (first, merge_two(q, p))
    return merged
