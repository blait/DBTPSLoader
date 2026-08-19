"""부하 워커 프로세스.

워커 하나가 T개 스레드를 돌린다. 각 스레드는 자기 pyodbc 커넥션을 갖고(믹스가
건드리는 DB마다 하나) "트랜잭션 선택 → 실행 → 기록"을 반복한다. 1초 단위 집계는
mp.Queue로 코디네이터에 보낸다.

프로세스와 스레드를 함께 쓰는 이유: pyodbc가 서버 호출 중 GIL을 놓기 때문에
스레드로도 실제 I/O 병렬이 나오고, 프로세스는 파라미터 생성과 집계가 한 코어에
몰리지 않게 한다.
"""
from __future__ import annotations

import random
import re
import threading
import time
from collections import defaultdict

from ..config import RunConfig, TargetDB
from ..db import connect
from ..seed.datagen import Gen
from ..workload.store import build_mix
from .pacing import TokenBucket

LAT_BUCKETS_MS = None  # 레이턴시는 원값(µs 리스트)으로 초·트랜잭션 단위로 보낸다


class _SecondAgg:
    __slots__ = ("count", "errors", "lat_us")

    def __init__(self):
        self.count = 0
        self.errors = 0
        self.lat_us = []


_ERR_VARYING = re.compile(r"\b(?:Process ID|process|session|PID)\s+\d+", re.I)


def _err_key(exc: Exception) -> str:
    """SQLSTATE + 드라이버 메시지에서 발생 건마다 달라지는 식별자를 지운 키.

    **에러는 절대 조용히 세면 안 된다.** 실측 사례: 어떤 런이 실패 78,572건을
    기록했는데 아티팩트 어디에도 메시지가 없어서, FK 범위 버그가 성능 결과로
    읽혔다. SQLSTATE + 메시지로 키를 잡으면 표본 크기를 제한하면서도 어떤 제약이
    걸렸는지는 남는다.

    식별자를 지우는 것이 메시지 자체만큼 중요하다. 교착 상태(1205)는 희생된
    세션의 spid를 메시지에 박아 넣기 때문에("Transaction (Process ID 498) was
    deadlocked...") 원문이 발생 건마다 유일해진다. 실측: 같은 실패 모드 하나가
    서로 다른 키 706개를 만들어 40키 상한에 걸린 뒤 95건이 조용히 버려졌다.
    변하는 식별자를 뭉개면 상한이 의도대로 *종류*를 제한한다.
    """
    msg = str(exc).replace("\n", " ")
    if not msg:
        return exc.__class__.__name__
    return _ERR_VARYING.sub(lambda m: m.group(0).rsplit(" ", 1)[0] + " N", msg)[:300]


def _thread_main(worker_id, thread_id, target, cfg, mix, ctx, shared, stop_event):
    rng = random.Random((worker_id << 16) ^ thread_id ^ 0x5EED)
    g = Gen(seed=(worker_id << 16) ^ thread_id)
    conns = {}

    def conn_for(db):
        c = conns.get(db)
        if c is None:
            c = connect(target, db, autocommit=True)
            conns[db] = c
        return c

    while not stop_event.is_set():
        if shared["bucket"] is not None and not shared["bucket"].acquire(stop_event):
            break
        txn = mix.pick(rng, shared["read_pct"])
        t0 = time.perf_counter()
        ok = True
        try:
            conn = conn_for(txn.database)
            cur = conn.cursor()
            if txn.explicit_tran:
                conn.autocommit = False
            committed = False
            try:
                params = txn.param_fn(g, ctx)
                for sql, p in zip(txn.sql, params):
                    cur.execute(sql, p)
                    if sql.lstrip().upper().startswith("SELECT"):
                        cur.fetchall()
                if txn.explicit_tran:
                    conn.commit()
                    committed = True
            finally:
                if txn.explicit_tran:
                    # 반드시 rollback을 먼저 한다. ODBC 규격상 수동->자동 커밋
                    # 전환은 열린 트랜잭션을 *커밋*하므로, 다중 문장 쓰기가
                    # 중간에 실패했을 때 앞선 문장만 커밋된 채 남는다.
                    # finally는 except보다 먼저 실행되니 뒤에서 손쓸 수도 없다.
                    if not committed:
                        try:
                            conn.rollback()
                        except Exception:  # noqa: BLE001 - 연결이 이미 죽었을 수 있다
                            pass
                    conn.autocommit = True
        except Exception as exc:  # noqa: BLE001
            ok = False
            with shared["lock"]:
                errs = shared["err_samples"]
                key = f"{txn.name}: {_err_key(exc)}"
                if key in errs or len(errs) < 40:
                    errs[key] = errs.get(key, 0) + 1
                else:
                    # 조용히 버리지 않는다. 키 없는 숫자만 남기면 표본이 완전한
                    # 것처럼 보이는데 실제로는 아니다.
                    errs["(over 40-key cap)"] = errs.get("(over 40-key cap)", 0) + 1
            # 실패 시 커넥션을 버리고 다시 만든다 (이미 죽었을 수 있다)
            c = conns.pop(txn.database, None)
            if c is not None:
                try:
                    c.close()
                except Exception:  # noqa: BLE001
                    pass
        lat_us = int((time.perf_counter() - t0) * 1_000_000)

        sec = int(time.time())
        with shared["lock"]:
            agg = shared["aggs"][sec][txn.name]
            agg.count += 1
            if not ok:
                agg.errors += 1
            agg.lat_us.append(lat_us)

    for c in conns.values():
        try:
            c.close()
        except Exception:  # noqa: BLE001
            pass


def worker_main(worker_id: int, target_dict: dict, cfg_dict: dict, workload: dict,
                ctx: dict, metrics_q, control_q) -> None:
    """워커 프로세스의 진입점 (코디네이터가 spawn한다).

    워크로드는 dict로 건네받아 이 프로세스에서 Mix로 조립한다. spawn 방식이라
    부모의 객체를 물려받지 못하고, dict는 pickle이 확실하다.
    """
    target = TargetDB(**target_dict)
    cfg = RunConfig(**cfg_dict)
    mix = build_mix(workload)

    stop_event = threading.Event()
    bucket = None
    if cfg.mode == "open" and cfg.target_tps:
        bucket = TokenBucket(cfg.target_tps / max(cfg.processes, 1))

    shared = {
        "read_pct": cfg.read_pct,
        "bucket": bucket,
        "lock": threading.Lock(),
        "aggs": defaultdict(lambda: defaultdict(_SecondAgg)),
        "err_samples": {},   # "<txn>: <sqlstate+메시지>" -> 건수, 40키 상한
    }

    threads = [
        threading.Thread(
            target=_thread_main,
            args=(worker_id, t, target, cfg, mix, ctx, shared, stop_event),
            daemon=True,
        )
        for t in range(cfg.threads_per_process)
    ]
    for t in threads:
        t.start()

    def flush(upto_sec: int) -> None:
        with shared["lock"]:
            ready = [s for s in shared["aggs"] if s < upto_sec]
            out = {}
            for s in ready:
                out[s] = {
                    name: (a.count, a.errors, a.lat_us)
                    for name, a in shared["aggs"].pop(s).items()
                }
        if out:
            metrics_q.put(("stats", worker_id, out))

    try:
        while not stop_event.is_set():
            # 제어 메시지 수신
            try:
                while True:
                    msg = control_q.get_nowait()
                    if msg.get("stop"):
                        stop_event.set()
                        break
                    if msg.get("read_pct") is not None:
                        shared["read_pct"] = msg["read_pct"]
                    if msg.get("target_tps") is not None and bucket is not None:
                        bucket.set_rate(msg["target_tps"] / max(cfg.processes, 1))
            except Exception:  # noqa: BLE001 - queue.Empty
                pass
            flush(int(time.time()))
            time.sleep(0.25)
    finally:
        stop_event.set()
        for t in threads:
            t.join(timeout=5)
        flush(int(time.time()) + 2)  # 현재 초까지 포함한 마지막 플러시
        with shared["lock"]:
            samples = dict(shared["err_samples"])
        metrics_q.put(("done", worker_id, {"err_samples": samples}))
