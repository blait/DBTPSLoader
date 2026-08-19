"""워크로드 드라이런 — 부하 전에 SQL이 실제로 동작하는지 확인한다.

부하를 걸어봐야 아는 문제가 세 가지 있다.

**1. 문법 오류.** 자동 생성한 SQL이 잘못된 열 이름이나 타입 불일치를 담고 있으면
런이 시작되자마자 에러로 채워진다.

**2. 0행 반환.** 조회가 아무것도 맞히지 못하는 경우다. **이건 에러가 아니다** —
서버는 정상 응답하고 트랜잭션은 "성공"으로 집계된다. 그래서 에러율 게이트에도
걸리지 않고, TPS만 높게 나온다. 부하가 조용히 무의미해지는 가장 흔한 경로다.

**3. 풀스캔.** 인덱스를 타지 않는 조회는 작은 테이블에서는 빠르지만 실규모에서
전혀 다르게 동작한다. 실행 계획을 봐야 알 수 있다.

쓰기는 `BEGIN TRAN` → 실행 → `ROLLBACK`으로 확인한다. 데이터를 건드리지 않는다.
"""
from __future__ import annotations

import logging
import time

from ..config import TargetDB
from ..db import connect
from ..seed.datagen import Gen
from .store import _param_fn

log = logging.getLogger(__name__)

# 실행 계획에서 찾는 문자열. 스캔이 잡히면 실규모에서 문제가 된다.
_SCAN_MARKERS = ("Table Scan", "Clustered Index Scan", "Index Scan",
                 "Columnstore Index Scan")
_SEEK_MARKERS = ("Index Seek", "Clustered Index Seek", "Key Lookup")


def _plan_verdict(plan_xml: str) -> tuple[str, str]:
    """(판정, 설명). 실행 계획 XML에서 스캔/시크를 찾는다."""
    if not plan_xml:
        return "unknown", "실행 계획을 얻지 못했다"
    scans = [m for m in _SCAN_MARKERS if m in plan_xml]
    seeks = [m for m in _SEEK_MARKERS if m in plan_xml]
    if scans and not seeks:
        return "scan", f"스캔만 사용: {', '.join(scans)}"
    if scans and seeks:
        return "mixed", f"시크+스캔 혼합: {', '.join(scans)}"
    if seeks:
        return "seek", ", ".join(seeks)
    return "unknown", "시크·스캔 표지를 찾지 못했다"


def _explain(cur, sql: str, params: tuple) -> str:
    """실제 실행 계획(actual plan)을 문자열로. 실패하면 빈 문자열.

    추정 계획(SHOWPLAN_XML)이 아니라 실제 계획을 쓴다 — 추정 계획은 파라미터를
    스니핑하지 않아 실제 실행과 다른 계획을 낼 수 있다.
    """
    try:
        cur.execute("SET STATISTICS XML ON")
        cur.execute(sql, params)
        # 첫 결과셋은 데이터, 다음이 계획 XML이다.
        while cur.nextset():
            row = cur.fetchone()
            if row and isinstance(row[0], str) and "ShowPlanXML" in row[0]:
                return row[0]
        return ""
    except Exception as exc:  # noqa: BLE001
        log.debug("실행 계획 수집 실패: %s", exc)
        return ""
    finally:
        try:
            cur.execute("SET STATISTICS XML OFF")
        except Exception:  # noqa: BLE001
            pass


def dryrun(target: TargetDB, workload: dict, ctx: dict,
           explain: bool = True) -> dict:
    """워크로드의 모든 트랜잭션을 한 번씩 실행해본다.

    `ctx`는 id 범위다 (`schema.ranges.id_ranges`). 없으면 파라미터 생성이 실패하고
    그 사실이 결과에 남는다 — 시딩을 건너뛴 상태가 여기서 드러난다.
    """
    results: list[dict] = []
    conns: dict[str, object] = {}
    g = Gen(seed=1)

    try:
        for t in workload.get("txns", []):
            if t.get("disabled"):
                continue
            name, kind, db = t["name"], t["kind"], t["database"]
            entry: dict = {"name": name, "kind": kind, "database": db,
                           "status": "ok", "rows": None, "ms": None,
                           "plan": None, "plan_note": None, "error": None}

            # 파라미터 생성. id 범위가 없으면 여기서 걸린다.
            try:
                params = _param_fn(t.get("params") or [[] for _ in t["sql"]])(g, ctx)
            except Exception as exc:  # noqa: BLE001
                entry.update(status="error", error=f"파라미터 생성 실패: {exc}")
                results.append(entry)
                continue

            conn = conns.get(db)
            if conn is None:
                try:
                    conn = conns[db] = connect(target, db, autocommit=False)
                except Exception as exc:  # noqa: BLE001
                    entry.update(status="error", error=f"연결 실패: {str(exc)[:200]}")
                    results.append(entry)
                    continue

            cur = conn.cursor()
            t0 = time.perf_counter()
            try:
                total_rows = 0
                plan_xml = ""
                for sql, p in zip(t["sql"], params):
                    is_select = sql.lstrip().upper().startswith("SELECT")
                    if explain and is_select:
                        plan_xml = plan_xml or _explain(cur, sql, p)
                    cur.execute(sql, p)
                    if is_select:
                        total_rows += len(cur.fetchall())
                    else:
                        # rowcount가 -1이면 드라이버가 모르는 경우다
                        total_rows += max(cur.rowcount, 0)
                entry["ms"] = round((time.perf_counter() - t0) * 1000, 2)
                entry["rows"] = total_rows

                if plan_xml:
                    verdict, note = _plan_verdict(plan_xml)
                    entry["plan"], entry["plan_note"] = verdict, note
                    if verdict == "scan":
                        entry["status"] = "warn"
                        entry["error"] = ("인덱스를 타지 않는다 — 작은 테이블에서는 "
                                          "빠르지만 실규모에서 전혀 다르게 동작한다")

                # 0행은 에러가 아니라서 에러율 게이트에 걸리지 않는다. 여기서
                # 잡지 않으면 부하가 조용히 무의미해진다.
                if kind == "read" and total_rows == 0:
                    entry["status"] = "warn"
                    entry["error"] = ("행을 하나도 맞히지 못했다 — 이 조회는 서버에 "
                                      "일을 주지 않으면서 '성공'으로 집계된다")
            except Exception as exc:  # noqa: BLE001
                entry.update(status="error", error=str(exc)[:300])
            finally:
                # 쓰기는 반드시 되돌린다. 검증이 데이터를 바꾸면 이후 측정의
                # 전제(양쪽 데이터 동일)가 깨진다.
                try:
                    conn.rollback()
                except Exception:  # noqa: BLE001
                    pass
            results.append(entry)
    finally:
        for c in conns.values():
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass

    errors = [r for r in results if r["status"] == "error"]
    warns = [r for r in results if r["status"] == "warn"]
    return {
        "label": target.label,
        "total": len(results),
        "ok": len(results) - len(errors) - len(warns),
        "warn": len(warns),
        "error": len(errors),
        # 오류가 하나라도 있으면 부하를 걸 준비가 안 된 것이다. 경고는 사용자가
        # 판단할 몫이다 — 풀스캔이 의도한 워크로드일 수도 있다.
        "ready": not errors,
        "results": results,
        "note": ("오류가 있다 — 이 상태로 부하를 걸면 에러만 쌓인다."
                 if errors else
                 "경고가 있다 — 0행 조회나 풀스캔은 측정을 왜곡한다. 확인할 것."
                 if warns else
                 "모든 트랜잭션이 정상 동작한다."),
    }
