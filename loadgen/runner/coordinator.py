"""Coordinator: spawns worker processes, relays control, collects metrics."""
from __future__ import annotations

import logging
import multiprocessing as mp
import queue
import threading
import time
from typing import Callable, Optional

from ..config import RunConfig, TargetDB
from ..metrics.collector import RunCollector
from ..metrics.export import append_timeseries, new_run_id, write_meta, write_summary
from ..schema.ranges import id_ranges
from .worker import worker_main

log = logging.getLogger(__name__)


class Run:
    def __init__(self, target: TargetDB, cfg: RunConfig, workload: dict,
                 on_point: Optional[Callable[[dict], None]] = None):
        self.target = target
        self.cfg = cfg
        self.workload = workload
        self.on_point = on_point
        # 워크로드 이름을 넣는다 — cfg에는 더 이상 profile이 없다.
        self.run_id = new_run_id(
            f"{target.label}_{workload.get('name', 'workload')}_{cfg.mode}")
        self.status = "created"
        self.started_at: Optional[float] = None
        self.ended_at: Optional[float] = None
        self._procs: list[mp.Process] = []
        self._control_qs: list[mp.Queue] = []
        self._metrics_q: Optional[mp.Queue] = None
        self._collector: Optional[RunCollector] = None
        self._pump: Optional[threading.Thread] = None
        self._stop = threading.Event()

    # ------------------------------------------------------------------ api

    def start(self) -> None:
        # id 범위는 대상 DB에 실제로 들어 있는 것에서 읽는다 (MAX(Id) 조회).
        # 추정값을 쓰면 안 되는 이유: 쓰기가 FK 부모의 MAX(Id)를 넘는 id를 뽑으면
        # 매 시도가 error 547이 되고, 실패 경로가 커넥션을 매번 다시 여는 탓에
        # 성능 결과가 아니라 실패 표본을 측정하게 된다. ctx는 meta.json에 기록해
        # 워커가 실제로 쓴 범위가 런 기록에 남게 한다.
        ctx, unresolved = id_ranges(self.target, self.workload)
        # 파라미터가 **실제로 참조하는** 범위만 필수다. 워크로드의 `tables`에는
        # 조인 상대가 들어 있지만 그 테이블의 id를 파라미터로 쓰지 않을 수 있다
        # (복합 PK 테이블을 자식으로 조인하는 경우 — 범위가 없는 것이 정상이다).
        needed = {
            spec["of"].lower()
            for txn in self.workload.get("txns", [])
            if not txn.get("disabled")
            for stmt in (txn.get("params") or [])
            for spec in stmt
            if spec.get("gen") in ("skewed_id", "uniform_id") and spec.get("of")
        }
        missing = sorted(needed - set(ctx))
        # 없으면 파라미터가 상수가 되거나 예외가 나고, 조회는 0행을 돌려주면서
        # "성공"으로 집계된다 — 부하가 조용히 무의미해진다. 시딩을 건너뛴 대상에
        # 부하를 거는 실수도 여기서 걸린다.
        if missing:
            raise RuntimeError(
                f"[{self.target.label}] 파라미터가 참조하는 id 범위를 확정하지 "
                f"못했다 — 시딩 상태와 PK 타입을 확인할 것: {', '.join(missing[:5])}"
                + (f" 외 {len(missing) - 5}개" if len(missing) > 5 else ""))
        if unresolved:
            # 파라미터가 쓰지 않는 테이블이므로 진행하되, 기록은 남긴다.
            log.info("id 범위를 얻지 못한 테이블 %d개 (파라미터가 참조하지 않음): %s",
                     len(unresolved), ", ".join(unresolved[:5]))
        mp_ctx = mp.get_context("spawn")
        self._metrics_q = mp_ctx.Queue()
        self.started_at = time.time()
        self._collector = RunCollector(warmup_until_epoch=self.started_at + self.cfg.warmup_sec)

        # 워크로드를 그대로 스냅샷한다. 사용자가 UI에서 SQL·가중치를 고칠 수 있으므로,
        # 이름만 남기면 나중에 무엇을 측정했는지 재현되지 않는다.
        write_meta(self.run_id, {
            "run_id": self.run_id,
            "label": self.target.label,
            "host": self.target.host,
            "config": self.cfg.model_dump(),
            "workload": self.workload,
            "ctx": ctx,
            "started_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.started_at)),
        })

        for w in range(self.cfg.processes):
            cq = mp_ctx.Queue()
            p = mp_ctx.Process(
                target=worker_main,
                args=(w, self.target.model_dump(), self.cfg.model_dump(),
                      self.workload, ctx, self._metrics_q, cq),
                daemon=True,
            )
            p.start()
            self._procs.append(p)
            self._control_qs.append(cq)

        self.status = "running"
        self._pump = threading.Thread(target=self._pump_loop, daemon=True)
        self._pump.start()

    def update(self, read_pct: Optional[int] = None, target_tps: Optional[int] = None) -> None:
        msg = {}
        if read_pct is not None:
            msg["read_pct"] = read_pct
            self.cfg.read_pct = read_pct
        if target_tps is not None:
            msg["target_tps"] = target_tps
            self.cfg.target_tps = target_tps
        if msg:
            for q in self._control_qs:
                q.put(msg)

    def stop(self) -> None:
        if self.status not in ("running",):
            return
        self.status = "stopping"
        for q in self._control_qs:
            q.put({"stop": True})

    # ------------------------------------------------------------- internals

    def _pump_loop(self) -> None:
        deadline = self.started_at + self.cfg.warmup_sec + self.cfg.duration_sec
        done_workers: set[int] = set()
        try:
            while True:
                now = time.time()
                if self.status == "running" and now >= deadline:
                    self.stop()
                try:
                    kind, wid, payload = self._metrics_q.get(timeout=0.5)
                    if kind == "stats":
                        self._collector.add(payload)
                    elif kind == "done":
                        done_workers.add(wid)
                        if payload and payload.get("err_samples"):
                            self._collector.add_err_samples(payload["err_samples"])
                except queue.Empty:
                    pass
                points = self._collector.flush_completed(time.time())
                if points:
                    append_timeseries(self.run_id, points)
                    if self.on_point:
                        for p in points:
                            self.on_point(p)
                if len(done_workers) >= len(self._procs):
                    break
                if self.status == "stopping" and all(not p.is_alive() for p in self._procs):
                    break
        finally:
            # drain remaining stats
            try:
                while True:
                    kind, wid, payload = self._metrics_q.get_nowait()
                    if kind == "stats":
                        self._collector.add(payload)
                    elif kind == "done" and payload and payload.get("err_samples"):
                        self._collector.add_err_samples(payload["err_samples"])
            except queue.Empty:
                pass
            points = self._collector.flush_completed(time.time() + 5)
            append_timeseries(self.run_id, points)
            for p in self._procs:
                p.join(timeout=10)
                if p.is_alive():
                    p.terminate()
            self.ended_at = time.time()
            summary = self._collector.summary()
            summary["ended_at_utc"] = time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.ended_at))
            write_summary(self.run_id, summary)
            self.status = "finished"
            log.info("run %s finished: %s", self.run_id, summary.get("avg_tps"))

    # -------------------------------------------------------------- snapshot

    def snapshot(self) -> dict:
        recent = self._collector.timeseries[-60:] if self._collector else []
        return {
            "run_id": self.run_id,
            "status": self.status,
            "config": self.cfg.model_dump(),
            "label": self.target.label,
            "started_at": self.started_at,
            "recent": recent,
        }
