"""CLI 스모크 테스트 — 웹 UI 없이 짧은 부하를 걸어본다.

시딩과 워크로드 저장은 미리 되어 있어야 한다 (UI 또는 API로).

사용법:
  python tools/smoke_run.py --host 127.0.0.1 --password '<pw>' --workload draft
  python tools/smoke_run.py --host 127.0.0.1 --password '<pw>' --workload draft \\
      --duration 30 --read-pct 50
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loadgen.config import RunConfig, TargetDB  # noqa: E402
from loadgen.metrics.export import load_run  # noqa: E402
from loadgen.runner.coordinator import Run  # noqa: E402
from loadgen.workload import store as workload_store  # noqa: E402


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=1433)
    ap.add_argument("--user", default="sa")
    # 패스워드에 기본값을 두지 않는다 — 저장소에 남는다.
    ap.add_argument("--password", default=os.environ.get("MSSQL_PASSWORD", ""),
                    help="DB 패스워드 (기본값: 환경변수 MSSQL_PASSWORD)")
    ap.add_argument("--label", default="smoke")
    ap.add_argument("--workload", required=True, help="workloads/<name>.json")
    ap.add_argument("--duration", type=int, default=20)
    ap.add_argument("--warmup", type=int, default=5)
    ap.add_argument("--read-pct", type=int, default=50)
    ap.add_argument("--processes", type=int, default=2)
    ap.add_argument("--threads", type=int, default=4)
    args = ap.parse_args()

    if not args.password:
        print("패스워드가 필요하다 (--password 또는 MSSQL_PASSWORD)", file=sys.stderr)
        return 1
    try:
        workload = workload_store.load(args.workload)
    except FileNotFoundError as e:
        print(e, file=sys.stderr)
        return 1

    t = TargetDB(label=args.label, host=args.host, port=args.port, user=args.user,
                 password=args.password, login_timeout=60)
    cfg = RunConfig(mode="closed", duration_sec=args.duration, warmup_sec=args.warmup,
                    processes=args.processes, threads_per_process=args.threads,
                    read_pct=args.read_pct)
    run = Run(t, cfg, workload=workload)
    run.start()
    while run.status != "finished":
        time.sleep(1)

    data = load_run(run.run_id)
    s = data["summary"]
    print(f"\nrun_id: {run.run_id}")
    print(f"avg_tps={s['avg_tps']} total={s['total_txns']} errors={s['total_errors']} "
          f"steady_s={s['steady_seconds']}")
    for name, m in sorted(s["per_txn"].items(), key=lambda x: -x[1]["count"]):
        print(f"  {name:<32} n={m['count']:>7} err={m['errors']:>4} "
              f"p50={m['p50_ms']:>7.1f} p95={m['p95_ms']:>7.1f} p99={m['p99_ms']:>8.1f}ms")
    # 에러는 조용히 넘기지 않는다 — 실패 표본을 성능 결과로 읽는 것을 막는다.
    if s["total_errors"]:
        print("\n에러 샘플:")
        print(json.dumps(s.get("err_samples", {}), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
