"""스키마 메타데이터로 트랜잭션 믹스를 초안 작성한다.

스키마만으로는 실제 쿼리 패턴을 알 수 없다. 여기서 만드는 것은 **초안**이고,
사용자가 UI에서 가중치·SQL을 고치는 것이 전제다. 그래서 판단 근거(`why`)를
각 트랜잭션에 붙여, 무엇을 왜 넣었는지 보고 고칠 수 있게 한다.

초안 규칙:
  PK                    → 단건 조회
  FK                    → 부모와 조인
  NC 인덱스             → 인덱스 키로 범위 조회
  IDENTITY + 날짜 컬럼  → append INSERT
  일반 갱신 컬럼        → UPDATE ... WHERE PK

읽기는 편중 분포(`skewed_id`), UPDATE 대상은 균등(`uniform_id`)을 쓴다.
이 구분은 타협 대상이 아니다 — UPDATE에 편중을 쓰면 모든 커넥션이 같은 소수의
행을 잠그려 들어 락 컨보이가 생기고, 서버가 한가한데 처리량이 평탄해진다.
"""
from __future__ import annotations

import logging

from ..schema.introspect import Column, Table

log = logging.getLogger(__name__)

_DATE_TYPES = ("datetime", "datetime2", "smalldatetime", "date")
_TEXT_TYPES = ("nvarchar", "varchar", "nchar", "char")
_NUM_TYPES = ("int", "bigint", "smallint", "tinyint", "decimal", "numeric",
              "money", "float", "real")


def _param_for(c: Column) -> dict:
    """컬럼 하나에 넣을 파라미터 명세."""
    t = c.type
    if t in _DATE_TYPES:
        return {"gen": "datetime"}
    if t == "bit":
        return {"gen": "bit"}
    if t == "uniqueidentifier":
        return {"gen": "uuid"}
    if t in ("varbinary", "binary", "image"):
        return {"gen": "const", "value": None}
    if t in _TEXT_TYPES:
        # max_length는 바이트. n계열은 2바이트/문자이고 -1은 MAX.
        nbytes = 80 if c.max_length in (-1, None) else min(c.max_length, 400)
        if "email" in c.name.lower():
            return {"gen": "email"}
        if any(k in c.name.lower() for k in ("name", "title")):
            return {"gen": "name"}
        return {"gen": "text", "bytes": max(2, nbytes)}
    if t in ("decimal", "numeric", "money", "float", "real"):
        return {"gen": "decimal", "min": 0, "max": 10000}
    if t in ("int", "bigint", "smallint"):
        return {"gen": "int", "min": 1, "max": 1000}
    if t == "tinyint":
        return {"gen": "int", "min": 0, "max": 100}
    return {"gen": "const", "value": None}


def _fk_param(fk_col: str, t: Table) -> dict | None:
    """FK 컬럼이면 부모 테이블 범위에서 뽑는 명세. 아니면 None."""
    for fk in t.foreign_keys:
        if fk_col in fk.columns and len(fk.columns) == 1:
            parent = fk.ref_table.split(".")[-1]
            return {"gen": "skewed_id", "of": parent}
    return None


def _reads(t: Table, db: str) -> list[dict]:
    out = []
    pk = t.identity_pk or t.single_pk
    if pk:
        cols = [c.name for c in t.columns[:6]]
        out.append({
            "name": f"{t.name.lower()}_by_pk", "kind": "read", "weight": 30,
            "database": db, "tables": [t.name],
            "sql": [f"SELECT {', '.join(f'[{c}]' for c in cols)} "
                    f"FROM dbo.[{t.name}] WHERE [{pk}] = ?"],
            "params": [[{"gen": "skewed_id", "of": t.name}]],
            "why": f"PK [{pk}] 단건 조회 — 인덱스 seek",
        })

    # FK 조인. 부모의 표시용 컬럼을 함께 읽는다.
    for fk in t.foreign_keys[:2]:
        if len(fk.columns) != 1:
            continue  # 복합 FK는 초안에서 제외 (사용자가 직접 작성)
        parent = fk.ref_table.split(".")[-1]
        out.append({
            "name": f"{t.name.lower()}_join_{parent.lower()}", "kind": "read", "weight": 15,
            "database": db, "tables": [t.name, parent],
            "sql": [f"SELECT TOP 20 c.*, p.[{fk.ref_columns[0]}] "
                    f"FROM dbo.[{t.name}] c "
                    f"JOIN dbo.[{parent}] p ON p.[{fk.ref_columns[0]}] = c.[{fk.columns[0]}] "
                    f"WHERE c.[{fk.columns[0]}] = ?"],
            "params": [[{"gen": "skewed_id", "of": parent}]],
            "why": f"FK {fk.name} 조인 — [{fk.columns[0]}] → {parent}",
        })

    # NC 인덱스 키로 범위 조회. 인덱스를 실제로 타는 쿼리를 만드는 것이 목적이다.
    for ix in t.indexes:
        if ix.primary_key or not ix.columns:
            continue
        key = ix.columns[0]
        col = next((c for c in t.columns if c.name == key), None)
        if col is None:
            continue
        param = _fk_param(key, t) or _param_for(col)
        order = f" ORDER BY [{pk}] DESC" if pk else ""
        out.append({
            "name": f"{t.name.lower()}_by_{key.lower()}", "kind": "read", "weight": 15,
            "database": db, "tables": [t.name],
            "sql": [f"SELECT TOP 50 * FROM dbo.[{t.name}] WHERE [{key}] = ?{order}"],
            "params": [[param]],
            "why": f"NC 인덱스 {ix.name} 키 [{key}] 범위 조회",
        })
        break  # 테이블당 인덱스 조회 1개면 충분하다
    return out


def _writes(t: Table, db: str) -> list[dict]:
    out = []
    insertable = [c for c in t.columns if c.insertable]
    has_date = any(c.type in _DATE_TYPES for c in t.columns)

    # append INSERT — IDENTITY PK + 날짜 컬럼이면 로그성으로 본다.
    if insertable and t.identity_pk and has_date:
        params = []
        for c in insertable:
            params.append(_fk_param(c.name, t) or _param_for(c))
        cols = ", ".join(f"[{c.name}]" for c in insertable)
        ph = ", ".join("?" for _ in insertable)
        out.append({
            "name": f"{t.name.lower()}_insert", "kind": "write", "weight": 40,
            "database": db, "tables": [t.name],
            "sql": [f"INSERT INTO dbo.[{t.name}] ({cols}) VALUES ({ph})"],
            "params": [params],
            "why": f"IDENTITY PK + 날짜 컬럼 → append-only 삽입 ({len(insertable)}개 컬럼)",
        })

    # UPDATE ... WHERE PK — 대상은 반드시 균등 분포.
    pk = t.single_pk
    if pk:
        target = next((c for c in t.columns
                       if c.insertable and not _fk_param(c.name, t)
                       and (c.type in _TEXT_TYPES or c.type in _NUM_TYPES)), None)
        if target:
            out.append({
                "name": f"{t.name.lower()}_update", "kind": "write", "weight": 10,
                "database": db, "tables": [t.name],
                "sql": [f"UPDATE dbo.[{t.name}] SET [{target.name}] = ? WHERE [{pk}] = ?"],
                "params": [[_param_for(target), {"gen": "uniform_id", "of": t.name}]],
                "why": f"[{target.name}] 갱신. 대상 id는 균등 분포 — 편중을 쓰면 "
                       f"모든 커넥션이 같은 행을 잠그려 들어 직렬화된다",
            })
    return out


def draft_workload(tables_by_db: dict[str, dict[str, Table]],
                   name: str = "draft",
                   max_tables: int = 12) -> dict:
    """스키마 → 트랜잭션 믹스 초안.

    행수가 많은 테이블을 우선한다 — 부하가 실제 데이터 분포를 닮게 하려면
    큰 테이블을 건드려야 한다. `max_tables`로 제한하는 이유는 200 테이블짜리
    스키마에서 트랜잭션 수백 개를 만들면 사용자가 검토할 수 없기 때문이다.
    """
    ranked = sorted(
        ((db, t) for db, tables in tables_by_db.items() for t in tables.values()),
        key=lambda x: -x[1].row_count,
    )
    picked = ranked[:max_tables]
    dropped = len(ranked) - len(picked)

    txns = []
    for db, t in picked:
        txns += _reads(t, db)
        txns += _writes(t, db)

    warnings = []
    if dropped > 0:
        # 조용히 자르지 않는다 — 잘린 것을 밝히지 않으면 "전체를 덮었다"로 읽힌다.
        warnings.append(f"테이블 {dropped}개는 초안에서 제외됐다 (행수 상위 "
                        f"{max_tables}개만 사용). 필요하면 직접 추가할 것")
    no_index = [t["name"] for t in txns
                if t["kind"] == "read" and "NC 인덱스" not in t.get("why", "")
                and "PK" not in t.get("why", "") and "FK" not in t.get("why", "")]
    if no_index:
        warnings.append(f"인덱스 근거 없이 만든 조회 {len(no_index)}개 — 큰 테이블에서 "
                        f"풀스캔이 될 수 있다: {', '.join(no_index[:3])}")

    return {
        "name": name,
        "txns": txns,
        "warnings": warnings,
        "read_count": sum(1 for t in txns if t["kind"] == "read"),
        "write_count": sum(1 for t in txns if t["kind"] == "write"),
        "edited": False,
    }
