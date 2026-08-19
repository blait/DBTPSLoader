"""CPU 지표의 출처를 고른다.

정규화 지표(`cpu_ms_txn` 등)는 CPU%가 있어야 계산된다. 원본 도구는 RDS Enhanced
Monitoring에만 의존해서, AWS 자격증명이 없으면 리포트가 "유효 레벨이 없다"만
출력했다 — 부하는 정상적으로 돌았는데도 결과가 통째로 비었다.

범용 도구에서는 CPU 출처를 세 가지로 나눈다:

  rds_em  RDS Enhanced Monitoring (5초). `runs/<id>/rds_metrics.json`에 스냅샷된다.
  manual  사용자가 런별 CPU%를 직접 입력. `runs/<id>/cpu.json`을 읽는다.
  none    CPU 없이 처리량·레이턴시만 비교.

`none`이어도 리포트는 나온다. TPS와 p50/p99 비교는 그 자체로 유효한 결과이고,
"CPU를 못 봤다"는 사실을 리포트가 명시한다.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

SOURCES = ("rds_em", "manual", "none")

# 사용자가 직접 입력할 때 쓰는 파일 형식:
#   {"cpu_pct": 24.9, "cpu_pct_mean": 21.3, "note": "OS 모니터링에서 읽음"}
MANUAL_FILE = "cpu.json"


def points_for_run(run_dir: Path, source: str) -> list[dict]:
    """런 하나의 CPU 시계열. 없으면 빈 리스트.

    `rds_em`은 EM 스냅샷을, `manual`은 단일 값을 시계열 한 점으로 변환해 돌려준다.
    호출부(`analysis._em_stats`)는 형식이 같으므로 출처를 몰라도 된다.
    """
    if source == "none":
        return []

    if source == "rds_em":
        f = run_dir / "rds_metrics.json"
        if not f.exists():
            return []
        try:
            return json.loads(f.read_text()).get("points", [])
        except Exception as exc:  # noqa: BLE001
            log.warning("%s 읽기 실패: %s", f, exc)
            return []

    if source == "manual":
        f = run_dir / MANUAL_FILE
        if not f.exists():
            return []
        try:
            d = json.loads(f.read_text())
        except Exception as exc:  # noqa: BLE001
            log.warning("%s 읽기 실패: %s", f, exc)
            return []
        cpu = d.get("cpu_pct")
        if cpu is None:
            return []
        # 단일 값이므로 max와 mean이 같다. 시계열 한 점으로 감싸 형식을 맞춘다.
        return [{"ts": 0, "cpu_total": float(cpu),
                 "disk_riops": d.get("disk_riops", 0),
                 "disk_wiops": d.get("disk_wiops", 0)}]

    log.warning("알 수 없는 CPU 소스: %s", source)
    return []


def describe(source: str, has_points: bool) -> str:
    """리포트에 실을 출처 설명. 없을 때 그 사실을 분명히 쓴다."""
    if source == "none":
        return ("CPU 지표를 수집하지 않았다 (`cpu.source: none`) — 처리량과 레이턴시만 "
                "비교한다. 트랜잭션당 CPU 효율은 이 리포트로 판단할 수 없다.")
    if source == "rds_em":
        return ("CPU는 RDS Enhanced Monitoring 5초 지표에서 읽었다."
                if has_points else
                "⚠️ RDS Enhanced Monitoring 지표가 없다 — AWS 자격증명이나 EM 설정을 "
                "확인할 것. CPU 관련 판정은 비어 있다.")
    if source == "manual":
        return ("CPU는 사용자가 입력한 값이다 (`runs/<id>/cpu.json`)."
                if has_points else
                "⚠️ 사용자 입력 CPU 값이 없다 — 각 런 폴더에 `cpu.json`을 두어야 한다.")
    return f"알 수 없는 CPU 소스: {source}"
