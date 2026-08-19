"""부하 실행 기본값 — 단일 정보원.

세 곳이 이 값을 읽는다: UI의 Run 폼, `tools/run_ladder.py`, 리포트의 "측정 조건"
서술. 사본을 만들면 반드시 갈라지고, 갈라진 측정 조건은 데이터를 조용히 잘못
설명한다. 그래서 여기에만 둔다.

TPS 레벨처럼 대상마다 달라지는 값은 여기 넣지 않는다 — `comparisons.yaml`에서
사용자가 정의한다.
"""
from __future__ import annotations

RUN_DEFAULTS = {
    # 쌍 비교의 기본은 open이다. 양쪽에 같은 TPS를 걸어 "일의 양"을 고정해야
    # 남는 차이를 리소스 효율 차이로 읽을 수 있다.
    "mode": "open",
    "duration_sec": 180,
    "warmup_sec": 30,
    "target_tps": 1000,

    # 읽기 비율. 대상 워크로드가 쓰기 위주라면 낮춰야 한다 — 실제와 다른 비율로
    # 측정하면 다른 워크로드를 측정한 것이 된다. 실측 비율은 대상 DB에서
    # `tools/calibrate_rw.sql`로 확인할 수 있다.
    "read_pct": 50,

    # 커넥션 상한 = processes × threads_per_process.
    # open 모드에서는 페이싱이 실제 동시 사용량을 정하므로 이 값은 상한 역할만 한다.
    # pyodbc가 서버 호출 중 GIL을 놓기 때문에 스레드로도 실제 I/O 병렬이 나온다.
    "processes": 8,
    "threads_per_process": 16,

    # 런 사이 대기(초). Enhanced Monitoring 스냅샷은 런 종료 후 ~90초에 저장되고
    # 창을 앞뒤로 넓게 잡으므로, 이보다 짧으면 다음 런의 부하가 이전 런 스냅샷에
    # 섞인다.
    "settle_sec": 120,
}
