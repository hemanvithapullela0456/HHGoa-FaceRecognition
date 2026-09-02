"""Recall benchmark: run the pipeline over eval/identities.csv and tabulate results.

Usage:
    python eval/recall_study.py --no-attest --out eval/results.json

Reports recall per group (public vs private), per engine, and per router path.
Writes a Markdown table you can paste straight into the README.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from faceprov.config import Config          # noqa: E402
from faceprov.pipeline import run_pipeline  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-attest", action="store_true")
    ap.add_argument("--out", default="eval/results.json")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    cfg = Config.load(require_chain=not args.no_attest, require_search=True)
    rows = list(csv.DictReader((ROOT / "eval" / "identities.csv").open()))
    if args.limit:
        rows = rows[: args.limit]

    per_group = defaultdict(lambda: {"n": 0, "hits": 0})
    engine_hits = Counter()
    path_hits = Counter()
    yandex_only = 0
    records = []

    for row in rows:
        img = ROOT / row["probe_image"]
        rec = {"id": row["id"], "group": row["group"], "image": str(img)}
        if not img.exists():
            rec["error"] = "probe image missing"
            records.append(rec)
            continue
        try:
            t0 = time.time()
            res = run_pipeline(str(img), cfg, attest=not args.no_attest)
            rec.update(
                matched=res["matched"],
                path_taken=res["path_taken"],
                entity=res.get("entity_name"),
                best_cosine=(res["match"].get("cosine") if res["matched"] else None),
                seconds=round(time.time() - t0, 1),
            )
            per_group[row["group"]]["n"] += 1
            if res["matched"]:
                per_group[row["group"]]["hits"] += 1
                eng = res["match"].get("engine", "unknown")
                engine_hits[eng] += 1
                path_hits[res["path_taken"]] += 1
                accepted_engines = {c["engine"] for c in res["candidates"] if c["accepted"]}
                if accepted_engines == {"yandex_images"}:
                    yandex_only += 1
        except Exception as e:  # noqa: BLE001
            rec["error"] = repr(e)
        records.append(rec)
        print(json.dumps(rec))

    summary = {
        "per_group": {
            g: {**v, "recall": round(v["hits"] / v["n"], 3) if v["n"] else None}
            for g, v in per_group.items()
        },
        "engine_hits": dict(engine_hits),
        "path_hits": dict(path_hits),
        "yandex_only_hits": yandex_only,
    }
    out = {"summary": summary, "records": records}
    Path(args.out).write_text(json.dumps(out, indent=2))

    print("\n### Recall study\n")
    print("| Group | n | Recall | Yandex-only | Path A | Path B |")
    print("|---|---|---|---|---|---|")
    for g, v in summary["per_group"].items():
        print(f"| {g} | {v['n']} | {v['recall']} | — | "
              f"{path_hits.get('A:entity', 0)} | {path_hits.get('B:visual', 0)} |")
    print(f"\nYandex-only hits (overall): {yandex_only}")


if __name__ == "__main__":
    main()
