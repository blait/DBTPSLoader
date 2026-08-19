"""데이터 시딩 — 대상 DB의 스키마를 조회해 만든 플랜대로 행을 채운다.

이 모듈에는 특정 스키마에 대한 지식이 없다. 무엇을 몇 행 넣을지는 전부
`loadgen.schema.plan`이 라이브 DB를 조회해 만든 시딩 플랜이 정하고, 여기서는
그 플랜을 실행하는 기계만 담당한다.

값 생성은 결정적이다 — `_insert_range()`가 테이블명과 오프셋으로 RNG 시드를
정하므로, 같은 플랜을 서로 다른 인스턴스에 적용하면 같은 데이터가 들어간다.
HT on/off 쌍 비교는 양쪽 데이터가 같아야 성립하므로 이 성질이 전제 조건이다.
"""
from __future__ import annotations

import logging
import threading
import uuid
import zlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Callable, Optional

from ..config import SeedConfig, TargetDB
from ..db import connect
from ..schema.guard import require_empty
from .datagen import Gen

log = logging.getLogger(__name__)

CHUNK = 100_000

ProgressCb = Callable[[str, int, int], None]  # (table, done_rows, target_rows)


@dataclass
class TableSeed:
    database: str
    table: str
    columns: list[str]
    factory: Callable  # (g: Gen, i: int, total: int, ctx: dict) -> tuple
    count_key: str  # ctx key holding target row count


# ---------------------------------------------------------------------------
# Insert machinery
# ---------------------------------------------------------------------------

def _insert_range(target: TargetDB, ts: TableSeed, start: int, end: int, total: int,
                  ctx: dict, batch_size: int, on_rows: Callable[[int], None]) -> None:
    cols = ", ".join(f"[{c}]" for c in ts.columns)
    ph = ", ".join("?" for _ in ts.columns)
    sql = f"INSERT INTO dbo.[{ts.table}] ({cols}) VALUES ({ph})"
    g = Gen(seed=zlib.crc32(f"{ts.table}:{start}".encode()))  # stable across processes/runs
    with connect(target, ts.database, autocommit=False) as conn:
        cur = conn.cursor()
        cur.fast_executemany = True
        batch = []
        for i in range(start, end + 1):
            batch.append(ts.factory(g, i, total, ctx))
            if len(batch) >= batch_size:
                cur.executemany(sql, batch)
                conn.commit()
                on_rows(len(batch))
                batch = []
        if batch:
            cur.executemany(sql, batch)
            conn.commit()
            on_rows(len(batch))


def _set_fk_checks(target: TargetDB, database: str, enable: bool) -> None:
    action = "CHECK" if enable else "NOCHECK"
    with connect(target, database) as conn:
        conn.cursor().execute(
            f"EXEC sp_MSforeachtable 'ALTER TABLE ? {action} CONSTRAINT ALL'"
        )


def _column_meta(cur, table: str) -> list[dict]:
    cur.execute(
        """
        SELECT c.name, t.name AS type_name, c.max_length, c.is_nullable,
               c.is_identity, c.is_computed
        FROM sys.columns c JOIN sys.types t ON c.user_type_id = t.user_type_id
        WHERE c.object_id = OBJECT_ID(?) ORDER BY c.column_id
        """,
        f"dbo.[{table}]",
    )
    return [
        {"name": r[0], "type": r[1], "max_length": r[2], "nullable": bool(r[3]),
         "identity": bool(r[4]), "computed": bool(r[5])}
        for r in cur.fetchall()
    ]


def _generic_value(g: Gen, meta: dict, i: int):
    t = meta["type"]
    if meta["nullable"]:
        return None
    if t in ("int", "bigint", "smallint"):
        return i % 1000 + 1
    if t == "tinyint":
        return i % 100
    if t == "bit":
        return False
    if t in ("decimal", "numeric", "money", "float", "real"):
        return 1.0
    if t in ("nvarchar", "varchar", "nchar", "char", "sysname"):
        maxlen = meta["max_length"]
        nchars = 20 if maxlen == -1 else max(1, (maxlen // 2 if t.startswith("n") else maxlen))
        return f"x{i}"[:nchars].ljust(min(nchars, 2), "x")
    if t in ("datetime2", "datetime", "smalldatetime", "date"):
        return g.dt()
    if t == "time":
        return "12:00:00"
    if t == "uniqueidentifier":
        return str(uuid.UUID(int=g.rng.getrandbits(128)))
    if t in ("varbinary", "binary", "image"):
        return b"\x00"
    return None


def seed_minimal(target: TargetDB, database: str, skip_tables: set[str], rows: int = 10,
                 progress: Optional[ProgressCb] = None) -> list[str]:
    """Insert a handful of rows into every empty non-hot table. Returns failures."""
    failures = []
    with connect(target, database, autocommit=False) as conn:
        cur = conn.cursor()
        cur.execute("SELECT name FROM sys.tables WHERE is_ms_shipped = 0 ORDER BY name")
        tables = [r[0] for r in cur.fetchall()]
        g = Gen(seed=42)
        for t in tables:
            if t in skip_tables:
                continue
            try:
                cur.execute(f"SELECT COUNT(*) FROM dbo.[{t}]")
                if cur.fetchone()[0] > 0:
                    continue
                metas = [m for m in _column_meta(cur, t)
                         if not m["identity"] and not m["computed"]
                         and m["type"] not in ("timestamp", "rowversion")]
                if not metas:
                    continue
                cols = ", ".join(f"[{m['name']}]" for m in metas)
                ph = ", ".join("?" for _ in metas)
                sql = f"INSERT INTO dbo.[{t}] ({cols}) VALUES ({ph})"
                data = [tuple(_generic_value(g, m, i) for m in metas) for i in range(1, rows + 1)]
                cur.executemany(sql, data)
                conn.commit()
                if progress:
                    progress(f"{database}.{t}", rows, rows)
            except Exception as e:  # noqa: BLE001
                conn.rollback()
                failures.append(f"{t}: {str(e)[:200]}")
                log.warning("minimal seed failed for %s.%s: %s", database, t, e)
    return failures


def seed_plan(target: TargetDB, plan: dict, cfg: SeedConfig,
              progress: Optional[ProgressCb] = None,
              stop_event: Optional[threading.Event] = None) -> dict:
    """시딩 플랜을 실행한다.

    `plan`은 `loadgen.schema.plan`이 라이브 스키마를 조회해 만들고 사용자가 UI에서
    수정한 결과다. 형태:

        {"databases": ["Sales", "Sales_Audit"],
         "tables": [{"database": ..., "table": ..., "columns": [...],
                     "rows": 50000, "strategies": {...}}, ...]}

    삽입 순서는 플랜이 정한 순서(FK 위상 정렬)를 따른다. 다만 FK 검사를 삽입 중
    끄기 때문에 순환 FK가 있어도 진행된다.
    """
    tables = [t for t in plan.get("tables", []) if t.get("rows", 0) > 0]
    databases = plan.get("databases") or sorted({t["database"] for t in tables})

    # 여기가 최종 관문이다. API 핸들러에만 두면 CLI 등 다른 경로로 우회된다.
    require_empty(target, databases)

    seeds = [
        TableSeed(database=t["database"], table=t["table"], columns=t["columns"],
                  factory=t["factory"], count_key=t["table"])
        for t in tables
    ]
    ctx = {t["table"]: t["rows"] for t in tables}

    done_lock = threading.Lock()
    done: dict[str, int] = {ts.table: 0 for ts in seeds}

    for db in databases:
        _set_fk_checks(target, db, enable=False)

    jobs = []  # (ts, start, end, total)
    for ts in seeds:
        total = ctx[ts.count_key]
        for start in range(1, total + 1, CHUNK):
            jobs.append((ts, start, min(start + CHUNK - 1, total), total))
    # large jobs first for better pool utilization
    jobs.sort(key=lambda j: -(j[2] - j[1]))

    errors: list[str] = []

    def run_job(job):
        ts, start, end, total = job
        if stop_event and stop_event.is_set():
            return

        def on_rows(n, _t=ts, _total=total):
            with done_lock:
                done[_t.table] += n
                if progress:
                    progress(f"{_t.database}.{_t.table}", done[_t.table], _total)

        _insert_range(target, ts, start, end, total, ctx, cfg.batch_size, on_rows)

    with ThreadPoolExecutor(max_workers=cfg.workers) as pool:
        futures = [pool.submit(run_job, j) for j in jobs]
        for f in as_completed(futures):
            exc = f.exception()
            if exc:
                errors.append(str(exc)[:300])
                log.error("seed job failed: %s", exc)

    # 플랜에 없는 테이블에도 최소 행을 넣어, FK 조인 읽기가 빈 결과를 돌려주지 않게 한다.
    seeded = {ts.table for ts in seeds}
    minimal_failures = []
    for db in databases:
        minimal_failures += seed_minimal(target, db, skip_tables=seeded, progress=progress)

    for db in databases:
        try:
            _set_fk_checks(target, db, enable=True)
        except Exception as e:  # noqa: BLE001
            log.warning("re-enabling FK checks on %s failed: %s", db, e)

    return {
        "targets": dict(ctx),
        "inserted": done,
        "errors": errors,
        "minimal_seed_failures": minimal_failures,
    }
