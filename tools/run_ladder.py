#!/usr/bin/env python3
"""open-loop TPS 래더 실행기 — 두 인스턴스의 리소스 사용량을 비교한다.

목적은 하나다: **양쪽에 같은 TPS를 걸고 리소스 차이를 본다.** open-loop pacing으로
처리량을 고정하면 "일의 양"이 같아지므로, 남는 차이가 리소스 효율 차이다.

그래서 TPS 레벨은 **양쪽이 모두 여유롭게 달성하는 구간**에서 골라야 한다. 한쪽이
목표에 미달하면 "같은 TPS"라는 전제가 깨져 비교가 무의미해진다 — 미달한 쪽은 CPU가
낮게 찍혀 오히려 유리해 보인다. 달성률은 `iso_tps_compare.py`의 hit% 로 확인하고,
95% 미만인 레벨은 리포트가 자동으로 제외한다.

비교 쌍과 TPS 레벨은 `comparisons.yaml`에서 읽는다. 이 파일에 값을 복사해 두지
않는다 — 사본은 반드시 갈라지고, 갈라진 실행 설정은 리포트가 설명하는 조건과
달라진다.

사용법:
  python tools/run_ladder.py --list
  python tools/run_ladder.py --pair 0
  python tools/run_ladder.py --pair 0 --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from loadgen import comparisons  # noqa: E402  (needs path above)
from loadgen.presets import RUN_DEFAULTS  # noqa: E402

BASE = os.environ.get("LOADGEN_URL", "http://127.0.0.1:8010")
COOKIE = "/tmp/ladder_cj.txt"


def sh(cmd: list[str]) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, check=False).stdout


def login(password: str) -> None:
    if not password:
        return  # 게이트가 비활성이면 로그인 불필요
    sh(["curl", "-s", "-c", COOKIE, "-X", "POST", f"{BASE}/login",
        "-d", f"password={password}", "-o", "/dev/null"])


def api(path: str, body: dict | None = None) -> dict:
    cmd = ["curl", "-s", "-b", COOKIE, f"{BASE}{path}"]
    if body is not None:
        cmd += ["-X", "POST", "-H", "Content-Type: application/json",
                "-d", json.dumps(body)]
    out = sh(cmd)
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return {"raw": out}


def wait_finish(poll: int = 10, limit: int = 1800) -> str:
    t0 = time.time()
    while time.time() - t0 < limit:
        st = api("/api/run/status").get("status", "?")
        if st in ("finished", "error", "none"):
            return st
        time.sleep(poll)
    return "timeout"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pair", type=int, help="comparisons.yaml의 쌍 인덱스 (--list로 확인)")
    ap.add_argument("--list", action="store_true", help="정의된 비교 쌍 출력")
    # 패스워드에 기본값을 두지 않는다. 여기 박아두면 저장소에 그대로 남는다.
    ap.add_argument("--password", default=os.environ.get("LOADGEN_PASSWORD", ""),
                    help="웹 UI 패스워드 (기본값: 환경변수 LOADGEN_PASSWORD)")
    ap.add_argument("--workload", help="쌍에 지정된 워크로드 대신 사용할 이름")
    ap.add_argument("--duration", type=int, default=RUN_DEFAULTS["duration_sec"])
    ap.add_argument("--warmup", type=int, default=RUN_DEFAULTS["warmup_sec"])
    ap.add_argument("--read-pct", type=int, default=RUN_DEFAULTS["read_pct"],
                    help="대상 워크로드의 실측 비율로 맞출 것")
    ap.add_argument("--processes", type=int, default=RUN_DEFAULTS["processes"])
    ap.add_argument("--threads", type=int, default=RUN_DEFAULTS["threads_per_process"])
    ap.add_argument("--tps", type=int, nargs="+", metavar="TPS",
                    help="쌍의 기본 레벨 대신 지정한 레벨만 실행. 한 레벨이 hit<95%%로 "
                         "무효 처리된 뒤 원인을 고치고 그 레벨만 다시 돌릴 때 쓴다 "
                         "(유효한 레벨을 재실행하면 그만큼 다른 시각의 데이터가 섞인다)")
    ap.add_argument("--settle", type=int, default=RUN_DEFAULTS["settle_sec"],
                    help="런 사이 대기(초). Enhanced Monitoring 스냅샷은 런 종료 후 "
                         "~90초에 저장되고 창을 앞뒤로 넓게 잡으므로, 이보다 짧게 두면 "
                         "다음 런의 부하가 이전 런 스냅샷에 섞인다")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    pairs = comparisons.pairs()
    if args.list or args.pair is None:
        if not pairs:
            print("comparisons.yaml에 비교 쌍이 없다.", file=sys.stderr)
            return 1
        for i, p in enumerate(pairs):
            print(f"[{i}] {p['name']}\n    A={p['a_label']} ({p['a_vcpu']} vCPU) "
                  f"B={p['b_label']} ({p['b_vcpu']} vCPU)\n"
                  f"    workload={p['workload']} tps={p['tps']}")
        return 0 if args.list else 1

    if not 0 <= args.pair < len(pairs):
        print(f"쌍 인덱스가 범위를 벗어났다: {args.pair} (0..{len(pairs)-1})", file=sys.stderr)
        return 1
    pair = pairs[args.pair]
    workload = args.workload or pair["workload"]
    if not workload:
        print("워크로드가 지정되지 않았다 — comparisons.yaml의 workload 또는 --workload",
              file=sys.stderr)
        return 1
    levels = args.tps or pair["tps"]
    if not levels:
        print("TPS 레벨이 없다 — comparisons.yaml의 tps 또는 --tps", file=sys.stderr)
        return 1

    # 인스턴스 단위로 묶지 않고 TPS 레벨을 번갈아 돌린다. 시간대에 따른 드리프트
    # (다른 부하, 스토리지 상태)가 한쪽에만 몰리지 않게 한다.
    targets = [pair["a_label"], pair["b_label"]]
    plan = [(tps, tgt) for tps in levels for tgt in targets]

    per_run = args.warmup + args.duration + args.settle
    print(f"쌍 [{args.pair}] {pair['name']} / 워크로드 {workload} / "
          f"{len(plan)} 런 × {args.warmup + args.duration}초 + {args.settle}초 대기 "
          f"= 약 {len(plan) * per_run / 60:.0f}분")
    for tps, tgt in plan:
        print(f"  open {tps:>6} tps -> {tgt}")
    if args.dry_run:
        return 0

    login(args.password)
    for i, (tps, tgt) in enumerate(plan, 1):
        body = {
            "label": tgt, "workload_name": workload, "mode": "open",
            "duration_sec": args.duration, "warmup_sec": args.warmup,
            "processes": args.processes, "threads_per_process": args.threads,
            "target_tps": tps, "read_pct": args.read_pct,
            # 리포트는 이 note로 배치를 구분해 짝을 맺는다 (analysis.bucket).
            "note": f"ladder {pair['name']} L{tps}",
        }
        r = api("/api/run", body)
        print(f"[{i}/{len(plan)}] {tgt} @ {tps} tps -> {r.get('run_id', r)}", flush=True)
        if "run_id" not in r:
            print("  실패, 중단", file=sys.stderr)
            return 1
        print(f"  status: {wait_finish()}", flush=True)
        if i < len(plan):
            time.sleep(args.settle)
    print("\n래더 완료. 비교표: python tools/iso_tps_compare.py --mode open")
    return 0


if __name__ == "__main__":
    sys.exit(main())
