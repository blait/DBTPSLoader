"""부하 파라미터가 뽑을 id 범위를 대상 DB에서 읽는다.

워크로드의 SQL은 `?` 파라미터에 id를 넣어 실행된다. 그 id가 실제로 존재하는
행을 가리켜야 부하가 의미를 갖는다 — 없는 id를 조회하면 0행이 돌아와 서버가
일을 거의 하지 않고, 없는 FK 부모를 참조하면 매 시도가 error 547이 된다.

그래서 범위를 추정하지 않고 `MAX()`로 읽는다. 인덱스 seek 한 번이라 비용은
무시할 수 있고, 시딩이 실제로 무엇을 남겼는지 아는 유일한 방법이다.
"""
from __future__ import annotations

import logging

from ..config import TargetDB
from ..db import connect
from .ident import object_name, quote

log = logging.getLogger(__name__)

# 숫자형 PK만 범위로 쓸 수 있다. uniqueidentifier·문자열 PK는 MAX()가 의미 없고
# 순번으로 뽑을 수도 없으므로 건너뛴다 (그 테이블을 쓰는 부하는 워크로드 초안이
# 만들지 않는다).
_NUMERIC_PK = ("int", "bigint", "smallint", "tinyint", "decimal", "numeric")


def _pk_column(cur, schema: str, table: str) -> tuple[str, str] | None:
    """(컬럼명, 타입). 단일 컬럼 숫자형 PK가 아니면 None.

    복합 PK를 걸러내는 것이 핵심이다. 복합 PK 테이블에 단일 값을 넣으면 조회가
    항상 0행을 돌려주는데, 그것도 성공으로 집계되어 부하가 조용히 무의미해진다.
    """
    cur.execute(
        """
        SELECT c.name, ty.name
        FROM sys.indexes i
        JOIN sys.index_columns ic ON ic.object_id = i.object_id
                                 AND ic.index_id = i.index_id
        JOIN sys.columns c ON c.object_id = ic.object_id
                          AND c.column_id = ic.column_id
        JOIN sys.types ty ON ty.user_type_id = c.user_type_id
        WHERE i.object_id = OBJECT_ID(?) AND i.is_primary_key = 1
        ORDER BY ic.key_ordinal
        """,
        object_name(schema, table),
    )
    cols = cur.fetchall()
    if len(cols) != 1:
        return None                      # PK 없음 또는 복합 PK
    name, type_name = cols[0]
    if type_name not in _NUMERIC_PK:
        return None                      # uniqueidentifier·문자열 PK
    return name, type_name


def id_ranges(target: TargetDB, workload: dict) -> dict[str, int]:
    """{테이블명(소문자): MAX(pk)} — 워크로드가 건드리는 테이블에 대해서만.

    조회에 실패한 테이블은 결과에서 빠진다. 0을 넣지 않는 이유: 빈 범위를 주면
    파라미터 생성이 조용히 무의미한 값을 내놓는다. 키가 없으면 부하 시작 시
    `coordinator`가 그 사실을 드러내며 거부한다.
    """
    # 워크로드가 실제로 참조하는 테이블만 조회한다. 스키마 전체를 도는 것은
    # 200 테이블짜리 DB에서 불필요한 왕복이다.
    wanted: dict[str, set[tuple[str, str]]] = {}
    for txn in workload.get("txns", []):
        db = txn.get("database")
        if not db:
            continue
        # 워크로드는 "schema.table" 또는 "table"을 담을 수 있다 (구버전 호환).
        for ref in txn.get("tables", []):
            sch, _, name = ref.rpartition(".")
            wanted.setdefault(db, set()).add((sch or "dbo", name))

    out: dict[str, int] = {}
    skipped: list[str] = []
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
            for schema, table in sorted(tables):
                key = f"{schema}.{table}"
                try:
                    pk = _pk_column(cur, schema, table)
                    if not pk:
                        skipped.append(f"{key} (단일 숫자형 PK 없음)")
                        continue
                    col, _ = pk
                    mx = cur.execute(
                        f"SELECT MAX({quote(col)}) FROM {object_name(schema, table)}"
                    ).fetchone()[0]
                except Exception as exc:  # noqa: BLE001
                    log.warning("id 범위 조회 실패 (%s.%s): %s", db, key, exc)
                    continue
                if not mx:
                    log.warning("%s.%s: 행이 없다 — 이 테이블을 쓰는 부하는 빈 결과가 된다",
                                db, key)
                    continue
                # 키는 테이블명만 쓴다 — 워크로드의 `of` 참조와 맞춘다.
                out[table.lower()] = int(mx)
        finally:
            conn.close()
    if skipped:
        log.info("id 범위를 건너뛴 테이블 %d개: %s", len(skipped), ", ".join(skipped[:5]))
    return out
