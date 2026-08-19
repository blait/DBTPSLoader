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

from ..schema.ident import qualify, quote
from ..schema.introspect import Column, Table

log = logging.getLogger(__name__)

_DATE_TYPES = ("datetime", "datetime2", "smalldatetime", "date")
_TEXT_TYPES = ("nvarchar", "varchar", "nchar", "char")
_NUM_TYPES = ("int", "bigint", "smallint", "tinyint", "decimal", "numeric",
              "money", "float", "real")

# SELECT 목록에서 제외할 타입. pyodbc가 변환하지 못하거나 페이로드가 과도해서,
# 부하 측정을 드라이버 변환·네트워크 전송 비용 측정으로 바꿔버린다.
# varbinary/binary도 제외한다 — 시딩이 최대 2KB를 넣으므로 매 조회마다 그만큼을
# 전송하게 되고, 서버 측 처리보다 전송이 지배해 인스턴스 비교가 흐려진다.
_UNREADABLE = ("xml", "geography", "geometry", "hierarchyid", "sql_variant",
               "image", "text", "ntext", "timestamp", "rowversion",
               "varbinary", "binary")

# 값을 만들어 넣을 수 없는 타입. INSERT/UPDATE 대상에서 제외한다.
# varbinary는 시딩에서는 넣지만(행 크기 재현), 부하 INSERT에서는 뺀다.
_UNWRITABLE = _UNREADABLE


def _param_for(c: Column, unique: bool = False) -> dict | None:
    """컬럼 하나에 넣을 파라미터 명세. 값을 만들 수 없으면 None.

    **컬럼 제약을 반드시 반영해야 한다.** 실제 RDS에서 세 가지로 실패했다:
    NOT NULL 컬럼에 NULL(`const: None`)을 넣어 23000, `decimal(5,4)`에 최대
    10000을 넣어 22003, 유니크 컬럼에 중복 가능한 값을 넣어 23000.

    `unique`면 매 실행마다 다른 값이 나오는 생성기를 쓴다 — 유니크 인덱스가
    걸린 컬럼에 INSERT하면서 중복을 내면 부하가 에러만 쌓는다.
    """
    t = c.type
    if t in _UNWRITABLE:
        return None
    if t in _DATE_TYPES or t == "date":
        return {"gen": "datetime"}
    if t == "datetimeoffset":
        # 부하 파라미터에는 tz-aware 생성기가 없다. 이 컬럼이 NOT NULL이면
        # 트랜잭션을 만들지 않는 편이 낫다.
        return None if not c.nullable else {"gen": "const", "value": None}
    if t == "time":
        return None if not c.nullable else {"gen": "const", "value": None}
    if t == "bit":
        return {"gen": "bit"}
    if t == "uniqueidentifier":
        return {"gen": "uuid"}
    if t in _TEXT_TYPES:
        # max_length는 바이트. n계열은 2바이트/문자이고 -1은 MAX.
        nchars = 40 if c.max_length in (-1, None) else (
            c.max_length // 2 if t.startswith("n") else c.max_length)
        nchars = max(1, min(nchars, 200))
        low = c.name.lower()
        if unique:
            # 매 실행마다 달라야 한다. token은 길이를 지정할 수 있어 안전하다.
            return {"gen": "token", "len": min(nchars, 24)}
        if "email" in low and nchars >= 24:
            return {"gen": "email"}
        if any(k in low for k in ("name", "title")) and nchars >= 12:
            return {"gen": "name"}
        return {"gen": "text", "bytes": nchars * 2}
    if t in ("decimal", "numeric", "money", "smallmoney"):
        # precision/scale을 지켜야 한다. decimal(5,4)의 최대는 9.9999다.
        prec, scale = c.precision or 18, min(c.scale or 0, c.precision or 18)
        hi = min(10 ** max(prec - scale, 1) * 0.9, 1_000_000.0)
        return {"gen": "decimal", "min": 0, "max": hi, "q": scale}
    if t in ("float", "real"):
        return {"gen": "decimal", "min": 0, "max": 10000, "q": 4}
    if t in ("int", "bigint"):
        return {"gen": "int", "min": 1, "max": 10 ** 6 if t == "int" else 10 ** 9}
    if t == "smallint":
        return {"gen": "int", "min": 1, "max": 30000}
    if t == "tinyint":
        return {"gen": "int", "min": 0, "max": 255}
    return None


def _fk_param(fk_col: str, t: Table) -> dict | None:
    """FK 컬럼이면 부모 테이블 범위에서 뽑는 명세. 아니면 None.

    `of`는 "schema.table"이다. 테이블명만 쓰면 스키마가 다른 동명 테이블이
    충돌해 잘못된 범위에서 id를 뽑는다.
    """
    for fk in t.foreign_keys:
        if fk_col in fk.columns and len(fk.columns) == 1:
            return {"gen": "skewed_id", "of": fk.ref_table}
    return None


def _reads(t: Table, db: str) -> list[dict]:
    out = []
    fq = qualify(t.schema, t.name)
    ref = t.qualified          # "schema.table" — ranges/ctx 조회에 쓴다
    pk = t.numeric_single_pk
    if pk:
        # SELECT * 를 쓰지 않는다 — xml·geography 같은 타입이 섞이면 드라이버가
        # 변환에 실패할 수 있고, 컬럼을 명시하면 무엇을 읽는지 사용자가 안다.
        cols = [c.name for c in t.columns if c.type not in _UNREADABLE][:6]
        if cols:
            out.append({
                "name": f"{t.name.lower()}_by_pk", "kind": "read", "weight": 30,
                "database": db, "tables": [ref],
                "sql": [f"SELECT {', '.join(quote(c) for c in cols)} "
                        f"FROM {fq} WHERE {quote(pk)} = ?"],
                "params": [[{"gen": "skewed_id", "of": ref}]],
                "why": f"PK {quote(pk)} 단건 조회 — 인덱스 seek",
            })

    # FK 조인. 부모의 표시용 컬럼을 함께 읽는다.
    for fk in t.foreign_keys[:2]:
        if len(fk.columns) != 1:
            # 복합 FK는 초안에서 만들지 않는다. 값 조합을 맞추려면 부모의 실제
            # 행을 읽어야 하는데, 그건 초안 휴리스틱으로 할 일이 아니다.
            continue
        parent = fk.ref_name
        p_fq = qualify(fk.ref_schema, parent)
        child_cols = [c.name for c in t.columns if c.type not in _UNREADABLE][:4]
        if not child_cols:
            continue
        out.append({
            "name": f"{t.name.lower()}_join_{parent.lower()}", "kind": "read", "weight": 15,
            "database": db, "tables": [ref, fk.ref_table],
            "sql": [f"SELECT TOP 20 {', '.join('c.' + quote(c) for c in child_cols)}, "
                    f"p.{quote(fk.ref_columns[0])} "
                    f"FROM {fq} c "
                    f"JOIN {p_fq} p ON p.{quote(fk.ref_columns[0])} = c.{quote(fk.columns[0])} "
                    f"WHERE c.{quote(fk.columns[0])} = ?"],
            "params": [[{"gen": "skewed_id", "of": fk.ref_table}]],
            "why": f"FK {fk.name} 조인 — {quote(fk.columns[0])} → {parent}",
        })

    # NC 인덱스 키로 범위 조회. 인덱스를 실제로 타는 쿼리를 만드는 것이 목적이다.
    for ix in t.indexes:
        if ix.primary_key or not ix.columns:
            continue
        if ix.filtered:
            # 필터 인덱스를 평범한 seek 키로 쓰면 임의 파라미터가 필터 조건을
            # 벗어나 조용히 스캔이 된다. 필터 조건을 재현할 방법이 없으므로 건너뛴다.
            continue
        key = ix.columns[0]
        col = next((c for c in t.columns if c.name == key), None)
        if col is None:
            continue
        param = _fk_param(key, t) or _param_for(col)
        cols = [c.name for c in t.columns if c.type not in _UNREADABLE][:6]
        if not cols:
            continue
        order = f" ORDER BY {quote(pk)} DESC" if pk else ""
        out.append({
            "name": f"{t.name.lower()}_by_{key.lower()}", "kind": "read", "weight": 15,
            "database": db, "tables": [ref],
            "sql": [f"SELECT TOP 50 {', '.join(quote(c) for c in cols)} "
                    f"FROM {fq} WHERE {quote(key)} = ?{order}"],
            "params": [[param]],
            "why": f"NC 인덱스 {ix.name} 키 {quote(key)} 범위 조회",
        })
        break  # 테이블당 인덱스 조회 1개면 충분하다
    return out


def _writes(t: Table, db: str) -> list[dict]:
    out = []
    insertable = [c for c in t.columns if c.insertable]
    has_date = any(c.type in _DATE_TYPES for c in t.columns)

    fq = qualify(t.schema, t.name)
    ref = t.qualified

    # CHECK 제약이나 트리거가 있으면 합성값이 거부되거나 부수 효과가 생긴다.
    # 초안에서는 쓰기를 만들지 않고, 사용자가 판단해 직접 추가하게 한다.
    if t.has_check or t.has_trigger:
        reason = " / ".join(x for x in (
            "CHECK 제약" if t.has_check else None,
            "트리거" if t.has_trigger else None) if x)
        log.info("%s: %s 때문에 쓰기 초안을 만들지 않았다", ref, reason)
        return out

    writable = [c for c in insertable if c.type not in _UNWRITABLE]
    # 유니크 인덱스가 걸린 컬럼은 매 실행마다 다른 값이 나와야 한다.
    unique_cols = {ix.columns[0] for ix in t.indexes
                   if ix.unique and len(ix.columns) == 1}

    # append INSERT — IDENTITY PK + 날짜 컬럼이면 로그성으로 본다.
    if writable and t.identity_pk and has_date:
        # 값을 만들 수 없는 컬럼(NOT NULL time/datetimeoffset 등)이 있으면
        # nullable인 것만 빼고, NOT NULL이면 트랜잭션을 만들지 않는다.
        pairs, blocked = [], []
        for c in writable:
            spec = _fk_param(c.name, t) or _param_for(c, unique=c.name in unique_cols)
            if spec is None:
                if c.nullable:
                    continue          # 컬럼 목록에서 제외 — DB가 NULL을 넣는다
                blocked.append(f"{c.name}({c.type})")
                continue
            pairs.append((c, spec))
        if blocked:
            log.info("%s: %s 때문에 INSERT 초안을 만들지 않았다", ref, ", ".join(blocked))
            pairs = []
        cols_used = [c for c, _ in pairs]
        params = [spec for _, spec in pairs]
        writable = cols_used
    else:
        writable = []
    if writable:
        cols = ", ".join(quote(c.name) for c in writable)
        ph = ", ".join("?" for _ in writable)
        # FK 부모까지 tables에 넣어야 id 범위가 조회된다 — 없으면 파라미터가
        # 없는 부모 id를 뽑아 error 547이 된다.
        refs = [ref] + sorted({fk.ref_table for fk in t.foreign_keys
                               if len(fk.columns) == 1})
        out.append({
            "name": f"{t.name.lower()}_insert", "kind": "write", "weight": 40,
            "database": db, "tables": refs,
            "sql": [f"INSERT INTO {fq} ({cols}) VALUES ({ph})"],
            "params": [params],
            "why": f"IDENTITY PK + 날짜 컬럼 → append-only 삽입 ({len(writable)}개 컬럼)",
        })

    # UPDATE ... WHERE PK — 대상은 반드시 균등 분포.
    pk = t.numeric_single_pk
    if pk:
        # 갱신 대상은 FK도 아니고 유니크 제약도 없는 평범한 컬럼이어야 한다.
        # 유니크 컬럼에 임의값을 넣으면 중복 키 위반이 난다.
        unique_cols = {c for ix in t.indexes if ix.unique and not ix.primary_key
                       for c in ix.columns}
        # UPDATE 대상은 INSERT에서 제외된 컬럼도 후보다 (원본 컬럼 목록을 쓴다).
        cand = [c for c in insertable if c.type not in _UNWRITABLE]
        target = next((c for c in cand
                       if not _fk_param(c.name, t) and c.name not in unique_cols
                       and c.name != pk
                       and (c.type in _TEXT_TYPES or c.type in _NUM_TYPES)
                       and _param_for(c) is not None), None)
        if target:
            out.append({
                "name": f"{t.name.lower()}_update", "kind": "write", "weight": 10,
                "database": db, "tables": [ref],
                "sql": [f"UPDATE {fq} SET {quote(target.name)} = ? "
                        f"WHERE {quote(pk)} = ?"],
                "params": [[_param_for(target), {"gen": "uniform_id", "of": ref}]],
                "why": f"{quote(target.name)} 갱신. 대상 id는 균등 분포 — 편중을 쓰면 "
                       f"모든 커넥션이 같은 행을 잠그려 들어 직렬화된다",
            })
    return out


def draft_workload(tables_by_db: dict[str, dict[str, Table]],
                   name: str = "draft",
                   max_tables: int = 12,
                   plan: dict | None = None) -> dict:
    """스키마 → 트랜잭션 믹스 초안.

    큰 테이블을 우선한다 — 부하가 실제 데이터 분포를 닮게 하려면 큰 테이블을
    건드려야 한다. `max_tables`로 제한하는 이유는 200 테이블짜리 스키마에서
    트랜잭션 수백 개를 만들면 사용자가 검토할 수 없기 때문이다.

    **`plan`을 넘기는 것이 정상 경로다.** 이 도구는 빈 DB를 전제로 하므로 조회
    시점의 `row_count`는 전부 0이고, 그러면 정렬이 무의미해져 알파벳 순 앞쪽
    테이블만 뽑힌다. 시딩 플랜의 행수를 쓰면 실제로 커질 테이블을 우선한다.
    """
    plan_rows: dict[str, int] = {}
    if plan:
        for e in plan.get("tables", []):
            plan_rows[e.get("qualified") or f"{e.get('schema','dbo')}.{e['table']}"] = \
                e.get("rows", 0)

    def size_of(t: Table) -> int:
        return plan_rows.get(t.qualified, t.row_count)

    candidates = [(db, t) for db, tables in tables_by_db.items()
                  for t in tables.values()
                  # 플랜이 0행으로 둔 테이블은 조회해도 빈 결과다
                  if not plan_rows or size_of(t) > 0]
    ranked = sorted(candidates, key=lambda x: -size_of(x[1]))
    picked = ranked[:max_tables]
    dropped = len(ranked) - len(picked)

    txns = []
    for db, t in picked:
        txns += _reads(t, db)
        txns += _writes(t, db)

    warnings = []
    if dropped > 0:
        # 조용히 자르지 않는다 — 잘린 것을 밝히지 않으면 "전체를 덮었다"로 읽힌다.
        basis = "플랜 행수" if plan_rows else "현재 행수"
        warnings.append(f"테이블 {dropped}개는 초안에서 제외됐다 ({basis} 상위 "
                        f"{max_tables}개만 사용). 필요하면 직접 추가할 것")
    if not plan_rows:
        # 빈 DB에서는 row_count가 전부 0이라 정렬이 무의미하다 — 알파벳 순으로
        # 뽑히는 것을 사용자가 알아야 한다.
        warnings.append("시딩 플랜 없이 생성했다 — 대상 DB가 비어 있으면 테이블 "
                        "선택이 사실상 이름 순이 된다. 플랜을 먼저 저장하고 다시 "
                        "생성하는 것을 권한다")
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
