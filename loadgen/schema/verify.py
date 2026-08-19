"""시딩 후 두 대상의 데이터가 같은지 대조한다.

쌍 비교(HT on/off 등)는 양쪽이 "같은 일을 같은 양만큼" 해야 성립한다. 데이터가
다르면 인덱스 깊이도 페이지 수도 달라지므로, 남은 차이를 인스턴스 차이로
해석할 수 없다.

시딩은 결정적이라 정상 경로에서는 항상 일치한다 (`seeder._insert_range`가
테이블명·오프셋으로 RNG 시드를 정한다). 그럼에도 대조하는 이유는 한쪽 시딩이
중간에 실패하거나 끊긴 경우를 잡기 위해서다 — 그때 리포트는 아무것도 모른다.
"""
from __future__ import annotations

import logging

from ..config import TargetDB
from ..db import connect

log = logging.getLogger(__name__)


def _stats(target: TargetDB, database: str, tables: list[str]) -> dict[str, dict]:
    """테이블별 {행수, 체크섬}. 조회 실패는 error로 남긴다."""
    out: dict[str, dict] = {}
    with connect(target, database, autocommit=True) as conn:
        cur = conn.cursor()
        for t in tables:
            try:
                # CHECKSUM_AGG는 컬럼 값까지 반영하므로 행수만 보는 것보다 강하다.
                # BINARY_CHECKSUM은 순서 무관이라 삽입 순서가 달라도 일치한다.
                row = cur.execute(
                    f"SELECT COUNT_BIG(*), CHECKSUM_AGG(BINARY_CHECKSUM(*)) "
                    f"FROM dbo.[{t}]").fetchone()
                out[t] = {"rows": int(row[0] or 0), "checksum": row[1]}
            except Exception as exc:  # noqa: BLE001
                out[t] = {"error": str(exc)[:200]}
                log.warning("대조 조회 실패 (%s.%s): %s", database, t, exc)
    return out


def compare_data(a: TargetDB, b: TargetDB, plan: dict) -> dict:
    """시딩 플랜에 있는 테이블에 대해 두 대상을 대조한다.

    체크섬 불일치가 곧 오류는 아니다. `SYSUTCDATETIME()`처럼 실행 시각에 의존하는
    기본값이 있으면 값이 갈린다 — 그래서 행수 불일치와 체크섬 불일치를 나눠
    보고하고, 전자만 확실한 문제로 다룬다.
    """
    by_db: dict[str, list[str]] = {}
    for t in plan.get("tables", []):
        if t.get("rows", 0) > 0:
            by_db.setdefault(t["database"], []).append(t["table"])

    rows_differ, checksum_differ, errors, matched = [], [], [], 0
    for db, tables in by_db.items():
        sa = _stats(a, db, sorted(tables))
        sb = _stats(b, db, sorted(tables))
        for t in sorted(tables):
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
        "note": ("행수가 다르다 — 한쪽 시딩이 완료되지 않았다. 이 상태의 비교는 "
                 "유효하지 않다." if rows_differ else
                 "행수는 같고 체크섬만 다르다 — 실행 시각에 의존하는 기본값"
                 "(SYSUTCDATETIME 등)이 있으면 정상이다. 해당 컬럼이 없다면 "
                 "시딩 로직을 확인할 것." if checksum_differ else
                 "양쪽 데이터가 일치한다."),
    }
