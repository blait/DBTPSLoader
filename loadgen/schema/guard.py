"""전제조건 검사 — 빈 DB인지, 두 대상의 스키마가 같은지.

이 도구는 쓰기 부하를 거는 도구다. 남의 운용 DB에 붙이는 사고를 구조적으로
막기 위해, **행이 하나라도 있는 DB에는 시딩과 부하를 진행하지 않는다.**

검사는 API 핸들러가 아니라 쓰기를 실제로 수행하는 함수 안쪽에서 호출해야 한다.
핸들러에만 두면 CLI나 다른 호출 경로로 우회된다.
"""
from __future__ import annotations

import logging

from ..config import TargetDB
from .introspect import Table, fingerprint, introspect

log = logging.getLogger(__name__)


class NotEmptyError(RuntimeError):
    """대상 DB에 이미 데이터가 있다."""


class SchemaMismatchError(RuntimeError):
    """비교 대상 두 DB의 스키마가 다르다."""


def row_counts(target: TargetDB, database: str) -> dict[str, int]:
    """{"schema.table": 행수} — sys.partitions 기준(대략값, COUNT(*)보다 훨씬 빠르다)."""
    return {k: t.row_count for k, t in introspect(target, database).items()}


def check_empty(target: TargetDB, databases: list[str],
                tables: dict[str, dict[str, Table]] | None = None) -> dict:
    """빈 DB 검사 결과. `ok`가 False면 시딩·부하를 진행해선 안 된다.

    `tables`를 넘기면 이미 조회한 결과를 재사용한다 (같은 요청에서 두 번 조회하지
    않도록). 없으면 여기서 조회한다.
    """
    occupied = []
    total_tables = 0
    for db in databases:
        t = (tables or {}).get(db) or introspect(target, db)
        total_tables += len(t)
        occupied += [
            {"database": db, "table": k, "rows": v.row_count}
            for k, v in sorted(t.items()) if v.row_count > 0
        ]
    return {
        "ok": not occupied,
        "label": target.label,
        "databases": databases,
        "table_count": total_tables,
        "occupied": occupied,
        # 테이블이 0개인 것도 정상이 아니다 — 스키마를 적용하지 않은 DB이거나
        # 조회 권한이 없는 것이므로, "비었다"와 구분해서 알린다.
        "warning": ("테이블이 하나도 보이지 않는다 — 스키마가 적용되지 않았거나 "
                    "sys.* 조회 권한이 없다" if total_tables == 0 else None),
    }


def require_empty(target: TargetDB, databases: list[str],
                  tables: dict[str, dict[str, Table]] | None = None) -> None:
    """빈 DB가 아니면 예외. 쓰기를 수행하는 함수의 첫 줄에서 호출한다.

    `tables`를 넘기면 이미 조회한 결과를 재사용한다 — 넘기지 않으면 한 번의
    시딩에서 스키마를 네 번 조회하게 된다 (200 테이블이면 16회 왕복).
    """
    result = check_empty(target, databases, tables=tables)
    if result["table_count"] == 0:
        raise NotEmptyError(
            f"[{target.label}] {result['warning']} (대상 DB: {', '.join(databases)})")
    if not result["ok"]:
        top = result["occupied"][:5]
        detail = ", ".join(f"{o['table']} {o['rows']:,}행" for o in top)
        more = (f" 외 {len(result['occupied']) - len(top)}개"
                if len(result["occupied"]) > len(top) else "")
        raise NotEmptyError(
            f"[{target.label}] 이미 데이터가 있는 테이블이 있다 — 이 도구는 빈 DB에만 "
            f"쓰기를 수행한다: {detail}{more}")


def compare_schemas(a_tables: dict[str, Table],
                    b_tables: dict[str, Table],
                    a_label: str = "A", b_label: str = "B") -> dict:
    """두 대상의 스키마 대조. 쌍 비교는 스키마가 같아야 성립한다."""
    fa, fb = fingerprint(a_tables), fingerprint(b_tables)
    only_a = sorted(set(fa) - set(fb))
    only_b = sorted(set(fb) - set(fa))
    differing = sorted(k for k in set(fa) & set(fb) if fa[k] != fb[k])
    return {
        "same": not (only_a or only_b or differing),
        "a_label": a_label, "b_label": b_label,
        "table_count": {a_label: len(fa), b_label: len(fb)},
        "only_in_a": only_a,
        "only_in_b": only_b,
        "differing": differing,
    }
