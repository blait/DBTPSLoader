"""시딩 플랜 — 조회한 스키마로 "무엇을 몇 행 넣을지"를 추천한다.

사용자는 총 행수만 정하고, 도구가 스키마 구조를 근거로 테이블 간 분배를 추정한다.
균등 분배는 쓰지 않는다 — 자식 테이블이 부모보다 많아야 조인이 현실적인 행수를
돌려주고, 로그성 테이블이 압도적으로 커야 append-only 쓰기 패턴이 의미를 갖는다.

추천값은 전부 UI에서 수정할 수 있다. 수정 없이 진행했는지를 플랜에 기록해,
나중에 결과를 볼 때 행수 분포가 검증된 것으로 오인되지 않게 한다.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from .introspect import Table

log = logging.getLogger(__name__)

PLAN_DIR = Path(__file__).resolve().parent.parent.parent / "plans"

# 테이블 성격별 상대 가중치. 절대값이 아니라 비율만 의미가 있다.
_W_LOOKUP = 1        # 참조만 되는 소형 코드/설정 테이블
_W_ENTITY = 10       # 많이 참조되는 마스터 (계정, 상품 등)
_W_TXN = 80          # FK를 갖고 다른 것에도 참조되는 거래성
_W_CHILD = 200       # 거래의 상세 라인 (부모보다 많다)
_W_LOG = 600         # append-only 로그/감사 — 보통 가장 크다

# 엔티티 테이블 최소 행수. 너무 작으면 편중 분포가 의미를 잃고, FK 자식이
# 참조할 부모가 부족해진다.
_MIN_ROWS = 50


def _classify(t: Table, referenced_by: int) -> tuple[str, int]:
    """(성격, 가중치). 스키마 신호만으로 추정한다."""
    n_fk = len(t.foreign_keys)
    n_col = len(t.columns)
    has_date = any(c.type in ("datetime", "datetime2", "smalldatetime", "date")
                   for c in t.columns)
    is_append = bool(t.identity_pk) and has_date

    if n_fk == 0 and referenced_by > 0:
        # 아무것도 참조하지 않고 남에게 참조되는 것 = 마스터.
        # 컬럼이 적고 참조가 많으면 코드성 룩업으로 본다.
        return ("lookup", _W_LOOKUP) if n_col <= 4 and referenced_by >= 3 \
            else ("entity", _W_ENTITY)
    if n_fk >= 2 and referenced_by == 0 and n_col <= 6:
        return "join", _W_CHILD
    if n_fk >= 1 and referenced_by == 0:
        # 잎 노드. 날짜 + IDENTITY면 로그로 본다.
        return ("log", _W_LOG) if is_append else ("child", _W_CHILD)
    if n_fk >= 1 and referenced_by > 0:
        return "txn", _W_TXN
    # FK도 없고 참조도 안 되는 고립 테이블
    return ("log", _W_LOG) if is_append else ("standalone", _W_ENTITY)


def _topo_order(tables: dict[str, Table]) -> list[str]:
    """FK 위상 정렬 — 부모부터. 순환은 남은 순서대로 뒤에 붙인다.

    순환 FK가 있어도 실패하지 않는다. 삽입 중에는 FK 검사를 끄기 때문에
    (`seeder._set_fk_checks`) 순서가 완벽하지 않아도 진행되고, 정렬은 어디까지나
    "가능하면 부모를 먼저"라는 최선의 노력이다.
    """
    deps = {
        k: {fk.ref_table for fk in t.foreign_keys if fk.ref_table != k and fk.ref_table in tables}
        for k, t in tables.items()
    }
    out, done = [], set()
    while len(out) < len(tables):
        ready = sorted(k for k in tables
                       if k not in done and deps[k] <= done)
        if not ready:
            # 순환. 남은 것을 이름 순으로 밀어넣고 끝낸다.
            rest = sorted(k for k in tables if k not in done)
            log.info("FK 순환 감지 — %d개 테이블은 정렬 없이 진행 (FK 검사를 끄고 삽입): %s",
                     len(rest), ", ".join(rest[:5]))
            out += rest
            break
        out += ready
        done.update(ready)
    return out


def draft_plan(tables_by_db: dict[str, dict[str, Table]],
               total_rows: int = 1_000_000,
               name: str = "draft") -> dict:
    """스키마 + 총 행수 → 시딩 플랜 초안.

    `tables_by_db`: {데이터베이스명: introspect() 결과}
    """
    # 몇 개 테이블에서 참조되는지 (성격 판정의 핵심 신호)
    ref_count: dict[str, int] = {}
    for tables in tables_by_db.values():
        for t in tables.values():
            for fk in t.foreign_keys:
                ref_count[fk.ref_table] = ref_count.get(fk.ref_table, 0) + 1

    entries = []
    for db, tables in tables_by_db.items():
        order = _topo_order(tables)
        for pos, key in enumerate(order):
            t = tables[key]
            insertable = [c for c in t.columns if c.insertable]
            uniq_ix = [ix for ix in t.indexes if ix.unique and not ix.primary_key]
            kind, weight = _classify(t, ref_count.get(key, 0))
            entries.append({
                "database": db, "table": t.name, "schema": t.schema,
                "qualified": key,
                "temporal": t.temporal,
                "order": pos,
                "kind": kind,
                "_weight": weight,
                "columns": [c.name for c in insertable],
                "skipped_columns": [
                    {"name": c.name,
                     "reason": ("IDENTITY" if c.identity else
                                "계산 컬럼" if c.computed else c.type)}
                    for c in t.columns if not c.insertable
                ],
                "pk": t.primary_key,
                "foreign_keys": [
                    {"columns": fk.columns, "ref_table": fk.ref_table,
                     "ref_columns": fk.ref_columns}
                    for fk in t.foreign_keys
                ],
                # 트리거·CHECK 제약이 있으면 합성값이 거부될 수 있다. 조용히
                # 넘기지 않고 사용자에게 보인다.
                "warnings": [w for w in (
                    "트리거 있음 — 삽입 시 부수 효과가 생길 수 있다" if t.has_trigger else None,
                    "CHECK 제약 있음 — 합성값이 거부될 수 있다" if t.has_check else None,
                    "시스템 버전 관리(temporal) 테이블 — 이력이 함께 생성된다"
                    if t.temporal else None,
                    "삽입 가능한 컬럼이 없다 (IDENTITY/계산 컬럼뿐)"
                    if not insertable else None,
                    # 유니크 제약에 임의값을 넣으면 중복 키 위반이 난다. 행수가
                    # 값 공간보다 크면 확률이 급격히 올라간다.
                    f"유니크 제약 {len(uniq_ix)}개 — 행수가 많으면 중복 키 위반 가능"
                    if uniq_ix else None,
                ) if w],
            })

    # 가중치대로 총 행수를 분배
    # 삽입 가능한 컬럼이 없는 테이블은 가중치 계산에서도 빼야 한다 — 넣지도 않을
    # 테이블에 배분한 몫만큼 나머지가 줄어든다.
    seedable = [e for e in entries if e["columns"]]
    total_w = sum(e["_weight"] for e in seedable) or 1
    for e in entries:
        e["rows"] = (max(_MIN_ROWS, round(total_rows * e["_weight"] / total_w))
                     if e["columns"] else 0)
        del e["_weight"]

    entries.sort(key=lambda e: (e["database"], e["order"]))
    return {
        "name": name,
        "total_rows_requested": total_rows,
        "total_rows_planned": sum(e["rows"] for e in entries),
        "databases": sorted(tables_by_db),
        "tables": entries,
        # 사용자가 손대지 않았음을 기록한다 (리포트가 이 사실을 표시한다)
        "edited": False,
    }


# --------------------------------------------------------------------- 영속화

def plan_path(name: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in name)
    return PLAN_DIR / f"{safe}.json"


def save_plan(plan: dict) -> Path:
    PLAN_DIR.mkdir(parents=True, exist_ok=True)
    p = plan_path(plan["name"])
    p.write_text(json.dumps(plan, indent=2, ensure_ascii=False, default=str))
    return p


def load_plan(name: str) -> dict:
    p = plan_path(name)
    if not p.exists():
        raise FileNotFoundError(f"시딩 플랜을 찾을 수 없다: {name}")
    return json.loads(p.read_text())


def plan_names() -> list[str]:
    if not PLAN_DIR.exists():
        return []
    return sorted(p.stem for p in PLAN_DIR.glob("*.json"))
