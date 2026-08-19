#!/usr/bin/env python3
"""iso-TPS 비교표: 같은 TPS에서 HT-on vs HT-off의 리소스 사용량.

지표 정의·steady 구간 산정·유효성 판정은 모두 `loadgen/analysis.py`에 있다.
UI의 Report 탭이 같은 모듈을 쓰므로 CLI와 웹이 다른 숫자를 낼 수 없다.

CPU%는 "가용 vCPU 대비 비율"이므로 32 vCPU와 16 vCPU 인스턴스 사이에서 같은
단위가 아니다. 그래서 정규화해서 본다:

  cpu_vcpu    = CPU% / 100 * vCPU        (실제로 소비한 vCPU 수)
  cpu_ms_txn  = cpu_vcpu * 1000 / TPS    (트랜잭션 1건당 CPU 시간)

cpu_ms_txn 이 HT on/off를 공정하게 비교하는 지표다. headroom_vcpu(남은 vCPU)
는 "피크를 얼마나 더 흡수할 수 있나"에 답한다.

CPU%는 부하 구간의 **최댓값**을 쓴다 (`analysis.CPU_BASIS`). cpu%_mean 열은
참고용으로 나란히 찍는다 — 최댓값만 보면 평평한 구간과 스파이크성 구간을
구분할 수 없다.

사용법 (`python3`이 아니라 `.venv/bin/python` — boto3가 거기 있고, 없으면 RDS
실측 조회가 실패해 인스턴스 표가 정적 기본값으로 조용히 내려앉는다):
  .venv/bin/python tools/iso_tps_compare.py              # runs/ 전체 자동 페어링
  .venv/bin/python tools/iso_tps_compare.py --mode open  # open-loop 런만
  .venv/bin/python tools/iso_tps_compare.py --csv out.csv
  .venv/bin/python tools/iso_tps_compare.py --md report.md  # 리포트 문서 생성
  .venv/bin/python tools/iso_tps_compare.py --run-ids A B C D --md report.md
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loadgen.analysis import build_report, scan_runs  # noqa: E402
from loadgen.rds_facts import facts_for_rows, vcpu_warnings  # noqa: E402
from loadgen.report_md import render_markdown  # noqa: E402

RUNS = Path(__file__).resolve().parent.parent / "runs"


def discarded_runs(runs_dir: Path) -> list[dict]:
    """Runs quarantined into runs/_*/ subfolders, so exclusions stay visible."""
    return [{"run_id": d.name, "skip": f"`{q.name}/`로 격리됨 — 집계 대상 아님"}
            for q in sorted(p for p in runs_dir.iterdir()
                            if p.is_dir() and p.name.startswith("_"))
            for d in sorted(p for p in q.iterdir() if p.is_dir())]


def fmt(rows: list[dict], cols: list[str]) -> str:
    hdr = {c: c for c in cols}
    w = {c: max(len(str(r.get(c, "") if r.get(c) is not None else "-")) for r in [hdr] + rows) for c in cols}
    out = [" ".join(c.rjust(w[c]) for c in cols),
           " ".join("-" * w[c] for c in cols)]
    for r in rows:
        out.append(" ".join(str(r.get(c) if r.get(c) is not None else "-").rjust(w[c]) for c in cols))
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["open", "closed"], help="이 모드만")
    ap.add_argument("--csv", help="CSV로도 저장")
    ap.add_argument("--md", help="마크다운 리포트로도 저장 (UI Report 탭과 동일)")
    ap.add_argument("--run-ids", nargs="+", metavar="RUN_ID",
                    help="이 런들만 집계한다. 같은 설정의 런이 여러 배치에 걸쳐 "
                         "있을 때 최신 배치만 리포트로 만들 때 쓴다 — 지정하지 "
                         "않으면 runs/ 전체를 자동 페어링하므로 옛 배치가 섞인다")
    args = ap.parse_args()

    if not RUNS.exists():
        print(f"no runs dir: {RUNS}", file=sys.stderr)
        return 1

    rows, skipped = scan_runs(RUNS, mode=args.mode, run_ids=args.run_ids)
    if not rows:
        print("비교할 런이 없다", file=sys.stderr)
        for s in skipped:
            print(f"  건너뜀 {s['run_id']}: {s['skip']}", file=sys.stderr)
        return 1

    cols = ["run_id", "inst", "vcpu", "target_tps", "conns", "read_pct", "tps",
            "hit_pct", "cpu_pct", "cpu_pct_mean", "cpu_vcpu", "cpu_ms_txn",
            "headroom_vcpu", "p50_ms", "p99_ms", "w_iops", "w_iops_max",
            "errors", "err_pct", "em_n"]
    print("=== 전체 런 ===")
    print(fmt(rows, cols))
    for s in skipped:
        print(f"  건너뜀 {s['run_id']}: {s['skip']}")

    # Same instance facts and quarantine list the UI uses, so `--md` and the
    # Report tab render the same document rather than two similar ones.
    facts = facts_for_rows(rows)
    report = build_report(rows, skipped, instances=facts,
                         discarded=discarded_runs(RUNS))
    report["warnings"] = vcpu_warnings(rows, facts)
    for w in report["warnings"]:
        print(f"\n⚠️  {w}", file=sys.stderr)

    # ---- 쌍 비교 -----------------------------------------------------------
    for sc in report["scenarios"]:
        on, off = sc["on"], sc["off"]
        print(f"\n=== {sc['title']}: {on} vs {off} ===")
        print("  같은 TPS를 걸고 CPU 차이를 본다. valid=n 인 행은 한쪽이 목표에")
        print("  미달(hit<95%)했거나 에러율이 1% 이상(err%)이라 '같은 일을 했다'는")
        print("  전제가 깨진 것 — CPU 비교에 쓰지 말 것. 실패 트랜잭션은 CPU를 덜")
        print("  쓰면서 커넥션 재수립으로 레이턴시만 올린다.")
        print("  cpu_ms_txn = 트랜잭션 1건당 CPU 시간. ratio<1.0 = HT-off가 CPU를 덜 씀.")
        comp = []
        for lv in sc["levels"]:
            a, b = lv["on"], lv["off"]
            comp.append({
                "target_tps": lv["target_tps"] if lv["target_tps"] is not None else "closed",
                "conns": lv["conns"], "read_pct": lv["read_pct"],
                "valid": "y" if lv["valid"] else "n",
                f"tps_{on}": a["tps"], f"tps_{off}": b["tps"],
                f"hit%_{on}": a.get("hit_pct"), f"hit%_{off}": b.get("hit_pct"),
                f"err%_{on}": a.get("err_pct"), f"err%_{off}": b.get("err_pct"),
                f"cpu%max_{on}": a.get("cpu_pct"), f"cpu%max_{off}": b.get("cpu_pct"),
                f"cpu%avg_{on}": a.get("cpu_pct_mean"), f"cpu%avg_{off}": b.get("cpu_pct_mean"),
                f"vcpu_{on}": a.get("cpu_vcpu"), f"vcpu_{off}": b.get("cpu_vcpu"),
                f"cpums_{on}": a.get("cpu_ms_txn"), f"cpums_{off}": b.get("cpu_ms_txn"),
                "cpums_ratio": lv["cpums_ratio"],
                f"hdrm_{on}": a.get("headroom_vcpu"), f"hdrm_{off}": b.get("headroom_vcpu"),
                f"p99_{on}": a.get("p99_ms"), f"p99_{off}": b.get("p99_ms"),
                "p99_ratio": lv["p99_ratio"],
                f"em_n_{on}": a.get("em_n"), f"em_n_{off}": b.get("em_n"),
            })
        print(fmt(comp, list(comp[0].keys())))
        for lv in sc["invalid_levels"]:
            print(f"  [무효 {lv['target_tps']}] " + " / ".join(lv["reasons"]))
        for u in sc["unpaired"]:
            print(f"  [짝없음] {u['run_id']} ({u['inst']}, target {u['target_tps']})")

    for u in report["unused_runs"]:
        print(f"\n비교에 안 쓰인 런: {u['run_id']} ({u['inst']}, {u['mode']}, "
              f"target {u['target_tps']}, tps {u['tps']})")

    if args.csv:
        with open(args.csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        print(f"\nCSV -> {args.csv}")
    if args.md:
        Path(args.md).write_text(render_markdown(report))
        print(f"Markdown -> {args.md}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
