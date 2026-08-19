"""런 아티팩트에서 뽑은 정규화 지표 (같은 TPS 기준 비교).

리포트 엔드포인트(`loadgen.app`)와 CLI 표(`tools/iso_tps_compare.py`)가 같은 것을
읽는다. 모든 값은 `runs/<run_id>/` 아래 파일에서 계산한다 — meta.json,
summary.json, rds_metrics.json. 어떤 수치도 문서에서 옮겨 적지 않는다.

**CPU%는 그대로 비교하면 안 된다.** CPU%는 *가용* vCPU에 대한 비율이라, 32 vCPU
인스턴스와 16 vCPU 인스턴스에서 같은 단위가 아니다. 그래서 전부 정규화한다:

    cpu_vcpu      = CPU% / 100 * vCPU           실제 소비한 vCPU
    cpu_ms_txn    = cpu_vcpu * 1000 / TPS       트랜잭션 1건당 CPU 시간
    headroom_vcpu = (100 - CPU%) / 100 * vCPU   피크에 쓸 수 있는 잔여

`cpu_ms_txn`이 공정한 비교 지표다. 비율이 1.0 미만이면 그쪽이 같은 일에 CPU를 덜 썼다.

**CPU_BASIS가 위 세 값의 입력을 정한다.** 기본은 `max` — 부하 구간 안의 피크 5초
지점이고, 구간 평균이 아니다. 사이징을 정하는 것은 피크이기 때문이다. 평균은
"평소 얼마였나"에 답하지만 실제 질문은 "가장 나쁠 때 버텼나"이고, 짧은 스파이크를
아예 지워버린다. 두 기준의 차이를 감출 수 없도록 `cpu_pct_mean`을 모든 행에 함께
남긴다 — 평탄한 구간을 스파이크가 있던 구간으로 오인하지 않게 한다.
"""
from __future__ import annotations

import calendar
import json
import statistics
import time
from pathlib import Path

from . import comparisons as _comparisons
from . import cpu_source as _cpu_source

# 라벨별 vCPU와 비교 쌍은 `comparisons.yaml`에서 온다. 코드에 박아두면 다른
# 인스턴스로 쓸 때 라벨이 목록에 없어 런이 조용히 리포트에서 탈락한다.
# RDS API에 접근할 수 있으면 실제 ProcessorFeatures와 대조해 불일치를 경고한다
# (`rds_facts.vcpu_warnings`).
_CFG = _comparisons.load()

VCPU: dict[str, int] = _comparisons.vcpu_map(_CFG)

PAIRS = [
    {"scenario": p["name"], "title": p["name"], "profile": p.get("workload") or "",
     "on": p["a_label"], "off": p["b_label"]}
    for p in _comparisons.pairs(_CFG)
]

# 유효성 게이트. HIT_MIN 미만이면 "양쪽에 같은 TPS" 전제가 깨지고, ERR_MAX 이상이면
# 부하가 아니라 실패를 측정한 표본이다 (실패한 트랜잭션은 CPU를 덜 쓰면서
# 재연결 경로가 레이턴시를 부풀린다 — 어느 방향으로도 쓸 수 없다).
HIT_MIN = float(_CFG["gates"]["hit_min_pct"])
ERR_MAX = float(_CFG["gates"]["err_max_pct"])

# 정규화 지표가 어느 CPU%에서 나오는가: "max"(부하 구간의 피크 5초 지점) 또는
# "mean". 모듈 docstring 참조.
CPU_BASIS = _CFG["cpu"]["basis"]

# CPU 지표의 출처: rds_em / manual / none. none이면 CPU 없이 처리량·레이턴시만
# 비교하고, 리포트가 그 사실을 명시한다 (`cpu_source.describe`).
CPU_SOURCE = _CFG["cpu"]["source"]

_FMT = "%Y-%m-%dT%H:%M:%SZ"


def label_key(label: str) -> str:
    """런 라벨에서 설정에 등록된 인스턴스 키를 찾는다.

    `"scenario: inst-a"` 처럼 접두사를 붙여도 되고 `"inst-a"` 그대로도 된다.
    등록되지 않은 라벨은 그대로 돌려주고, 호출부에서 vCPU 미등록으로 처리한다.
    """
    for k in VCPU:
        if k in label:
            return k
    return label


def scenario_key(label: str, default: str = "") -> str:
    """`"scenario: inst-a"` -> `"scenario"`. 접두사가 없으면 default."""
    return label.split(":")[0].strip() if ":" in label else default


def _epoch(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return calendar.timegm(time.strptime(s, _FMT))
    except (ValueError, TypeError):
        return None


def steady_window(meta: dict, summary: dict) -> tuple[float, float] | None:
    """부하 구간을 (시작, 끝) epoch 초로. 시각 정보가 없으면 None.

    구간은 런의 **시계**로 정한다. 처리량 임계값으로 자르지 않는다. EM 스냅샷이
    런 앞뒤를 60초씩 감싸므로 유휴 구간을 잘라내야 하는 건 맞지만(실측: 부하
    구간 7.94% vs 스냅샷 전체 3.92%), 처리량으로 자르면 **가장 중요한 런에서
    조용히 실패한다**.

    실측 사례: `disk_wiops > max * 0.2` 규칙을 쓰던 때, 락 컨보이로 IOPS가
    3,945에서 60~400으로 주저앉은 런이 있었다. 임계값을 넘긴 것은 붕괴 직전
    2개 포인트뿐이어서 CPU가 25.2%로 보고됐지만 붕괴 구간의 실제 값은 4.2%였다 —
    6배 과대보고이고, "CPU가 한가하니 HT가 아니라 락 문제다"라는 증거를 뒤집었다.

    끝은 설정된 duration이 아니라 *실제* 종료 시각으로 자른다. 210초로 설정된
    런이 34초에 죽은 사례에서, 유휴 꼬리가 CPU를 14.1%에서 1.4%로 희석시켰다.
    표본이 얇은 것을 감추지 않도록 살아남은 포인트 수를 `em_n`에 남긴다.
    """
    cfg = meta.get("config", {})
    t0 = _epoch(meta.get("started_at_utc"))
    if t0 is None:
        return None
    t1 = t0 + cfg.get("warmup_sec", 0) + cfg.get("duration_sec", 0)
    ended = _epoch(summary.get("ended_at_utc"))
    if ended:
        t1 = min(t1, ended)
    t0 += cfg.get("warmup_sec", 0)          # exclude warmup
    return (t0, t1) if t1 > t0 else None


def _em_stats(pts: list[dict], window: tuple[float, float] | None) -> dict:
    use = [p for p in pts if window[0] <= p.get("ts", 0) <= window[1]] if window else []
    if not use:
        # Runs predating started_at_utc fall back to the old throughput cut.
        wmax = max(p.get("disk_wiops", 0) for p in pts)
        cmax = max(p.get("cpu_total", 0) for p in pts)
        if wmax > 50:
            use = [p for p in pts if p.get("disk_wiops", 0) > wmax * 0.2]
        elif cmax > 1:
            use = [p for p in pts if p.get("cpu_total", 0) > cmax * 0.2]
        use = use or pts
    mean = lambda k: statistics.mean(p.get(k, 0) for p in use)  # noqa: E731
    cpu_mean = mean("cpu_total")
    cpu_max = max(p.get("cpu_total", 0) for p in use)
    return {
        "em_n": len(use),
        # 반올림하지 않은 값 — 정규화 지표는 이걸 쓴다. 표시용 1자리 값에서
        # cpu_ms_txn을 유도하면 비율의 셋째 자리가 움직인다(실측 0.842 vs 0.839).
        # 표시 정밀도가 헤드라인 수치로 새어드는 것이다.
        "cpu_pct_exact": cpu_max if CPU_BASIS == "max" else cpu_mean,
        # cpu_pct는 리포트에 표시되는 CPU%. 두 기준을 모두 행에 남겨, 하나로
        # 뭉개지 않고 둘의 차이를 검산할 수 있게 한다.
        "cpu_pct": round(cpu_max if CPU_BASIS == "max" else cpu_mean, 1),
        "cpu_pct_mean": round(cpu_mean, 1),
        "cpu_max_pct": round(cpu_max, 1),
        "w_iops": round(mean("disk_wiops")),
        "w_iops_max": round(max(p.get("disk_wiops", 0) for p in use)),
        "r_iops": round(mean("disk_riops")),
        "sql_mem_gb": round(mean("sql_mem_gb"), 1),
    }


def run_row(d: Path) -> dict | None:
    """One row of normalized metrics for a run directory.

    Returns None when the directory is not a finished run. Rows that cannot be
    compared still come back, carrying `skip` with the reason — a run is never
    dropped without saying so.
    """
    meta_f, sum_f = d / "meta.json", d / "summary.json"
    if not (meta_f.exists() and sum_f.exists()):
        return None
    meta, summary = json.loads(meta_f.read_text()), json.loads(sum_f.read_text())
    cfg = meta.get("config", {})
    inst = label_key(meta.get("label", ""))
    tps = summary.get("avg_tps") or 0
    txns = summary.get("total_txns") or 0
    errors = summary.get("total_errors", 0)

    # 래더 실행기는 note에 "ladder <이름> L<tps>"를 쓴다. 같은 배치 태그의 런끼리만
    # 짝을 맺는다 (bucket() 참조).
    note = cfg.get("note", "")
    batch = note.rsplit(" L", 1)[0] if note.startswith("ladder ") else note

    row = {
        "run_id": d.name,
        "inst": inst,
        "label": meta.get("label", ""),
        "host": meta.get("host", ""),
        "db_id": (meta.get("host", "") or "").split(".")[0],
        "scenario": scenario_key(meta.get("label", "")),
        # 워크로드 이름. meta에 워크로드 스냅샷이 통째로 들어 있으므로 거기서 읽는다.
        "profile": (meta.get("workload") or {}).get("name", ""),
        "mode": cfg.get("mode", ""),
        "batch": batch,
        "note": note,
        "target_tps": cfg.get("target_tps"),
        "conns": cfg.get("processes", 0) * cfg.get("threads_per_process", 0),
        "processes": cfg.get("processes"),
        "threads_per_process": cfg.get("threads_per_process"),
        "read_pct": cfg.get("read_pct"),
        "duration_sec": cfg.get("duration_sec"),
        "warmup_sec": cfg.get("warmup_sec"),
        "scale": meta.get("scale"),
        "started_at_utc": meta.get("started_at_utc"),
        "ended_at_utc": summary.get("ended_at_utc"),
        "steady_seconds": summary.get("steady_seconds"),
        "vcpu": VCPU.get(inst),
        "tps": round(tps, 1),
        "total_txns": txns,
        "errors": errors,
        # 실패한 트랜잭션은 CPU를 덜 쓰면서 레이턴시는 부풀리므로 어느 방향으로도
        # 쓸 수 없다. 실측 사례: FK 위반 78,572건이 섞인 런의 p99가 11.5초였는데,
        # 아티팩트에 에러 메시지가 없어 성능 결과로 읽혔다.
        "err_pct": round(errors / txns * 100, 1) if txns else None,
        "em_n": 0,
    }

    # CPU 출처는 설정이 정한다 (rds_em / manual / none). none이면 여기서 아무것도
    # 붙지 않고, 리포트는 처리량·레이턴시 비교로 내려간다.
    pts = _cpu_source.points_for_run(d, CPU_SOURCE)
    if pts:
        row.update(_em_stats(pts, steady_window(meta, summary)))

    if cfg.get("target_tps"):
        row["hit_pct"] = round(tps / cfg["target_tps"] * 100, 1)
    if row.get("cpu_pct") is not None and row["vcpu"] and tps:
        cpu, vcpu = row["cpu_pct_exact"], row["vcpu"]
        row["cpu_vcpu"] = round(cpu / 100 * vcpu, 2)
        row["cpu_ms_txn"] = round(cpu / 100 * vcpu * 1000 / tps, 3)
        row["headroom_vcpu"] = round((100 - cpu) / 100 * vcpu, 2)

    per = summary.get("per_txn", {})
    if per:
        n = sum(v["count"] for v in per.values()) or 1
        row["p50_ms"] = round(sum(v["p50_ms"] * v["count"] for v in per.values()) / n, 1)
        row["p99_ms"] = round(sum(v["p99_ms"] * v["count"] for v in per.values()) / n, 1)
        row["per_txn"] = per

    samples = summary.get("err_samples") or {}
    if samples:
        top, top_n = max(samples.items(), key=lambda kv: kv[1])
        row["err_top"] = top
        row["err_top_n"] = top_n
        row["err_kinds"] = len(samples)

    if not row["vcpu"]:
        row["skip"] = f"vCPU 미등록 라벨 ({inst}) — HT 쌍 비교 대상 아님"
    elif not tps:
        row["skip"] = "avg_tps 0 — 측정 구간 없음"
    return row


def scan_runs(runs_dir: Path, mode: str | None = None,
              run_ids: list[str] | None = None) -> tuple[list[dict], list[dict]]:
    """(comparable rows, skipped rows) from a runs/ directory."""
    rows, skipped = [], []
    if not runs_dir.exists():
        return rows, skipped
    names = run_ids if run_ids is not None else sorted(
        d.name for d in runs_dir.iterdir() if d.is_dir() and not d.name.startswith("_"))
    for name in names:
        r = run_row(runs_dir / name)
        if not r:
            skipped.append({"run_id": name, "skip": "meta.json/summary.json 없음"})
            continue
        if mode and r["mode"] != mode:
            continue
        (skipped if r.get("skip") else rows).append(r)
    return rows, skipped


def bucket(row: dict) -> tuple:
    """서로 비교 가능한 런을 묶는 키.

    부하 설정만으로는 부족하다 — `note`에 담긴 실행 배치도 같아야 한다. 실측 사례:
    락 컨보이를 고치기 전후로 **같은 설정이 20배 다른 TPS**를 냈다. 배치를 무시하면
    코드 버전 차이를 인스턴스 차이로 읽게 된다.
    """
    return (row["profile"], row["mode"], row["target_tps"], row["conns"],
            row["read_pct"], row["batch"])


def row_reasons(r: dict) -> list[str]:
    """Why this single run fails the gates (empty if it passes)."""
    out = []
    hit = r.get("hit_pct")
    if hit is not None and hit < HIT_MIN:
        out.append(f"{r['inst']} 목표 미달 (hit {hit}% < {HIT_MIN:g}%) — "
                   f"'같은 TPS' 전제 붕괴")
    err = r.get("err_pct")
    if err is not None and err >= ERR_MAX:
        out.append(f"{r['inst']} 에러율 {err}% ≥ {ERR_MAX:g}% "
                   f"({r['errors']:,}건) — 실패 표본")
    return out


def level_validity(a: dict, b: dict) -> tuple[bool, list[str]]:
    """Is this level usable for HT comparison, and if not, why not."""
    reasons = row_reasons(a) + row_reasons(b)
    return (not reasons), reasons


_SIDE_KEYS = ("run_id", "inst", "label", "db_id", "vcpu", "tps", "hit_pct", "cpu_pct",
              "cpu_pct_mean", "cpu_max_pct", "cpu_vcpu", "cpu_ms_txn", "headroom_vcpu", "p50_ms",
              "p99_ms", "w_iops", "w_iops_max", "r_iops", "sql_mem_gb", "errors",
              "err_pct", "err_top", "err_top_n", "err_kinds", "em_n", "total_txns",
              "steady_seconds", "started_at_utc")


def _side(r: dict) -> dict:
    return {k: r.get(k) for k in _SIDE_KEYS}


def _bottleneck(r: dict, inst: dict | None) -> list[str]:
    """Evidence for why a side fell short, stated in measured numbers only."""
    ev = []
    w, prov = r.get("w_iops"), (inst or {}).get("iops")
    if w and prov:
        pct = w / prov * 100
        ev.append(f"WriteIOPS {w:,} / 프로비저닝 {prov:,} ({pct:.0f}%)"
                  + (" — 스토리지가 먼저 막았다" if pct >= 85 else ""))
    elif w:
        ev.append(f"WriteIOPS {w:,} (프로비저닝 값 확인 불가)")
    cpu, cvcpu = r.get("cpu_pct"), r.get("cpu_vcpu")
    if cpu is not None:
        tail = " — CPU가 아니다" if cpu < 60 else ""
        ev.append(f"같은 구간 CPU {cpu}%"
                  + (f" ({cvcpu} vCPU 소비)" if cvcpu is not None else "") + tail)
    if r.get("errors"):
        top = r.get("err_top", "")
        kind = ("deadlock 1205" if "1205" in top or "deadlock" in top.lower()
                else "FK 위반 547" if "547" in top else "")
        ev.append(f"에러 {r['errors']:,}건 ({r.get('err_pct')}%)"
                  + (f", 전량 {kind}" if kind else "")
                  + (f", 대표 메시지: {top[:120]}" if top else ""))
    if r.get("p99_ms"):
        ev.append(f"p99 {r['p99_ms']:,.1f}ms / p50 {r.get('p50_ms')}ms")
    return ev


def _per_txn_table(a: dict, b: dict) -> list[dict]:
    """Per-transaction latency for one level, shares taken from the HT-on side."""
    pa, pb = a.get("per_txn") or {}, b.get("per_txn") or {}
    common = [n for n in pa if n in pb]
    total = sum(pa[n]["count"] for n in common) or 1
    out = []
    for n in sorted(common, key=lambda n: -pa[n]["count"]):
        ma, mb = pa[n], pb[n]
        out.append({
            "name": n,
            "share_pct": round(ma["count"] / total * 100, 1),
            "on_p50": round(ma["p50_ms"], 2), "off_p50": round(mb["p50_ms"], 2),
            "on_p99": round(ma["p99_ms"], 2), "off_p99": round(mb["p99_ms"], 2),
            "on_count": ma["count"], "off_count": mb["count"],
            "errors": ma["errors"] + mb["errors"],
        })
    return out


def _distinct(rows: list[dict], key: str):
    vals = sorted({r.get(key) for r in rows if r.get(key) is not None}, key=str)
    return vals[0] if len(vals) == 1 else (vals or None)


def _symmetry(on: dict | None, off: dict | None) -> list[dict]:
    """Pair symmetry check — fields that must match for the comparison to hold."""
    if not (on and off):
        return []
    fields = [("storage_type", "스토리지 타입"), ("storage_gb", "스토리지 (GB)"),
              ("iops", "프로비저닝 IOPS"), ("storage_mbps", "스토리지 (MB/s)"),
              ("engine_version", "엔진 버전"), ("multi_az", "Multi-AZ")]
    out = []
    for k, ko in fields:
        a, b = on.get(k), off.get(k)
        if a is None and b is None:
            continue
        out.append({"field": ko, "on": a, "off": b, "same": a == b})
    return out


def build_report(rows: list[dict], skipped: list[dict] | None = None,
                 instances: dict | None = None,
                 discarded: list[dict] | None = None) -> dict:
    """The whole report as data. The UI and the markdown export both render this."""
    instances = instances or {}
    scenarios = []
    used: set[str] = set()

    for pair in PAIRS:
        on, off = pair["on"], pair["off"]
        # 쌍에 workload가 지정돼 있으면 그것으로 좁히고, 없으면 라벨만으로 묶는다.
        # 설정에 workload를 안 적었을 때 런이 전부 탈락하지 않게 하는 것이 요점이다.
        want = pair.get("profile")
        mine = [r for r in rows
                if r["inst"] in (on, off) and (not want or r["profile"] == want)]
        if not mine:
            continue
        groups: dict[tuple, dict[str, dict]] = {}
        superseded = []
        for r in sorted(mine, key=lambda r: r["run_id"]):
            slot = groups.setdefault(bucket(r), {})
            if r["inst"] in slot:      # same settings run twice -> keep the later
                superseded.append({"run_id": slot[r["inst"]]["run_id"],
                                   "inst": r["inst"], "target_tps": r["target_tps"],
                                   "kept": r["run_id"]})
            slot[r["inst"]] = r
        matched = {k: v for k, v in groups.items() if on in v and off in v}
        unpaired = [v[i] for k, v in groups.items() if k not in matched
                    for i in v]
        # An unpaired run can still be evidence, just not HT evidence: the p7
        # 11,000 level only has the m6i side because the ladder stopped when that
        # side missed the target, and *why* it missed (WriteIOPS 11,124 of 12,000
        # provisioned) is the ceiling of the rig. Listing it only as "no pair"
        # would silently drop the reason the ladder ends where it does.
        one_sided = []
        for r in sorted(unpaired, key=lambda r: (r["target_tps"] or 0, r["run_id"])):
            hit, err = r.get("hit_pct"), r.get("err_pct")
            if (hit is not None and hit < HIT_MIN) or (err is not None and err >= ERR_MAX):
                one_sided.append({
                    "target_tps": r["target_tps"], "batch": r["batch"],
                    "side": "on" if r["inst"] == on else "off",
                    "row": _side(r),
                    "reasons": row_reasons(r),
                    "evidence": _bottleneck(r, instances.get(r["db_id"])),
                })

        levels = []
        for k in sorted(matched, key=lambda x: (x[2] or 0, x[3])):
            a, b = matched[k][on], matched[k][off]
            used.update({a["run_id"], b["run_id"]})
            valid, reasons = level_validity(a, b)
            lv = {
                "target_tps": k[2], "conns": k[3], "read_pct": k[4],
                "valid": valid, "reasons": reasons,
                "on": _side(a), "off": _side(b),
                "cpums_ratio": (round(b["cpu_ms_txn"] / a["cpu_ms_txn"], 3)
                                if a.get("cpu_ms_txn") and b.get("cpu_ms_txn") else None),
                "vcpu_ratio": (round(b["cpu_vcpu"] / a["cpu_vcpu"], 3)
                               if a.get("cpu_vcpu") and b.get("cpu_vcpu") else None),
                "p99_ratio": (round(b["p99_ms"] / a["p99_ms"], 3)
                              if a.get("p99_ms") and b.get("p99_ms") else None),
            }
            if not valid:
                lv["evidence"] = {
                    on: _bottleneck(a, instances.get(a["db_id"])),
                    off: _bottleneck(b, instances.get(b["db_id"])),
                }
            levels.append(lv)

        valid_levels = [lv for lv in levels if lv["valid"]]
        ratios = [lv["cpums_ratio"] for lv in valid_levels if lv["cpums_ratio"]]
        top = valid_levels[-1] if valid_levels else None
        sc = {
            **{k: pair[k] for k in ("scenario", "title", "profile", "on", "off")},
            "scenario": _distinct(mine, "scenario") or pair["scenario"],
            "instances": {"on": instances.get(next((r["db_id"] for r in mine
                                                    if r["inst"] == on), "")),
                          "off": instances.get(next((r["db_id"] for r in mine
                                                     if r["inst"] == off), ""))},
            "targets": {s: next(({"label": r["label"], "db_id": r["db_id"],
                                  "vcpu": r["vcpu"]} for r in mine if r["inst"] == i), None)
                        for s, i in (("on", on), ("off", off))},
            "conditions": {k: _distinct(mine, k) for k in
                           ("mode", "read_pct", "conns", "processes",
                            "threads_per_process", "duration_sec", "warmup_sec")},
            "levels": levels,
            "valid_tps": [lv["target_tps"] for lv in valid_levels],
            "invalid_levels": [lv for lv in levels if not lv["valid"]],
            "superseded": superseded,
            "unpaired": [{"run_id": r["run_id"], "inst": r["inst"],
                          "target_tps": r["target_tps"],
                          "skip": "짝이 없다 (같은 설정·배치의 반대편 런 없음)"}
                         for r in unpaired],
            "one_sided": one_sided,
        }
        if ratios:
            sc["verdict"] = {
                "ratios": ratios,
                "max_ratio": max(ratios), "min_ratio": min(ratios),
                "all_below_one": all(x < 1.0 for x in ratios),
                "saving_pct_range": [round((1 - max(ratios)) * 100, 1),
                                     round((1 - min(ratios)) * 100, 1)],
                "min_hit_pct": min(x for lv in valid_levels for x in
                                   (lv["on"].get("hit_pct"), lv["off"].get("hit_pct"))
                                   if x is not None),
                "errors": sum((lv["on"].get("errors") or 0) + (lv["off"].get("errors") or 0)
                              for lv in valid_levels),
                "p99_ratios": [lv["p99_ratio"] for lv in valid_levels if lv["p99_ratio"]],
            }
        elif valid_levels:
            # CPU 지표가 없어도 처리량·레이턴시 비교는 유효한 결과다. 원본 도구는
            # 이 경우 리포트를 통째로 비워서, 정상적으로 돌아간 부하가 아무것도
            # 말하지 않는 것처럼 보였다.
            p99s = [lv["p99_ratio"] for lv in valid_levels if lv["p99_ratio"]]
            sc["verdict_no_cpu"] = {
                "levels": len(valid_levels),
                "p99_ratios": p99s,
                "p99_max": max(p99s) if p99s else None,
                "p99_min": min(p99s) if p99s else None,
                "min_hit_pct": min(x for lv in valid_levels for x in
                                   (lv["on"].get("hit_pct"), lv["off"].get("hit_pct"))
                                   if x is not None),
                "errors": sum((lv["on"].get("errors") or 0) + (lv["off"].get("errors") or 0)
                              for lv in valid_levels),
                "reason": "CPU 지표가 없어 트랜잭션당 CPU 비교는 못 했다 — "
                          "처리량과 레이턴시만 비교한다",
            }
        if top:
            # What actually capped the level, in the run's own numbers. Saying
            # only "CPU was 28%, so something else blocked it" leaves the reader
            # to guess; the peak write IOPS against provisioned IOPS names it.
            io = sc["instances"]
            iops_prov = ((io.get("off") or io.get("on")) or {}).get("iops")
            w_max = max((x for x in (top["on"].get("w_iops_max"),
                                     top["off"].get("w_iops_max")) if x), default=None)
            sc["ceiling"] = {
                "target_tps": top["target_tps"],
                "on_cpu_pct": top["on"].get("cpu_pct"), "off_cpu_pct": top["off"].get("cpu_pct"),
                "on_headroom": top["on"].get("headroom_vcpu"),
                "off_headroom": top["off"].get("headroom_vcpu"),
                "w_iops_max": w_max, "iops_prov": iops_prov,
                "w_iops_pct": round(w_max / iops_prov * 100) if w_max and iops_prov else None,
            }
            sc["per_txn"] = {"target_tps": top["target_tps"],
                             "rows": _per_txn_table(matched[
                                 next(k for k in sorted(matched, key=lambda x: (x[2] or 0, x[3]))
                                      if (k[2] or 0) == top["target_tps"])][on],
                                 matched[next(k for k in sorted(matched, key=lambda x: (x[2] or 0, x[3]))
                                              if (k[2] or 0) == top["target_tps"])][off])}
        sc["symmetry"] = _symmetry(sc["instances"]["on"], sc["instances"]["off"])
        scenarios.append(sc)

    report = {
        "generated_at_utc": time.strftime(_FMT, time.gmtime()),
        "gates": {"hit_min_pct": HIT_MIN, "err_max_pct": ERR_MAX},
        # CPU 출처를 리포트에 박아둔다. 지표가 없는 리포트를 "CPU가 낮았다"로
        # 읽는 것을 막는 것이 목적이다.
        "cpu": {
            "source": CPU_SOURCE, "basis": CPU_BASIS,
            "note": _cpu_source.describe(
                CPU_SOURCE, any(r.get("cpu_pct") is not None for r in rows)),
        },
        "run_count": len(rows),
        "scenarios": scenarios,
        "skipped": skipped or [],
        "discarded": discarded or [],
        "unused_runs": [{"run_id": r["run_id"], "inst": r["inst"], "mode": r["mode"],
                         "target_tps": r["target_tps"], "tps": r["tps"]}
                        for r in rows if r["run_id"] not in used],
        "limits": _limits(scenarios),
        "rows": rows,
    }
    return report


def _limits(scenarios: list[dict]) -> list[dict]:
    """측정 자체가 함의하는 한계 — 각 항목은 실제 수치에 근거한다.

    리포트가 답하지 못한 것을 리포트가 직접 말하게 하는 것이 목적이다.
    독자가 스스로 채워 넣게 두면 대개 유리한 쪽으로 해석된다.
    """
    out = []
    unsat = [s for s in scenarios if s.get("ceiling")
             and (s["ceiling"].get("off_cpu_pct") or 0) < 70]
    if unsat:
        detail = " · ".join(
            f"{s['title']} {s['ceiling']['target_tps']:,} TPS에서 CPU "
            f"{s['ceiling']['off_cpu_pct']}%" for s in unsat)
        # 무엇이 막았는지 이름을 댄다. "CPU가 한계가 아니었다"만 쓰면 독자가 답을
        # 상상하게 되므로, 행에 있는 피크 write IOPS와 프로비저닝 값을 밝힌다.
        io = " · ".join(
            f"{s['title']} 피크 WriteIOPS {s['ceiling']['w_iops_max']:,} / 프로비저닝 "
            f"{s['ceiling']['iops_prov']:,} ({s['ceiling']['w_iops_pct']}%)"
            for s in unsat if s["ceiling"].get("w_iops_pct"))
        body = f"유효 상한에서 vCPU가 적은 쪽의 CPU가 70% 미만이다 ({detail}). "
        if io:
            body += (f"먼저 막은 것은 스토리지다 — {io}. 쓰기 위주 워크로드에서는 "
                     f"트랜잭션당 디스크 write가 거의 1:1로 발생해, 실질 천장이 "
                     f"CPU가 아니라 프로비저닝된 IOPS가 된다. ")
        else:
            body += "CPU가 아닌 자원이 먼저 막았다는 뜻이다. "
        body += ("따라서 '적은 vCPU로 CPU 포화를 버티는가'는 이 측정으로 답하지 못한다.")
        if io:
            body += " 양쪽 스토리지 사양이 같다면 두 인스턴스의 비교 자체는 공정하다."
        out.append({"title": "CPU 포화는 검증하지 못했다", "body": body})

    hz = [s for s in scenarios if s.get("ceiling") and s["ceiling"].get("on_headroom")
          and s["ceiling"].get("off_headroom")]
    if hz:
        detail = " · ".join(
            f"{s['title']} {s['ceiling']['on_headroom']} → "
            f"{s['ceiling']['off_headroom']} vCPU" for s in hz)
        out.append({
            "title": "절대 헤드룸은 별개 문제다",
            "body": f"트랜잭션당 CPU가 줄었더라도 남은 vCPU 총량은 다르다 ({detail}). "
                    f"트랜잭션당 효율과 피크 흡수 여력은 다른 질문이며, 후자는 "
                    f"vCPU 수에 그대로 비례한다.",
        })

    gens = [s for s in scenarios if s.get("instances", {}).get("on")
            and s.get("instances", {}).get("off")]
    if gens:
        classes = {
            s["title"]: (
                (s["instances"]["on"] or {}).get("instance_class"),
                (s["instances"]["off"] or {}).get("instance_class"),
            ) for s in gens
        }
        differing = {k: v for k, v in classes.items() if v[0] and v[1] and v[0] != v[1]}
        if differing:
            detail = " · ".join(f"{k}: {a} vs {b}" for k, (a, b) in differing.items())
            out.append({
                "title": "인스턴스 세대 차이가 섞인 합성값이다",
                "body": f"비교 대상의 인스턴스 클래스가 다르다 ({detail}). 세대가 다르면 "
                        f"코어당 성능도 달라지므로, 측정값은 의도한 변수의 효과와 세대 "
                        f"차이가 합쳐진 값이다. 실제 전환 시나리오가 세대 변경을 포함한다면 "
                        f"이 합성값이 곧 답이지만, 단일 변수의 순효과로 인용하면 안 된다.",
            })

    reads = [s for s in scenarios for lv in s["levels"] if lv["valid"]
             and (lv["on"].get("r_iops") or 0) == 0 and (lv["off"].get("r_iops") or 0) == 0]
    if reads:
        out.append({
            "title": "읽기가 디스크에 닿지 않았다",
            "body": f"유효 레벨 {len(reads)}개 전부에서 `disk_riops`가 0이다. 읽기가 "
                    f"전량 버퍼 캐시에서 처리됐다는 뜻이다. 실제 워크로드의 캐시 적중률이 "
                    f"높다면 일치하는 결과지만, 시딩 행수가 적어 hot set이 메모리에 다 "
                    f"들어간 것일 수도 있다 — 시딩 플랜의 행수와 함께 봐야 한다.",
        })
    return out
