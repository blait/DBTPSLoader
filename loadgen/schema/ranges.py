"""부하 파라미터가 뽑을 id 범위를 대상 DB에서 읽는다.

워크로드의 SQL은 `?` 파라미터에 id를 넣어 실행된다. 그 id가 실제로 존재하는
행을 가리켜야 부하가 의미를 갖는다 — 없는 id를 조회하면 0행이 돌아와 서버가
일을 거의 하지 않고, 없는 FK 부모를 참조하면 매 시도가 error 547이 된다.

그래서 범위를 추정하지 않고 `MAX(Id)`로 읽는다. 인덱스 seek 한 번이라 비용은
무시할 수 있고, 시딩이 실제로 무엇을 남겼는지 아는 유일한 방법이다.
"""
from __future__ import annotations

import logging

from ..config import TargetDB
from ..db import connect

log = logging.getLogger(__name__)


def _pk_column(cur, table: str) -> str | None:
    """단일 컬럼 PK의 이름. 복합 PK거나 PK가 없으면 None."""
    cur.execute(
        """
        SELECT c.name
        FROM sys.indexes i
        JOIN sys.index_columns ic ON ic.object_id = i.object_id
                                 AND ic.index_id = i.index_id
        JOIN sys.columns c ON c.object_id = ic.object_id
                          AND c.column_id = ic.column_id
        WHERE i.object_id = OBJECT_ID(?) AND i.is_primary_key = 1
        ORDER BY ic.key_ordinal
        """,
        f"dbo.[{table}]",
    )
    cols = [r[0] for r in cur.fetchall()]
    return cols[0] if len(cols) == 1 else None


def id_ranges(target: TargetDB, workload: dict) -> dict[str, int]:
    """{테이블명(소문자): MAX(pk)} — 워크로드가 건드리는 테이블에 대해서만.

    조회에 실패한 테이블은 결과에서 빠진다. 0을 넣지 않는 이유: 빈 범위를 주면
    파라미터 생성이 조용히 무의미한 값을 내놓는다. 키가 없으면 워크로드 초안이
    그 사실을 드러내며 실패하는 편이 낫다.
    """
    # 워크로드가 실제로 참조하는 테이블만 조회한다. 스키마 전체를 도는 것은
    # 200 테이블짜리 DB에서 불필요한 왕복이다.
    wanted: dict[str, set[str]] = {}
    for txn in workload.get("txns", []):
        db = txn.get("database")
        if not db:
            continue
        wanted.setdefault(db, set()).update(txn.get("tables", []))

    out: dict[str, int] = {}
    for db, tables in wanted.items():
        if not tables:
            continue
        try:
            conn = connect(target, db, autocommit=True)
        except Exception as exc:  # noqa: BLE001
            log.warning("id 범위 조회 실패 (%s 연결): %s", db, exc)
            continue
        try:
            cur = conn.cursor()
            for table in sorted(tables):
                try:
                    pk = _pk_column(cur, table)
                    if not pk:
                        log.info("%s.%s: 단일 컬럼 PK가 없어 id 범위를 건너뜀", db, table)
                        continue
                    mx = cur.execute(f"SELECT MAX([{pk}]) FROM dbo.[{table}]").fetchone()[0]
                except Exception as exc:  # noqa: BLE001
                    log.warning("id 범위 조회 실패 (%s.%s): %s", db, table, exc)
                    continue
                if not mx:
                    log.warning("%s.%s: 행이 없다 — 이 테이블을 쓰는 부하는 빈 결과가 된다",
                                db, table)
                    continue
                out[table.lower()] = int(mx)
        finally:
            conn.close()
    return out
