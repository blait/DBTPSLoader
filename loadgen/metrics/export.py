"""Persist run artifacts: meta.json, timeseries.jsonl/csv, summary.json."""
from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

RUNS_DIR = Path(__file__).resolve().parent.parent.parent / "runs"


def new_run_id(label: str) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in label)
    return f"{ts}_{safe}"


def run_dir(run_id: str) -> Path:
    d = RUNS_DIR / run_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_meta(run_id: str, meta: dict) -> None:
    (run_dir(run_id) / "meta.json").write_text(json.dumps(meta, indent=2, default=str))


def append_timeseries(run_id: str, points: list[dict]) -> None:
    if not points:
        return
    with open(run_dir(run_id) / "timeseries.jsonl", "a") as f:
        for p in points:
            f.write(json.dumps(p) + "\n")


def write_summary(run_id: str, summary: dict) -> None:
    d = run_dir(run_id)
    (d / "summary.json").write_text(json.dumps(summary, indent=2))
    # CSV mirror of the time series for spreadsheet use
    ts_file = d / "timeseries.jsonl"
    if ts_file.exists():
        points = [json.loads(line) for line in ts_file.read_text().splitlines() if line]
        if points:
            with open(d / "timeseries.csv", "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=list(points[0].keys()))
                w.writeheader()
                w.writerows(points)


def list_runs() -> list[dict]:
    out = []
    if not RUNS_DIR.exists():
        return out
    for d in sorted(RUNS_DIR.iterdir(), reverse=True):
        meta_f = d / "meta.json"
        if not meta_f.exists():
            continue
        meta = json.loads(meta_f.read_text())
        summary_f = d / "summary.json"
        meta["run_id"] = d.name
        meta["has_summary"] = summary_f.exists()
        if summary_f.exists():
            s = json.loads(summary_f.read_text())
            meta["avg_tps"] = s.get("avg_tps")
            meta["total_errors"] = s.get("total_errors")
        out.append(meta)
    return out


def load_run(run_id: str) -> dict:
    d = RUNS_DIR / run_id
    out = {"run_id": run_id}
    for name in ("meta", "summary"):
        f = d / f"{name}.json"
        if f.exists():
            out[name] = json.loads(f.read_text())
    ts = d / "timeseries.jsonl"
    if ts.exists():
        out["timeseries"] = [json.loads(l) for l in ts.read_text().splitlines() if l]
    return out
