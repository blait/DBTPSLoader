"""Aggregate per-second worker stats into run-level metrics."""
from __future__ import annotations

import threading
from collections import defaultdict

from hdrh.histogram import HdrHistogram


def _pcts(lat_us: list[int]) -> dict:
    if not lat_us:
        return {"p50": 0, "p95": 0, "p99": 0, "max": 0}
    s = sorted(lat_us)
    n = len(s)
    return {
        "p50": s[int(n * 0.50)] / 1000.0,
        "p95": s[min(int(n * 0.95), n - 1)] / 1000.0,
        "p99": s[min(int(n * 0.99), n - 1)] / 1000.0,
        "max": s[-1] / 1000.0,
    }


class RunCollector:
    """Accumulates: 1-second time series + whole-run per-txn histograms."""

    def __init__(self, warmup_until_epoch: float):
        self.lock = threading.Lock()
        self.warmup_until = warmup_until_epoch
        # sec -> txn -> [count, errors, [lat_us...]]
        self._pending: dict[int, dict[str, list]] = defaultdict(
            lambda: defaultdict(lambda: [0, 0, []])
        )
        self.hist: dict[str, HdrHistogram] = {}
        self.totals: dict[str, dict] = defaultdict(lambda: {"count": 0, "errors": 0})
        self.timeseries: list[dict] = []
        self._flushed_secs: set[int] = set()
        # "<txn>: <driver message>" -> count, merged from every worker at exit
        self.err_samples: dict[str, int] = {}

    def add_err_samples(self, samples: dict[str, int]) -> None:
        with self.lock:
            for key, n in samples.items():
                self.err_samples[key] = self.err_samples.get(key, 0) + n

    def add(self, sec_stats: dict) -> None:
        with self.lock:
            for sec, txns in sec_stats.items():
                for name, (count, errors, lat_us) in txns.items():
                    slot = self._pending[sec][name]
                    slot[0] += count
                    slot[1] += errors
                    slot[2].extend(lat_us)

    def flush_completed(self, now_epoch: float) -> list[dict]:
        """Finalize seconds older than now-2s into time-series points."""
        new_points = []
        with self.lock:
            ready = sorted(s for s in self._pending if s < int(now_epoch) - 1)
            for sec in ready:
                txns = self._pending.pop(sec)
                total_count = sum(v[0] for v in txns.values())
                total_errors = sum(v[1] for v in txns.values())
                all_lat = [x for v in txns.values() for x in v[2]]
                point = {
                    "ts": sec,
                    "tps": total_count,
                    "errors": total_errors,
                    "warmup": sec < self.warmup_until,
                    **_pcts(all_lat),
                }
                self.timeseries.append(point)
                new_points.append(point)
                if sec >= self.warmup_until:
                    for name, (count, errors, lat_us) in txns.items():
                        self.totals[name]["count"] += count
                        self.totals[name]["errors"] += errors
                        h = self.hist.get(name)
                        if h is None:
                            h = self.hist[name] = HdrHistogram(1, 60_000_000, 3)
                        for us in lat_us:
                            h.record_value(max(us, 1))
        return new_points

    def summary(self) -> dict:
        with self.lock:
            steady = [p for p in self.timeseries if not p["warmup"]]
            out = {
                "steady_seconds": len(steady),
                "total_txns": sum(p["tps"] for p in steady),
                "total_errors": sum(p["errors"] for p in steady),
                "avg_tps": round(sum(p["tps"] for p in steady) / len(steady), 1) if steady else 0,
                "per_txn": {},
            }
            if self.err_samples:
                # Descending so the dominant failure is the first thing read.
                out["err_samples"] = dict(sorted(self.err_samples.items(),
                                                 key=lambda kv: -kv[1]))
            for name, h in self.hist.items():
                out["per_txn"][name] = {
                    "count": self.totals[name]["count"],
                    "errors": self.totals[name]["errors"],
                    "p50_ms": h.get_value_at_percentile(50) / 1000.0,
                    "p95_ms": h.get_value_at_percentile(95) / 1000.0,
                    "p99_ms": h.get_value_at_percentile(99) / 1000.0,
                    "max_ms": h.get_max_value() / 1000.0,
                    "mean_ms": round(h.get_mean_value() / 1000.0, 3),
                }
            return out
