"""컬럼별 값 생성 — 시딩 플랜의 행 팩토리를 조립한다.

이 계층에서 잘못된 값을 만들면 두 가지로 나타난다. 하나는 삽입 실패(배치 전체가
죽는다), 다른 하나는 **조용히 잘못된 데이터**다. 후자가 더 나쁘다 — 부하는 정상적으로
돌고 숫자도 그럴듯하지만 측정 대상이 실제와 다르다.

그래서 여기서는 세 가지를 지킨다.

**1. 선언된 범위를 넘지 않는다.** `decimal(5,4)`의 최대는 9.9999인데 0~10000을
생성하면 거의 모든 행이 산술 오버플로로 죽는다. precision과 scale을 모두 본다.

**2. 값을 만들 수 없는 타입은 만들지 않는다.** 미처리 타입에 NULL을 돌려주면
NOT NULL 컬럼에서 배치 전체가 실패한다. 생성 가능 여부를 미리 판정해 플랜 단계에서
제외한다.

**3. 유일해야 하는 컬럼은 유일하게.** 값 공간이 작으면 중복이 나오고, 삽입 중에는
제약이 꺼져 있어 그대로 들어간다. 그러면 "유니크" 인덱스에 중복이 남아 선택도가
실제와 달라진다. 유니크 컬럼에는 행 순번을 섞는다.
"""
from __future__ import annotations

import os
import uuid
from datetime import timedelta

from ..seed.datagen import EPOCH_START, Gen
from .introspect import Column, Table

_DATE_TYPES = ("datetime", "datetime2", "smalldatetime")
_TEXT_TYPES = ("nvarchar", "varchar", "nchar", "char", "sysname")
_BIN_TYPES = ("varbinary", "binary")

# 값을 생성할 수 있는 타입. 여기 없는 타입은 시딩 대상에서 제외한다 —
# NOT NULL 컬럼에 NULL을 넣어 배치를 죽이는 것보다 낫다.
GENERATABLE = frozenset(
    _DATE_TYPES + _TEXT_TYPES + _BIN_TYPES + (
        "date", "time", "datetimeoffset",
        "bit", "uniqueidentifier",
        "int", "bigint", "smallint", "tinyint",
        "decimal", "numeric", "money", "smallmoney", "float", "real",
        "xml", "hierarchyid",
    )
)

# 정수 타입별 최대값. PK 순번이 이걸 넘으면 삽입이 죽는다.
INT_MAX = {"tinyint": 255, "smallint": 32_767, "int": 2_147_483_647}

# nullable 컬럼이 NULL이 될 확률. 0이면 실제 데이터와 다르고, 100이면 인덱스가
# 무의미해진다. 실제 스키마에서 nullable은 "때때로 빈다"는 뜻이므로 소수로 둔다.
NULL_PCT = 12

# varbinary(MAX) 등 길이 무제한 이진 컬럼에 넣을 바이트 수. 1바이트만 넣으면
# 행 크기가 실제와 자릿수 단위로 달라져 페이지 밀도와 I/O가 전부 어긋난다.
_MAX_BLOB_BYTES = 2048


def can_generate(c: Column) -> bool:
    """이 컬럼에 값을 만들 수 있는가. 없으면 시딩에서 제외해야 한다."""
    return c.type in GENERATABLE


def _text_chars(c: Column) -> int:
    """max_length(바이트)를 문자 수로. n계열은 2바이트/문자, -1은 MAX."""
    if c.max_length in (-1, None):
        return 40
    # sysname은 nvarchar(128)인데 max_length가 256으로 보고된다 — n계열 규칙이
    # 그대로 적용되므로 별도 처리는 필요 없다.
    n = c.max_length // 2 if c.type.startswith("n") else c.max_length
    return max(1, min(n, 400))


# decimal 생성값의 현실적 상한. precision이 허용하더라도 이 이상은 만들지 않는다.
#
# 이유가 두 가지다. (1) `decimal(18,2)`의 정수부는 16자리라 1조를 넘는 값이
# 나오는데, 금액·수량 컬럼에 그런 값이 들어가면 데이터가 비현실적이 된다.
# (2) 큰 정수부를 float로 다루면 pyodbc 변환에서 "Numeric value out of range"가
# 나 배치 전체가 죽는다 — 실제 RDS에서 `decimal(12,2)`가 이 이유로 실패했다.
_DECIMAL_SOFT_MAX = 1_000_000.0


def _decimal_value(c: Column, g: Gen) -> float:
    """선언된 precision/scale 안에 들어가면서 현실적인 값.

    `decimal(p, s)`의 절대 최대는 10**(p-s) - 10**-s 다. precision을 무시하고
    0~10000을 생성하면 `decimal(5,4)`(최대 9.9999)에서 사실상 모든 행이
    "Arithmetic overflow"로 죽는다.

    반대로 precision만 보고 최대까지 채우면 `decimal(18,2)`가 1조를 넘는 값을
    만든다. 두 제약을 모두 지켜야 한다.
    """
    if c.type in ("money", "smallmoney"):
        # money 범위는 ±922조지만 위 이유로 낮춰 잡는다.
        # smallmoney는 ±214,748.3647이므로 그보다 작게.
        hi = _DECIMAL_SOFT_MAX if c.type == "money" else 200_000.0
        return g.dec(0, hi, 4)
    prec = c.precision or 18
    scale = min(c.scale or 0, prec)
    int_digits = max(prec - scale, 1)
    # 상한을 조금 낮춰 잡는다 — 반올림으로 자릿수가 하나 올라가면 오버플로다.
    hi = min(10 ** int_digits * 0.9, _DECIMAL_SOFT_MAX)
    return g.dec(0, hi, scale)


def _unique_text(base: str, i: int, n: int) -> str:
    """유니크 컬럼용 문자열 — 행 순번을 붙여 충돌을 없앤다.

    `g.name()`은 조합이 1,225개뿐이라 1만 행이면 99%가 중복이다. 삽입 중에는
    제약이 꺼져 있어 중복이 그대로 들어가고, 인덱스 선택도가 실제와 달라진다.
    """
    suffix = str(i)
    if len(suffix) >= n:
        return suffix[-n:]
    return (base[: n - len(suffix) - 1] + "-" + suffix) if n > len(suffix) + 1 \
        else (base[: n - len(suffix)] + suffix)


def value_for(c: Column, g: Gen, i: int, total: int, ctx: dict,
              fk_parent: str | None = None, unique: bool = False,
              sequential: bool = False) -> object:
    """컬럼 하나의 값.

    `i`/`total`은 행 순번 — 날짜를 단조 증가시켜 append-only 테이블의 시간 분포를
    재현하고, 유니크 컬럼의 충돌을 피하는 데 쓴다.
    `fk_parent`가 있으면 그 부모의 범위에서 뽑는다.
    `unique`면 값에 순번을 섞는다. `sequential`이면 순번 자체를 쓴다 (PK).
    """
    if fk_parent:
        max_id = ctx.get(fk_parent.lower(), 0)
        if max_id < 1:
            # 부모 범위를 모른다. nullable이면 NULL, 아니면 1 — 어느 쪽이든
            # 플랜 단계에서 걸러졌어야 하는 상황이다.
            return None if c.nullable else 1
        # 시딩 단계의 FK는 편중시킨다. 읽기가 부모를 조인할 때 핫셋이 생겨야
        # 버퍼 캐시 거동이 실제와 닮는다.
        return g.skewed_id(max_id)

    t = c.type

    # PK·유니크 정수 컬럼은 순번을 쓴다. 난수를 쓰면 100만 행에서 37%가 충돌한다.
    if sequential and t in ("int", "bigint", "smallint", "tinyint", "decimal", "numeric"):
        # 타입 범위를 넘으면 "Numeric value out of range"로 배치가 죽는다.
        # tinyint(0~255) 컬럼에 300행을 요청하는 일이 실제로 있다 — 실측으로
        # 발견했다. 되접어서 범위 안에 남기되, 그러면 유일성이 깨지므로
        # 플랜 단계에서 행수를 제한하는 것이 정답이다 (plan.py의 max_rows).
        cap = INT_MAX.get(t)
        return (i - 1) % cap + 1 if cap else i

    if c.nullable and not unique and g.bit(NULL_PCT):
        return None

    if t in _DATE_TYPES:
        return g.dt_seq(i, total)
    if t == "date":
        return g.dt_seq(i, total).date()
    if t == "time":
        # 상수를 쓰면 이 컬럼의 인덱스는 선택도가 0이 된다.
        return (EPOCH_START + timedelta(seconds=g.i(0, 86399))).time()
    if t == "datetimeoffset":
        # tz-aware가 아니면 드라이버가 거부할 수 있다. UTC로 고정한다.
        from datetime import timezone
        return g.dt_seq(i, total).replace(tzinfo=timezone.utc)
    if t == "bit":
        return g.bit(30)
    if t == "uniqueidentifier":
        return str(uuid.UUID(int=g.rng.getrandbits(128)))
    if t == "xml":
        return f"<r><i>{i}</i><v>{g.token(8)}</v></r>"
    if t == "hierarchyid":
        # 문자열 표현을 넣으면 SQL Server가 변환한다.
        return f"/{i}/"
    if t in _BIN_TYPES:
        n = _MAX_BLOB_BYTES if c.max_length in (-1, None) else min(c.max_length, _MAX_BLOB_BYTES)
        return os.urandom(max(1, n))
    if t in _TEXT_TYPES:
        n = _text_chars(c)
        low = c.name.lower()
        if "email" in low:
            base = g.email(i)
        elif any(k in low for k in ("name", "title")):
            base = g.name()
        else:
            base = g.text(n * 2)      # 2바이트 = 1문자
        return _unique_text(base, i, n) if unique else base[:n]
    if t in ("decimal", "numeric", "money", "smallmoney"):
        return _decimal_value(c, g)
    if t in ("float", "real"):
        return g.dec(0, 10000, 4)
    if t == "int":
        return i if unique else g.i(1, 10**6)
    if t == "bigint":
        return i if unique else g.i(1, 10**9)
    if t == "smallint":
        return i % 32767 + 1 if unique else g.i(1, 30000)
    if t == "tinyint":
        return i % 256 if unique else g.i(0, 255)

    # 여기 오면 GENERATABLE과 어긋난 것이다. NULL을 돌려주면 NOT NULL 컬럼에서
    # 배치 전체가 죽으므로, 호출부가 알아채도록 예외를 낸다.
    raise ValueError(f"{c.name}: 값을 생성할 수 없는 타입 {t}")


def make_factory(t: Table, columns: list[str]):
    """플랜의 컬럼 목록 → 행 팩토리 (g, i, total, ctx) -> tuple.

    컬럼별 역할(FK / 유니크 / 순번)을 미리 계산해 매 행마다 다시 찾지 않게 한다.
    """
    by_name = {c.name: c for c in t.columns}

    # 단일 컬럼 FK만 부모 범위에서 뽑는다. 복합 FK는 값 조합을 맞출 수 없으므로
    # 플랜 단계에서 해당 테이블을 제외한다 (plan.py 참조).
    fk_of: dict[str, str] = {}
    for fk in t.foreign_keys:
        if len(fk.columns) == 1:
            # ctx 키는 부모의 "schema.table"이다 — 스키마가 다른 동명 테이블을
            # 구분하지 못하면 잘못된 범위에서 id를 뽑는다.
            fk_of[fk.columns[0]] = fk.ref_table

    # 유니크 제약이 걸린 단일 컬럼. 여기에 중복을 넣으면 제약을 다시 켤 때
    # 실패하거나(NOCHECK면) 중복이 남아 선택도가 왜곡된다.
    unique_cols = {ix.columns[0] for ix in t.indexes
                   if ix.unique and len(ix.columns) == 1}

    # PK가 IDENTITY가 아니면 우리가 값을 넣어야 하고 유일해야 한다.
    #
    # **복합 PK도 포함한다.** 단일 PK만 처리하면 복합 PK의 각 컬럼이 난수가 되어
    # 조합이 충돌한다 — 실제 RDS에서 `PK_OrderLine` 위반으로 배치가 죽었다.
    # 복합 PK는 마지막 컬럼만 순번으로 두고 앞 컬럼은 FK/난수를 유지해야
    # 부모 참조가 성립하는데, 그러면 조합의 유일성이 깨진다. 그래서 마지막
    # 컬럼에 행 순번을 쓴다 — 앞 컬럼이 같아도 뒤가 다르므로 조합은 유일하다.
    pk_cols = set(t.primary_key)
    if len(t.primary_key) > 1:
        # 앞 컬럼은 FK일 수 있으므로 건드리지 않고, 마지막만 순번으로.
        seq_pk = {t.primary_key[-1]}
    else:
        seq_pk = pk_cols

    seq_cols = {c for c in (seq_pk | unique_cols)
                if (col := by_name.get(c)) and not col.identity
                and col.type in ("int", "bigint", "smallint", "tinyint",
                                 "decimal", "numeric")}

    cols = [by_name[n] for n in columns if n in by_name]

    def factory(g: Gen, i: int, total: int, ctx: dict) -> tuple:
        return tuple(
            value_for(c, g, i, total, ctx,
                      fk_parent=fk_of.get(c.name),
                      unique=c.name in unique_cols or c.name in seq_pk,
                      sequential=c.name in seq_cols)
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
            entries.append({**e, "rows": 0,
                            "warnings": list(e.get("warnings", []))
                            + ["대상 DB에 이 테이블이 없다"]})
            continue
        entries.append({**e, "factory": make_factory(t, e["columns"])})
    out["tables"] = entries
    return out
