"""컬럼별 값 생성 — 시딩 플랜의 행 팩토리를 조립한다.

원본에는 `_generic_value()`가 있었지만 "핫 테이블이 아닌 곳에 10행 채우기"용
보조 장치였고, nullable 컬럼을 타입도 보지 않고 즉시 NULL로 만들었다. 주 엔진이
되는 지금은 그럴 수 없다 — nullable이 전부 NULL이면 인덱스를 타지 않고 행 크기도
비현실적이 되어, 페이지 밀도와 I/O가 실제와 달라진다.

그래서 여기서는 일정 비율만 NULL로 두고 나머지는 타입에 맞는 값을 만든다.
"""
from __future__ import annotations

import uuid

from ..seed.datagen import Gen
from .introspect import Column, Table

_DATE_TYPES = ("datetime", "datetime2", "smalldatetime", "date")
_TEXT_TYPES = ("nvarchar", "varchar", "nchar", "char", "sysname")

# nullable 컬럼이 NULL이 될 확률. 0이면 실제 데이터와 다르고, 100이면 인덱스가
# 무의미해진다. 실제 스키마에서 nullable은 "때때로 빈다"는 뜻이므로 소수로 둔다.
NULL_PCT = 12


def _text_chars(c: Column) -> int:
    """max_length(바이트)를 문자 수로. n계열은 2바이트/문자, -1은 MAX."""
    if c.max_length in (-1, None):
        return 40
    n = c.max_length // 2 if c.type.startswith("n") else c.max_length
    return max(1, min(n, 400))


def value_for(c: Column, g: Gen, i: int, total: int, ctx: dict,
              fk_parent: str | None = None) -> object:
    """컬럼 하나의 값.

    `i`/`total`은 행 순번 — 날짜를 단조 증가시켜 append-only 테이블의 시간 분포를
    재현하는 데 쓴다. `fk_parent`가 있으면 그 부모의 범위에서 뽑는다.
    """
    if fk_parent:
        max_id = ctx.get(fk_parent.lower(), 0)
        if max_id < 1:
            return None if c.nullable else 1
        # 시딩 단계의 FK는 편중시킨다. 읽기가 부모를 조인할 때 핫셋이 생겨야
        # 버퍼 캐시 거동이 실제와 닮는다.
        return g.skewed_id(max_id)

    if c.nullable and g.bit(NULL_PCT):
        return None

    t = c.type
    if t in _DATE_TYPES:
        return g.dt_seq(i, total)
    if t == "bit":
        return g.bit(30)
    if t == "uniqueidentifier":
        return str(uuid.UUID(int=g.rng.getrandbits(128)))
    if t in ("varbinary", "binary", "image"):
        return b"\x00"
    if t == "time":
        return "12:00:00"
    if t in _TEXT_TYPES:
        n = _text_chars(c)
        low = c.name.lower()
        if "email" in low:
            return g.email(i)[:n]
        if any(k in low for k in ("name", "title")):
            return g.name()[:n]
        # 2바이트 = 1문자이므로 바이트 단위로 환산해 넘긴다
        return g.text(n * 2)[:n]
    if t in ("decimal", "numeric", "money"):
        return g.dec(0, 10000, min(c.scale or 2, 4))
    if t in ("float", "real"):
        return g.dec(0, 10000, 4)
    if t in ("int", "bigint"):
        return g.i(1, 10**6)
    if t == "smallint":
        return g.i(1, 30000)
    if t == "tinyint":
        return g.i(0, 255)
    return None


def make_factory(t: Table, columns: list[str]):
    """플랜의 컬럼 목록 → 행 팩토리 (g, i, total, ctx) -> tuple.

    FK 컬럼은 부모 이름을 미리 묶어두어 매 행마다 다시 찾지 않게 한다.
    """
    by_name = {c.name: c for c in t.columns}
    fk_of: dict[str, str] = {}
    for fk in t.foreign_keys:
        if len(fk.columns) == 1:
            fk_of[fk.columns[0]] = fk.ref_table.split(".")[-1]

    cols = [by_name[n] for n in columns if n in by_name]

    def factory(g: Gen, i: int, total: int, ctx: dict) -> tuple:
        return tuple(
            value_for(c, g, i, total, ctx, fk_parent=fk_of.get(c.name))
            for c in cols
        )
    return factory


def attach_factories(plan: dict, tables_by_db: dict[str, dict[str, Table]]) -> dict:
    """플랜 각 항목에 `factory`를 붙인다. 시딩 직전에 호출한다.

    팩토리는 JSON에 담을 수 없으므로 플랜 파일에는 없다 — 저장은 선언적 형태로
    하고, 실행 시점에 스키마를 다시 조회해 조립한다.
    """
    out = dict(plan)
    entries = []
    for e in plan.get("tables", []):
        tables = tables_by_db.get(e["database"], {})
        t = tables.get(e.get("qualified")) or tables.get(f"{e.get('schema', 'dbo')}.{e['table']}")
        if t is None:
            # 스키마가 바뀌어 테이블이 사라졌다. 조용히 넘기면 행수가 조용히
            # 줄어드니 0행으로 두고 이유를 남긴다.
            e = {**e, "rows": 0,
                 "warnings": list(e.get("warnings", [])) + ["대상 DB에 이 테이블이 없다"]}
            entries.append(e)
            continue
        entries.append({**e, "factory": make_factory(t, e["columns"])})
    out["tables"] = entries
    return out
