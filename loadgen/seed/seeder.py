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
import zlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Callable, Optional

from ..config import SeedConfig, TargetDB
from ..db import connect
from ..schema.guard import require_empty
from ..schema.ident import qualify, quote
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
    count_key: str  # 목표 행수를 담은 ctx 키
    schema: str = "dbo"   # dbo가 아닌 스키마도 대상이 된다


# ---------------------------------------------------------------------------
# 삽입 기계
# ---------------------------------------------------------------------------

def _insert_range(target: TargetDB, ts: TableSeed, start: int, end: int, total: int,
                  ctx: dict, batch_size: int, on_rows: Callable[[int], None]) -> None:
    cols = ", ".join(quote(c) for c in ts.columns)
    ph = ", ".join("?" for _ in ts.columns)
    sql = f"INSERT INTO {qualify(ts.schema, ts.table)} ({cols}) VALUES ({ph})"
    # 시드를 테이블명+오프셋으로 정한다. 스키마명까지 넣는 이유: 서로 다른 스키마에
    # 같은 이름의 테이블이 있으면 같은 데이터가 들어가 버린다.
    g = Gen(seed=zlib.crc32(f"{ts.schema}.{ts.table}:{start}".encode()))
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


def _set_fk_checks(target: TargetDB, database: str, enable: bool) -> list[str]:
    """모든 사용자 테이블의 제약을 켜거나 끈다. 실패한 테이블 목록을 돌려준다.

    `sp_MSforeachtable`을 쓰지 않는다. 그것은 (1) 문서화되지 않은 프로시저라 관리형
    인스턴스에서 막힐 수 있고, (2) 한 테이블에서 실패하면 나머지를 건너뛰며,
    (3) 무엇이 실패했는지 알려주지 않는다.

    직접 순회하면 권한이 없는 테이블만 건너뛰고 이유를 남길 수 있다. 삽입 전에
    제약을 끄는 것은 FK 순서가 완벽하지 않아도 진행되게 하려는 것이므로, 일부
    테이블에서 실패해도 전체를 중단할 이유는 없다.
    """
    action = "CHECK" if enable else "NOCHECK"
    failed: list[str] = []
    with connect(target, database) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT s.name, t.name FROM sys.tables t "
            "JOIN sys.schemas s ON s.schema_id = t.schema_id "
            "WHERE t.is_ms_shipped = 0 AND t.temporal_type <> 2"
        )
        tables = cur.fetchall()
        for sch, name in tables:
            try:
                cur.execute(f"ALTER TABLE {qualify(sch, name)} {action} CONSTRAINT ALL")
            except Exception as exc:  # noqa: BLE001
                failed.append(f"{sch}.{name}: {str(exc)[:120]}")
    if failed:
        log.warning("제약 %s 실패 %d개 (권한 또는 시스템 버전 관리 테이블): %s",
                    action, len(failed), failed[0])
    return failed


def seed_minimal(target: TargetDB, database: str, skip: set[str], rows: int = 10,
                 progress: Optional[ProgressCb] = None) -> list[str]:
    """플랜에 없는 빈 테이블에 소량의 행을 넣는다. 실패 목록을 돌려준다.

    목적은 FK 조인 읽기가 빈 결과를 돌려주지 않게 하는 것이다. 부모 테이블이
    비어 있으면 조인이 0행을 반환하는데, 그것도 "성공한 트랜잭션"으로 집계되어
    서버가 일을 거의 하지 않고도 TPS가 높게 나온다.

    `skip`은 "schema.table" 형식이다. 스키마명을 포함하지 않으면 서로 다른 스키마의
    동명 테이블을 구분할 수 없다.
    """
    from ..schema.introspect import introspect
    from ..schema.values import value_for

    failures: list[str] = []
    try:
        tables = introspect(target, database)
    except Exception as exc:  # noqa: BLE001
        return [f"{database}: 스키마 조회 실패 — {str(exc)[:200]}"]

    with connect(target, database, autocommit=False) as conn:
        cur = conn.cursor()
        g = Gen(seed=42)
        for key, t in sorted(tables.items()):
            if key in skip or t.row_count > 0:
                continue
            insertable = [c for c in t.columns if c.insertable]
            if not insertable:
                continue   # IDENTITY만 있는 테이블 — 넣을 것이 없다
            cols = ", ".join(quote(c.name) for c in insertable)
            ph = ", ".join("?" for _ in insertable)
            sql = f"INSERT INTO {qualify(t.schema, t.name)} ({cols}) VALUES ({ph})"
            fk_of = {fk.columns[0]: fk.ref_table.split(".")[-1]
                     for fk in t.foreign_keys if len(fk.columns) == 1}
            try:
                data = [
                    tuple(value_for(c, g, i, rows, {}, fk_parent=fk_of.get(c.name))
                          for c in insertable)
                    for i in range(1, rows + 1)
                ]
                cur.executemany(sql, data)
                conn.commit()
                if progress:
                    progress(f"{database}.{key}", rows, rows)
            except Exception as e:  # noqa: BLE001
                conn.rollback()
                # 조용히 넘기지 않는다 — 사용자가 왜 조인이 빈 결과를 주는지
                # 알 수 있어야 한다.
                failures.append(f"{key}: {str(e)[:200]}")
                log.warning("최소 시딩 실패 %s.%s: %s", database, key, e)
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
        TableSeed(database=t["database"], table=t["table"],
                  schema=t.get("schema", "dbo"), columns=t["columns"],
                  factory=t["factory"], count_key=t["table"].lower())
        for t in tables
    ]
    # ctx 키는 테이블명 소문자다 — 워크로드의 `of` 참조와 맞춰야 한다.
    # 서로 다른 스키마에 동명 테이블이 있으면 큰 쪽을 남긴다 (FK 부모로 쓰일 때
    # 범위가 좁으면 위반이 나므로, 좁은 쪽을 남기는 것이 더 위험하다).
    ctx: dict[str, int] = {}
    for t in tables:
        k = t["table"].lower()
        ctx[k] = max(ctx.get(k, 0), t["rows"])

    done_lock = threading.Lock()
    done: dict[str, int] = {f"{ts.schema}.{ts.table}": 0 for ts in seeds}

    fk_off_failures: list[str] = []
    for db in databases:
        fk_off_failures += _set_fk_checks(target, db, enable=False)

    jobs = []  # (ts, start, end, total)
    for t, ts in zip(tables, seeds):
        total = t["rows"]     # 플랜의 값을 그대로 쓴다 (ctx의 max가 아니라)
        for start in range(1, total + 1, CHUNK):
            jobs.append((ts, start, min(start + CHUNK - 1, total), total))
    # 큰 작업부터 — 스레드풀 활용도를 높인다
    jobs.sort(key=lambda j: -(j[2] - j[1]))

    errors: list[str] = []

    def run_job(job):
        ts, start, end, total = job
        if stop_event and stop_event.is_set():
            return

        def on_rows(n, _t=ts, _total=total):
            key = f"{_t.schema}.{_t.table}"
            with done_lock:
                done[key] += n
                if progress:
                    progress(f"{_t.database}.{key}", done[key], _total)

        _insert_range(target, ts, start, end, total, ctx, cfg.batch_size, on_rows)

    with ThreadPoolExecutor(max_workers=cfg.workers) as pool:
        futures = [pool.submit(run_job, j) for j in jobs]
        for f in as_completed(futures):
            exc = f.exception()
            if exc:
                errors.append(str(exc)[:300])
                log.error("시딩 작업 실패: %s", exc)

    # 플랜에 없는 테이블에도 최소 행을 넣어, FK 조인 읽기가 빈 결과를 돌려주지 않게 한다.
    seeded = {f"{ts.schema}.{ts.table}" for ts in seeds}
    minimal_failures: list[str] = []
    for db in databases:
        minimal_failures += seed_minimal(target, db, skip=seeded, progress=progress)

    fk_on_failures: list[str] = []
    for db in databases:
        try:
            fk_on_failures += _set_fk_checks(target, db, enable=True)
        except Exception as e:  # noqa: BLE001
            fk_on_failures.append(f"{db}: {str(e)[:200]}")
            log.warning("%s의 제약 재활성화 실패: %s", db, e)

    return {
        "targets": dict(ctx),
        "inserted": done,
        "errors": errors,
        "minimal_seed_failures": minimal_failures,
        # 제약을 끄거나 켜지 못한 테이블. 끄지 못했으면 FK 순서 문제로 삽입이
        # 실패할 수 있고, 켜지 못했으면 DB가 제약 미검증 상태로 남는다 —
        # 어느 쪽도 조용히 넘기면 안 된다.
        "constraint_off_failures": fk_off_failures,
        "constraint_on_failures": fk_on_failures,
    }
