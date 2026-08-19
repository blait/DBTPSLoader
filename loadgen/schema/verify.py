"""시딩 후 두 대상의 데이터가 같은지 대조한다.

쌍 비교(HT on/off 등)는 양쪽이 "같은 일을 같은 양만큼" 해야 성립한다. 데이터가
다르면 인덱스 깊이도 페이지 수도 달라지므로, 남은 차이를 인스턴스 차이로
해석할 수 없다.

시딩은 결정적이라 정상 경로에서는 항상 일치한다 (`seeder._insert_range`가
테이블명·오프셋으로 RNG 시드를 정한다). 그럼에도 대조하는 이유는 한쪽 시딩이
중간에 실패하거나 끊긴 경우를 잡기 위해서다 — 그때 리포트는 아무것도 모른다.

**한계를 알고 써야 한다.** `BINARY_CHECKSUM`은 `xml`·`text`·`ntext`·`image`와
비교 불가 CLR 타입을 **조용히 무시한다**. 페이로드가 대부분 그런 타입인 테이블은
내용이 전혀 달라도 "일치"로 나온다. 또 `CHECKSUM_AGG`는 순서 무관이고 NULL을
무시하므로 행 집합이 바뀐 것도 잡지 못한다. 그래서 무시된 컬럼을 함께 보고한다.
"""
from __future__ import annotations

import logging

from ..config import TargetDB
from ..db import connect
from .ident import qualify

log = logging.getLogger(__name__)

# BINARY_CHECKSUM이 무시하는 타입 (MS 문서). 이 타입이 대부분인 테이블의
# 체크섬 일치는 내용 일치를 뜻하지 않는다.
_CHECKSUM_BLIND = ("xml", "text", "ntext", "image", "sql_variant",
                   "geography", "geometry", "hierarchyid")


def _stats(target: TargetDB, database: str,
           tables: list[tuple[str, str]]) -> dict[str, dict]:
    """테이블별 {행수, 체크섬}. `tables`는 (schema, name) 목록."""
    out: dict[str, dict] = {}
    with connect(target, database, autocommit=True) as conn:
        cur = conn.cursor()
        for sch, name in tables:
            key = f"{sch}.{name}"
            try:
                # CHECKSUM_AGG는 컬럼 값까지 반영하므로 행수만 보는 것보다 강하다.
                # BINARY_CHECKSUM은 순서 무관이라 삽입 순서가 달라도 일치한다.
                row = cur.execute(
                    f"SELECT COUNT_BIG(*), CHECKSUM_AGG(BINARY_CHECKSUM(*)) "
                    f"FROM {qualify(sch, name)}").fetchone()
                out[key] = {"rows": int(row[0] or 0), "checksum": row[1]}
            except Exception as exc:  # noqa: BLE001
                out[key] = {"error": str(exc)[:200]}
                log.warning("대조 조회 실패 (%s.%s): %s", database, key, exc)
    return out


def _blind_columns(plan: dict) -> dict[str, list[str]]:
    """테이블별로 체크섬이 무시하는 컬럼 목록. 플랜의 컬럼 타입 정보를 쓴다."""
    out: dict[str, list[str]] = {}
    for t in plan.get("tables", []):
        key = t.get("qualified") or f"{t.get('schema','dbo')}.{t['table']}"
        blind = [c["name"] for c in t.get("column_types", [])
                 if c.get("type") in _CHECKSUM_BLIND]
        if blind:
            out[key] = blind
    return out


def compare_data(a: TargetDB, b: TargetDB, plan: dict) -> dict:
    """시딩 플랜에 있는 테이블에 대해 두 대상을 대조한다.

    체크섬 불일치가 곧 오류는 아니다. `SYSUTCDATETIME()`처럼 실행 시각에 의존하는
    기본값이 있으면 값이 갈린다 — 그래서 행수 불일치와 체크섬 불일치를 나눠
    보고하고, 전자만 확실한 문제로 다룬다.
    """
    by_db: dict[str, list[tuple[str, str]]] = {}
    for t in plan.get("tables", []):
        if t.get("rows", 0) > 0:
            by_db.setdefault(t["database"], []).append(
                (t.get("schema", "dbo"), t["table"]))

    blind = _blind_columns(plan)
    rows_differ, checksum_differ, errors, matched = [], [], [], 0
    for db, tables in by_db.items():
        sa = _stats(a, db, sorted(tables))
        sb = _stats(b, db, sorted(tables))
        for sch, name in sorted(tables):
            t = f"{sch}.{name}"
            ra, rb = sa.get(t, {}), sb.get(t, {})
            if "error" in ra or "error" in rb:
                errors.append({"database": db, "table": t,
                               a.label: ra.get("error"), b.label: rb.get("error")})
                continue
            if ra["rows"] != rb["rows"]:
                rows_differ.append({"database": db, "table": t,
                                    a.label: ra["rows"], b.label: rb["rows"]})
            elif ra["checksum"] != rb["checksum"]:
                checksum_differ.append({"database": db, "table": t,
                                        "rows": ra["rows"],
                                        a.label: ra["checksum"], b.label: rb["checksum"]})
            else:
                matched += 1

    return {
        "same": not (rows_differ or checksum_differ or errors),
        "rows_same": not rows_differ,
        "a_label": a.label, "b_label": b.label,
        "matched": matched,
        "rows_differ": rows_differ,
        "checksum_differ": checksum_differ,
        "errors": errors,
        # 체크섬이 무시한 컬럼. 이 목록이 비어 있지 않으면 "일치"가 내용 일치를
        # 보장하지 않는다.
        "checksum_blind": blind,
        "note": ("행수가 다르다 — 한쪽 시딩이 완료되지 않았다. 이 상태의 비교는 "
                 "유효하지 않다." if rows_differ else
                 "행수는 같고 체크섬만 다르다 — 실행 시각에 의존하는 기본값"
                 "(SYSUTCDATETIME 등)이 있으면 정상이다. 해당 컬럼이 없다면 "
                 "시딩 로직을 확인할 것." if checksum_differ else
                 ("양쪽 데이터가 일치한다. 단 일부 컬럼은 체크섬이 무시하는 "
                  "타입이라 내용 일치가 보장되지 않는다 (checksum_blind 참조)."
                  if blind else "양쪽 데이터가 일치한다.")),
    }
