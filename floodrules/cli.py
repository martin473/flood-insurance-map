"""Command line.

  floodrules fetch                          download the eval documents listed in evals/sources.json
  floodrules extract guide.pdf [--save]     extract, verify and print rules for one PDF
  floodrules eval --model claude-haiku-5-5 --model claude-sonnet-5-5 [--repeat 2]
  floodrules eval --replay                  rescore saved responses offline (no API calls)
  floodrules compare runs/a.json runs/b.json
  floodrules publish-rules --model ollama:nemotron-3-nano:30b
                                            extract the documents marked "publish" in evals/sources.json,
                                            review them against the labels and save what passes to data/rules
  floodrules costs                          rebuild map/data/ri-costs.json from FEMA and Treasury data
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from . import store
from .claude import ModelError, Usage
from .evaluation import CATEGORIES, combine, load_labels, score
from .extract import PROMPT_VERSION, ReplayExtractor, make_extractor, run_extraction
from .pdftext import read_pdf

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MODEL = os.environ.get("FLOODRULES_MODEL", "claude-haiku-5-5")
CACHE = ROOT / "evals" / "cache"
RUNS = ROOT / "evals" / "runs"
FETCH_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; flood-insurance-map/0.1; +https://github.com/martin473/flood-insurance-map)",
                 "Accept": "application/pdf,*/*;q=0.8"}


def _sources(path: Path) -> list[dict]:
    return json.loads(path.read_text())["documents"]


def cmd_fetch(args) -> int:
    for d in _sources(Path(args.sources)):
        dest = ROOT / d["file"]
        if dest.exists():
            print(f"have  {dest.name}")
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        # some publishers refuse the default python-requests user agent with a 403
        r = requests.get(d["url"], timeout=60, headers=FETCH_HEADERS)
        if r.status_code != 200 or not r.content.startswith(b"%PDF"):
            print(f"FAIL  {d['id']}: HTTP {r.status_code}. Download it by hand from {d['url']} to {dest}")
            continue
        dest.write_bytes(r.content)
        print(f"saved {dest.name} ({len(r.content) // 1024} KB)")
    return 0


def cmd_text(args) -> int:
    """Print a document's text exactly as the verifier sees it (pypdf), page by page, for writing labels."""
    for d in _sources(Path(args.sources)):
        if args.only and d["id"] not in args.only:
            continue
        path = ROOT / d["file"]
        if not path.exists():
            print(f"## {d['id']}: missing {d['file']} (run `floodrules fetch`)\n")
            continue
        doc = read_pdf(path, name=f"{d['id']}.pdf")
        print(f"## {d['id']} ({len(doc.pages)} pages)\n")
        for i, page in enumerate(doc.pages, 1):
            print(f"[page {i}]\n{page.strip()}\n")
    return 0


def cmd_models(args) -> int:
    from .ollama import Ollama
    client = Ollama(model="")
    names = client.list_models()
    print(f"{len(names)} models at {client.host}:")
    for n in names:
        print(f"  ollama:{n}")
    return 0


def cmd_extract(args) -> int:
    doc = read_pdf(args.pdf)
    run = run_extraction(doc, make_extractor(args.model), cache_dir=CACHE)
    print(json.dumps({"products": [p.model_dump(mode="json") for p in run.verified.products],
                      "rejected": [r.__dict__ for r in run.report.rejected],
                      "usage": {**run.usage.__dict__, "cost_usd": run.cost_usd}}, indent=1))
    if args.save:
        print(f"saved {store.save(run, args.url)}", file=sys.stderr)
    return 0


def evaluate_model(model: str, sources: list[dict], replay: bool, repeat: int, run_index: int = 1) -> dict:
    extractor: Any = ReplayExtractor(CACHE, model, run_index) if replay else make_extractor(model)
    extractor.run_index = run_index
    raw_scores, ver_scores, docs = [], [], []
    for d in sources:
        path = ROOT / d["file"]
        if not path.exists():
            docs.append({"id": d["id"], "skipped": f"missing {d['file']} (run `floodrules fetch`)"})
            continue
        doc = read_pdf(path, name=f"{d['id']}.pdf")
        run_note = f" run {run_index}" if run_index > 1 else ""
        print(f"{model}{run_note}: {d['id']} ({len(doc.pages)} pages)...", file=sys.stderr, flush=True)
        labels = load_labels(ROOT / d["labels"])
        attempts = []
        try:
            for i in range(max(1, repeat)):
                run = run_extraction(doc, extractor, cache_dir=None if replay else CACHE)
                attempts.append({"attempt": i + 1, "usage": run.usage.__dict__, "cost_usd": run.cost_usd})
        except (ModelError, FileNotFoundError) as e:
            # A failed document scores as all misses rather than stopping the whole eval.
            print(f"  {d['id']}: failed: {e}", file=sys.stderr)
            empty = score([], labels)
            raw_scores.append(empty)
            ver_scores.append(empty)
            docs.append({"id": d["id"], "pages": len(doc.pages), "error": str(e), "raw": empty.as_dict(), "verified": empty.as_dict()})
            continue
        raw_s, ver_s = score(run.extraction.products, labels), score(run.verified.products, labels)
        raw_scores.append(raw_s)
        ver_scores.append(ver_s)
        docs.append({"id": d["id"], "pages": len(doc.pages), "chunks": run.chunks, "raw": raw_s.as_dict(), "verified": ver_s.as_dict(),
                     "schema_dropped": [x.__dict__ for x in run.schema_dropped],
                     "format_accepted": getattr(getattr(extractor, "client", None), "used_format", None),
                     "verification": {"kept": run.report.kept, "rejected": len(run.report.rejected),
                                      "page_corrected": run.report.page_corrected,
                                      "rejections": [r.__dict__ for r in run.report.rejected]},
                     "attempts": attempts})

    def total_usage(key: int) -> Usage:
        u = Usage()
        for d in docs:
            if "attempts" in d and len(d["attempts"]) > key:
                u = u.add(Usage(**d["attempts"][key]["usage"]))
        return u

    first, later = total_usage(0), total_usage(1) if repeat > 1 else None
    return {
        "model": model, "prompt_version": PROMPT_VERSION, "replay": replay,
        "totals": {"raw": combine(raw_scores).as_dict(), "verified": combine(ver_scores).as_dict()},
        "cost": {"first_run": {**first.__dict__, "cost_usd": first.cost_usd(model)},
                 "repeat_run": ({**later.__dict__, "cost_usd": later.cost_usd(model)} if later else None)},
        "documents": docs,
    }


METRICS = [("raw", "precision"), ("raw", "recall"), ("raw", "f1"), ("verified", "precision"), ("verified", "recall"), ("verified", "f1")]


def evaluate_runs(model: str, sources: list[dict], replay: bool, repeat: int, runs: int) -> dict:
    """Run the whole eval `runs` times. Cloud models don't answer identically even at temperature 0,
    so one run's score can be off by several points; the mean and range are what to compare."""
    results = [evaluate_model(model, sources, replay, repeat, run_index=i) for i in range(1, max(1, runs) + 1)]
    first = results[0]
    summary = {}
    for kind, metric in METRICS:
        values = [r["totals"][kind]["overall"][metric] for r in results if r["totals"][kind]["overall"][metric] is not None]
        summary[f"{kind}_{metric}"] = ({"mean": round(sum(values) / len(values), 3), "min": min(values), "max": max(values)}
                                       if values else None)
    return {**first, "runs": len(results), "summary": summary, "per_run": [r["totals"] for r in results],
            "per_run_latency_s": [r["cost"]["first_run"]["latency_s"] for r in results]}


def _span(s: dict | None, runs: int) -> str:
    if not s:
        return "n/a"
    if runs == 1 or s["min"] == s["max"]:
        return f"{s['mean']:.0%}"
    return f"{s['mean']:.0%} ({s['min']:.0%}–{s['max']:.0%})"


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.0%}"


def _cost(c: dict | None, model: str = "") -> str:
    if model.startswith("ollama:"):
        return "no per-token cost" if c else "n/a"
    return "n/a" if not c or c.get("cost_usd") is None else f"${c['cost_usd']:.4f}"


def render_markdown(results: list[dict]) -> str:
    pct = _pct
    runs = max(r.get("runs", 1) for r in results)
    lines = ["# Extraction eval", "", f"Prompt version `{results[0]['prompt_version']}`. "
             "Scores are over atomic facts. *Verified* drops rules whose quote isn't in the document.", ""]
    if runs > 1:
        lines += [f"## Summary: mean (lowest–highest) over {runs} runs", "",
                  "| Model | Precision | Recall | F1 | Precision (verified) | Recall (verified) | F1 (verified) | Latency per run |",
                  "|---|---|---|---|---|---|---|---|"]
        for r in results:
            s, n = r.get("summary", {}), r.get("runs", 1)
            lat = r.get("per_run_latency_s", [])
            lat_txt = f"{sum(lat) / len(lat):.1f}s" if lat else "n/a"
            lines.append(f"| {r['model']}{' (replay)' if r['replay'] else ''} | " +
                         " | ".join(_span(s.get(f"{k}_{m}"), n) for k, m in METRICS) + f" | {lat_txt} |")
        lines += ["", "Details below are from run 1.", ""]
    lines += [
             "| Model | Precision | Recall | F1 | Precision (verified) | Recall (verified) | Products found | Cost, first run | Cost, cached rerun | Latency |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        raw, ver = r["totals"]["raw"]["overall"], r["totals"]["verified"]["overall"]
        c1, c2 = r["cost"]["first_run"], r["cost"]["repeat_run"]
        found = f"{r['totals']['verified']['products_matched']}/{r['totals']['verified']['products_expected']}"
        lines.append(f"| {r['model']}{' (replay)' if r['replay'] else ''} | {pct(raw['precision'])} | {pct(raw['recall'])} | {pct(raw['f1'])} | "
                     f"{pct(ver['precision'])} | {pct(ver['recall'])} | {found} | "
                     f"{_cost(c1, r['model'])} | {_cost(c2, r['model'])} | {c1['latency_s']:.1f}s |")
    lines += ["", "## Time and tokens per document (run 1)", "",
              "| Model | Documents scored | Failed | Seconds per document | Input tokens per document | Output tokens per document |",
              "|---|---|---|---|---|---|"]
    for r in results:
        ok = [d for d in r["documents"] if d.get("attempts")]
        failed = sum(1 for d in r["documents"] if "error" in d)
        n = len(ok) or 1
        usage = [d["attempts"][0]["usage"] for d in ok]
        secs = sum(u.get("latency_s", 0) for u in usage) / n
        tin = sum(u.get("input_tokens", 0) for u in usage) / n
        tout = sum(u.get("output_tokens", 0) for u in usage) / n
        lines.append(f"| {r['model']} | {len(ok)} | {failed} | {secs:.1f} | {tin:,.0f} | {tout:,.0f} |")
    lines += ["", "## Recall by category (verified)", "", "| Model | " + " | ".join(CATEGORIES) + " |", "|---|" + "---|" * len(CATEGORIES)]
    for r in results:
        cats = r["totals"]["verified"]["by_category"]
        lines.append(f"| {r['model']} | " + " | ".join(pct(cats.get(c, {}).get("recall")) for c in CATEGORIES) + " |")
    for r in results:
        lines += ["", f"## {r['model']}: per document", ""]
        for d in r["documents"]:
            if "skipped" in d:
                lines.append(f"- **{d['id']}**: skipped, {d['skipped']}")
                continue
            if "error" in d:
                lines.append(f"- **{d['id']}** ({d['pages']} pages): failed, scored as all misses. {d['error']}")
                continue
            v = d["verified"]
            chunked = f", {d['chunks']} chunks" if d.get("chunks", 1) > 1 else ""
            lines.append(f"- **{d['id']}** ({d['pages']} pages{chunked}): precision {pct(v['overall']['precision'])}, recall {pct(v['overall']['recall'])}; "
                         f"{d['verification']['rejected']} rules rejected by the quote check, "
                         f"{len(d.get('schema_dropped', []))} dropped (commercial programs, or values that didn't fit the schema).")
            for x in d["verification"].get("rejections", [])[:12]:
                near = f" Closest in the document ({x['similarity']:.0%} similar): \"{x['closest']}\"" if x.get("closest") else ""
                lines.append(f"  - rejected {x['field']} ({x['rule']}): {x['reason']}. Quote: \"{x['quote']}\".{near}")
            for x in d.get("schema_dropped", [])[:5]:
                lines.append(f"  - dropped {x['field']} `{x['value']}`: {x['reason']}")
            for m in v["misses"][:8]:
                lines.append(f"  - missed `{m}`")
            for f in v["false_positives"][:8]:
                lines.append(f"  - extra `{f}`")
    return "\n".join(lines) + "\n"


def cmd_eval(args) -> int:
    sources = _sources(Path(args.sources))
    if args.only:
        sources = [s for s in sources if s["id"] in args.only]
    results = [evaluate_runs(m, sources, args.replay, args.repeat, args.runs) for m in (args.model or [DEFAULT_MODEL])]
    RUNS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = RUNS / f"{stamp}.json"
    n = 2
    while out.exists():   # two evals in the same second
        out = RUNS / f"{stamp}-{n}.json"
        n += 1
    out.write_text(json.dumps(results, indent=1))
    md = render_markdown(results)
    (RUNS / "latest.md").write_text(md)
    print(md)
    print(f"wrote {out} and {RUNS / 'latest.md'}", file=sys.stderr)
    return 0


def cmd_compare(args) -> int:
    before, after = ({r["model"]: r for r in json.loads(Path(p).read_text())} for p in (args.before, args.after))

    def delta(x: dict, y: dict, k: str) -> str:
        if x[k] is None or y[k] is None:
            return "n/a"
        return f"{x[k]:.2f} -> {y[k]:.2f} ({y[k] - x[k]:+.2f})"

    def headline(r: dict) -> dict:
        s = r.get("summary") or {}
        if s.get("verified_precision"):   # compare means when the runs were repeated
            return {m: (s.get(f"verified_{m}") or {}).get("mean") for m in ("precision", "recall", "f1")}
        return r["totals"]["verified"]["overall"]

    for m in sorted(set(before) & set(after)):
        x, y = headline(before[m]), headline(after[m])
        print(f"{m}: precision {delta(x, y, 'precision')}, recall {delta(x, y, 'recall')}, f1 {delta(x, y, 'f1')}")
    return 0


def cmd_publish_rules(args) -> int:
    from .publish import review
    docs = [d for d in _sources(Path(args.sources)) if d.get("publish") and (not args.only or d["id"] in args.only)]
    rules_dir = Path(args.rules_dir) if args.rules_dir else store.DEFAULT_DIR
    models = args.model or [DEFAULT_MODEL]
    lines = [f"## Rules review ({', '.join(models)})", "", "| Document | Published | Held back | Model |", "|---|---|---|---|"]
    published = 0
    for d in docs:
        path = ROOT / d["file"]
        if not path.exists():
            lines.append(f"| {d['id']} | missing PDF (run `floodrules fetch`) | |")
            continue
        doc = read_pdf(path, name=f"{d['id']}.pdf")
        labels = load_labels(ROOT / d["labels"])
        best = None
        # try each model in turn, a few times each, until every labeled program passes review
        tries = [(m, a) for m in models for a in range(1, max(1, args.attempts) + 1)]
        for model, attempt in tries:
            print(f"{d['id']}: {model} attempt {attempt}...", file=sys.stderr, flush=True)
            try:
                # retries skip the cache so each attempt is a fresh answer from the model
                run = run_extraction(doc, make_extractor(model), cache_dir=CACHE if attempt == 1 else None)
            except ModelError as e:
                print(f"  failed: {str(e)[:300]}", file=sys.stderr)
                continue
            r = review(run.verified.products, labels)
            for h in r.held:
                print(f"  held {h['program']}: {h['why']} {h.get('extra', '')} {h.get('missing', '')}", file=sys.stderr)
            if best is None or len(r.published) > len(best[1].published):
                best = (run, r, attempt)
            if not r.held:
                break
        if best is None:
            lines.append(f"| {d['id']} | model failed | all | |")
            continue
        run, r, attempt = best
        if r.published:
            store.save(run, d.get("url"), rules_dir=rules_dir, products=r.published, published=d.get("published"),
                       review={**r.as_dict(), "attempt": attempt})
            published += len(r.published)
        held = "; ".join(f"{h['program']}: {h['why']}" + (f" (extra {h['extra']}, missing {h['missing']})" if h.get("extra") or h.get("missing") else "")
                         for h in r.held)
        lines.append(f"| {d['id']} | {', '.join(x.split(' / ')[-1] for x in r.as_dict()['published']) or 'none'} | {held or 'none'} | {run.model} |")
    report = "\n".join(lines) + "\n"
    print(report)
    if args.report:
        Path(args.report).write_text(report)
    return 0 if published else 1


def cmd_costs(args) -> int:
    from . import costs
    data = costs.build(state=args.state)
    problems = costs.check(data)
    if problems:
        for msg in problems:
            print(f"error: {msg}", file=sys.stderr)
        print("not written; the existing file is unchanged", file=sys.stderr)
        return 1
    costs.write(data, Path(args.out))
    f, h = data["flood"], data["homeowners"]
    print(f"flood: {f['policies']} policies {f['window']['from']} to {f['window']['to']}, "
          f"{len(f['tract'])} tract groups, {len(f['town'])} town groups")
    for g, st in f["statewide"].items():
        print(f"  statewide {g}: median ${st['median']:,}, mean ${st['mean']:,} (n={st['n']})")
    print(f"homeowners: {len(h['zip'])} ZIPs, {h['year']}")
    print(f"wrote {args.out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="floodrules")
    p.add_argument("--sources", default=str(ROOT / "evals" / "sources.json"))
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("models", help="list the Ollama models available (Ollama Cloud when OLLAMA_API_KEY is set)").set_defaults(fn=cmd_models)
    sub.add_parser("fetch", help="download the eval PDFs listed in evals/sources.json").set_defaults(fn=cmd_fetch)
    t = sub.add_parser("text", help="print documents' text as the verifier sees it, for writing labels")
    t.add_argument("--only", action="append", help="limit to these document ids")
    t.set_defaults(fn=cmd_text)
    e = sub.add_parser("extract", help="extract and verify rules from one PDF")
    e.add_argument("pdf")
    e.add_argument("--model", default=DEFAULT_MODEL, help="Claude model ID, or ollama:<name> for a local model")
    e.add_argument("--save", action="store_true", help="write verified rules to data/rules")
    e.add_argument("--url", help="source URL recorded with saved rules")
    e.set_defaults(fn=cmd_extract)
    v = sub.add_parser("eval", help="score extraction against the labeled documents")
    v.add_argument("--model", action="append", help="Claude model ID or ollama:<name>; repeat to compare models")
    v.add_argument("--replay", action="store_true", help="rescore saved responses without calling the API")
    v.add_argument("--repeat", type=int, default=1, help="run each document N times; run 2 shows the prompt-caching discount")
    v.add_argument("--runs", type=int, default=1, help="repeat the whole eval N times and report the mean and range")
    v.add_argument("--only", action="append", help="limit to these document ids")
    v.set_defaults(fn=cmd_eval)
    c = sub.add_parser("compare", help="compare two eval runs")
    c.add_argument("before")
    c.add_argument("after")
    c.set_defaults(fn=cmd_compare)
    r = sub.add_parser("publish-rules", help="extract, review against labels and save rules for the map")
    r.add_argument("--model", action="append", help="model to extract with; repeat to fall back to the next model when one fails review")
    r.add_argument("--attempts", type=int, default=3, help="re-run a document up to N times until every labeled program passes review")
    r.add_argument("--only", action="append", help="limit to these document ids")
    r.add_argument("--rules-dir", help="where to save (default data/rules)")
    r.add_argument("--report", help="also write the markdown summary here")
    r.set_defaults(fn=cmd_publish_rules)
    k = sub.add_parser("costs", help="rebuild what-neighbors-paid data from FEMA and Treasury datasets")
    k.add_argument("--state", default="RI")
    k.add_argument("--out", default=str(ROOT / "map" / "data" / "ri-costs.json"))
    k.set_defaults(fn=cmd_costs)
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
