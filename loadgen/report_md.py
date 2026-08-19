"""분석 결과를 마크다운 리포트로 렌더링한다.

모든 수치는 `build_report()`에서 오고, 그것은 런 아티팩트에서 읽는다. 여기서
어떤 값도 문서에서 옮겨 적지 않는다 — 런이 없으면 없다고 쓰고, 기억한 값으로
채우지 않는다.
"""
from __future__ import annotations

from .analysis import ERR_MAX, HIT_MIN


def _n(v, dash: str = "–", fmt: str = "{:,}") -> str:
    if v is None:
        return dash
    if isinstance(v, float):
        return fmt.format(v) if fmt != "{:,}" else f"{v:,.1f}"
    return fmt.format(v) if isinstance(v, int) else str(v)


def _pair(a, b, fmt: str = "{:,.1f}") -> str:
    def one(v):
        return "–" if v is None else fmt.format(v)

    return f"{one(a)} / {one(b)}"


def _rng(lo, hi, fmt: str = "{:.1f}") -> str:
    """"a~b" 형태. 양 끝이 같으면 "a"로 줄인다.

    유효 레벨이 1개면 min == max가 되는데, "6.3~6.3% 덜 썼다"는 측정 결과가 아니라
    서식 버그처럼 읽힌다.
    """
    a, b = fmt.format(lo), fmt.format(hi)
    return a if a == b else f"{a}~{b}"


def _lv(n: int) -> str:
    """n == 1일 때 "유효 1레벨 모두"가 되는 것을 막는다."""
    return "유효 1레벨에서" if n == 1 else f"유효 {n}레벨 모두"


def _cond(sc: dict) -> str:
    c = sc["conditions"]
    bits = []
    if c.get("read_pct") is not None:
        bits.append(f"**read {c['read_pct']}% / write {100 - c['read_pct']}%**")
    if c.get("conns"):
        bits.append(f"{_n(c['conns'])} 커넥션 상한"
                    + (f" ({c.get('processes')} proc × {c.get('threads_per_process')} thr)"
                       if c.get("processes") else ""))
    if c.get("duration_sec"):
        bits.append(f"측정 {c['duration_sec']}초 + warmup {c.get('warmup_sec', 0)}초")
    if c.get("mode") == "open":
        bits.append("open-loop pacing (양쪽에 같은 목표 TPS)")
    return ", ".join(bits) if bits else "설정 정보 없음"


def _levels_table(sc: dict) -> list[str]:
    on, off = sc["on"], sc["off"]
    # 여기 CPU%는 구간 평균이 아니라 피크 5초 지점이다 (analysis.CPU_BASIS).
    # 평균 열을 나란히 남기는 이유: 없으면 평탄한 구간과 스파이크가 있던 구간이
    # 똑같이 보여서, 그 피크가 어디서 왔는지 독자가 판단할 수 없다.
    out = [
        f"| 목표 TPS | 달성 ({on} / {off}) | hit% | **CPU% 최대** | (평균) | 소비 vCPU | "
        f"**cpu_ms/txn** | 비율 | 잔여 vCPU | p99 (ms) | 유효 |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for lv in sc["levels"]:
        a, b = lv["on"], lv["off"]
        ratio = f"**{lv['cpums_ratio']}**" if lv["cpums_ratio"] else "–"
        out.append(
            f"| {_n(lv['target_tps'])} | {_pair(a.get('tps'), b.get('tps'))} | "
            f"{_pair(a.get('hit_pct'), b.get('hit_pct'))} | "
            f"{_pair(a.get('cpu_pct'), b.get('cpu_pct'))} | "
            f"{_pair(a.get('cpu_pct_mean'), b.get('cpu_pct_mean'))} | "
            f"{_pair(a.get('cpu_vcpu'), b.get('cpu_vcpu'), '{:,.2f}')} | "
            f"{_pair(a.get('cpu_ms_txn'), b.get('cpu_ms_txn'), '{:,.3f}')} | "
            f"{ratio} | "
            f"{_pair(a.get('headroom_vcpu'), b.get('headroom_vcpu'), '{:,.2f}')} | "
            f"{_pair(a.get('p99_ms'), b.get('p99_ms'))} | "
            f"{'y' if lv['valid'] else '**n**'} |")
    return out


def _instance_table(sc: dict) -> list[str]:
    ti, io = sc["targets"], sc["instances"]
    if not (ti.get("on") and ti.get("off")):
        return []
    rows = [("인스턴스 식별자", ti["on"]["db_id"], ti["off"]["db_id"])]
    fields = [("클래스", "instance_class"), ("메모리 (GB)", "memory_gb"),
              ("스토리지", "storage_desc"), ("엔진", "engine_desc")]
    for ko, k in fields:
        a = (io.get("on") or {}).get(k)
        b = (io.get("off") or {}).get(k)
        if a or b:
            rows.append((ko, a or "–", b or "–"))
    rows.append(("vCPU", f"**{ti['on']['vcpu']}**", f"**{ti['off']['vcpu']}**"))
    core = [((io.get("on") or {}).get("processor")), ((io.get("off") or {}).get("processor"))]
    if any(core):
        # ProcessorFeatures가 없으면 클래스 기본값이다 — 실측이 아니라는 점을 밝힌다.
        rows.append(("ProcessorFeatures", core[0] or "기본값 (미지정)",
                     core[1] or "기본값 (미지정)"))
    out = [f"| | {sc['on']} | {sc['off']} |", "|---|---|---|"]
    out += [f"| {k} | {a} | {b} |" for k, a, b in rows]
    return out


def render_markdown(report: dict) -> str:
    """리포트를 마크다운 문서로."""
    L: list[str] = []
    scs = report["scenarios"]
    valid_scs = [s for s in scs if s.get("verdict")]

    # 측정일은 시계가 아니라 런에서 읽는다. 몇 달 뒤에 이 문서를 다시 생성해도
    # 부하를 실제로 걸었던 날짜가 바뀌면 안 된다.
    days = sorted({(r.get("started_at_utc") or "")[:10]
                   for r in report.get("rows", []) if r.get("started_at_utc")})
    when = (f"측정일 **{days[0]}**" if len(days) == 1
            else f"측정일 **{days[0]} ~ {days[-1]}**" if days else "측정일 미기록")
    cpu = report.get("cpu") or {}
    L += ["# 부하 테스트 결과 — 인스턴스 비교", "",
          f"> {when} · 런 아티팩트 {report['run_count']}건에서 산출 "
          f"(생성 {report['generated_at_utc']})",
          "> 목적: **같은 워크로드를 같은 처리량으로 걸었을 때 두 인스턴스의 "
          "리소스 사용량이 어떻게 다른가**", ""]
    if cpu.get("note"):
        L += [f"> {cpu['note']}", ""]
    # UI는 이 경고를 배너로 보여준다. 마크다운에도 실어야 한다 — 다운로드한 문서가
    # vCPU 근거도 검증되지 않은 상태로 "전부 확인됨"처럼 보이면 안 된다.
    if report.get("warnings"):
        L += [f"> ⚠️ **{w}**" for w in report["warnings"]] + [""]
    if not scs:
        L += ["비교 가능한 런이 없다. `comparisons.yaml`에 정의한 쌍의 양쪽 런이 "
              "같은 설정으로 `runs/`에 있어야 한다.", ""]
        if report["skipped"]:
            L += ["건너뛴 런:", ""]
            L += [f"- `{s['run_id']}` — {s['skip']}" for s in report["skipped"]]
        return "\n".join(L) + "\n"

    has_cpu = bool(valid_scs)
    L += ["---", "", "## 1. 결론", ""]
    if has_cpu:
        L += ["| 비교 | A → B | 유효 레벨 | **트랜잭션당 CPU 비율** | 처리량 손실 | 에러 |",
              "|---|---|---|---|---|---|"]
    else:
        L += ["| 비교 | A → B | 유효 레벨 | **p99 비율** | 처리량 손실 | 에러 |",
              "|---|---|---|---|---|---|"]
    for s in scs:
        v = s.get("verdict") or s.get("verdict_no_cpu")
        ti = s["targets"]
        _side = lambda k: (  # noqa: E731
            f"{(s['instances'].get(k) or {}).get('instance_class', s[k])} "
            f"{ti[k]['vcpu'] if ti.get(k) else '?'} vCPU" if ti.get(k) else s[k])
        if s.get("verdict"):
            lvls = " / ".join(f"{t:,}" for t in s["valid_tps"]) + " TPS"
            metric = " / ".join(f"**{r}**" for r in v["ratios"])
        elif s.get("verdict_no_cpu"):
            lvls = f"{v['levels']}개"
            metric = (" / ".join(f"{r}" for r in v["p99_ratios"])
                      if v["p99_ratios"] else "–")
        else:
            lvls, metric = "**없음**", "–"
        if v:
            loss = ("없음" if v["min_hit_pct"] >= HIT_MIN else f"hit {v['min_hit_pct']}%")
            errs = f"{v['errors']:,}건"
        else:
            loss, errs = "판정 불가", "–"
        L.append(f"| **{s['title']}** | {_side('on')} → **{_side('off')}** | "
                 f"{lvls} | {metric} | {loss} | {errs} |")
    L.append("")

    if valid_scs:
        total = sum(len(s["verdict"]["ratios"]) for s in valid_scs)
        below = sum(1 for s in valid_scs for r in s["verdict"]["ratios"] if r < 1.0)
        lo = min(s["verdict"]["saving_pct_range"][0] for s in valid_scs)
        hi = max(s["verdict"]["saving_pct_range"][1] for s in valid_scs)
        if below == total:
            L += [f"**유효 레벨 {total}개 전부에서 비율이 1.0 미만** — B가 같은 일에 CPU를 "
                  f"{_rng(lo, hi)}% 덜 썼다.", ""]
        elif below == 0:
            L += [f"**유효 레벨 {total}개 전부에서 비율이 1.0 이상** — B가 같은 일에 CPU를 "
                  f"더 썼다.", ""]
        else:
            L += [f"유효 레벨 {total}개 중 {below}개에서 비율이 1.0 미만이다 "
                  f"(나머지 {total - below}개는 B가 CPU를 더 썼다). 레벨에 따라 방향이 "
                  f"갈리므로 단일 결론으로 요약할 수 없다.", ""]
        L += ["이 비율은 **\"같은 일에 드는 CPU\"**에 대한 답이다. **\"최대 얼마까지 "
              "버티나\"**는 별개 질문이고, 아래 §측정이 답하지 못한 것을 함께 볼 것.", ""]
    elif any(s.get("verdict_no_cpu") for s in scs):
        L += ["CPU 지표가 없어 트랜잭션당 CPU 효율은 판정하지 않았다. 아래 표의 처리량과 "
              "레이턴시는 그대로 유효하다.", ""]
    else:
        L += ["⚠️ **유효 레벨이 없다.** 아래 각 비교의 무효 사유를 먼저 해소해야 "
              "판정할 수 있다.", ""]

    # --- what this did not answer
    if report["limits"]:
        L += ["### 단, 이 측정이 답하지 못한 것", ""]
        cl = [s for s in scs if s.get("ceiling")]
        if cl:
            L += ["| 서비스 | 유효 상한 | 그 지점 CPU (B) | 피크 WriteIOPS / 프로비저닝 | 잔여 vCPU (A → B) |",
                  "|---|---|---|---|---|"]
            for s in cl:
                c = s["ceiling"]
                iops = ("–" if not c.get("w_iops_pct") else
                        f"{_n(c['w_iops_max'])} / {_n(c['iops_prov'])} "
                        f"(**{c['w_iops_pct']}%**)")
                L.append(f"| {s['title']} | {_n(c['target_tps'])} TPS | "
                         f"{_n(c['off_cpu_pct'])}% | {iops} | "
                         f"{_n(c['on_headroom'])} → **{_n(c['off_headroom'])}** |")
            L.append("")
        for lim in report["limits"]:
            L += [f"- **{lim['title']}** — {lim['body']}"]
        L.append("")

    # --- metric definitions
    #
    # Deliberately just definitions. An earlier version opened with a "raw CPU%
    # flips the conclusion / the denominator is halved" framing plus a two-row
    # before-after table. It was dropped: leading with the reassurance reads as
    # arguing the numbers into shape, and the analysis does not depend on it --
    # every table below is already normalized, so stating the units is enough.
    L += ["### 지표 정의", "",
          "**CPU는 부하 구간의 최댓값(EM 5초 포인트)을 쓴다.** 사이징을 정하는 건 피크이고, "
          "평평한 구간의 평균은 짧은 스파이크를 그냥 지운다 — 아래 표에 평균을 나란히 둔 것은 "
          "그 최댓값이 어떤 모양의 구간에서 나왔는지 같이 보이게 하기 위한 것이다.", "",
          "| 지표 | 정의 |", "|---|---|",
          "| `CPU% 최대` | 부하 구간 EM 5초 포인트 중 최댓값. **가용 vCPU 대비 비율**이므로 "
          "vCPU 수가 다른 인스턴스 사이에서는 같은 단위가 아니다 |",
          "| `소비 vCPU` | `CPU% 최대 / 100 × vCPU` — 피크에서 실제로 쓴 vCPU 수 |",
          "| **`cpu_ms/txn`** | `소비 vCPU × 1000 / TPS` — **트랜잭션 1건당 CPU 시간"
          "(공정 비교 지표)** |",
          "| `잔여 vCPU` | `(100 - CPU% 최대) / 100 × vCPU` — 피크에서도 남아 있던 여력 |",
          "| `(평균)` | 같은 구간의 CPU% 평균. 참고용 — 정규화 지표에는 쓰지 않는다 |",
          f"| `hit%` | `달성 TPS / 목표 TPS` — {HIT_MIN:g}% 미만이면 \"같은 TPS\" 전제 "
          f"붕괴 → 레벨 무효 |",
          f"| `err%` | `에러 / 총 트랜잭션` — {ERR_MAX:g}% 이상이면 무효 |", ""]

    # --- per-scenario sections
    for i, s in enumerate(scs, start=2):
        L += ["---", "", f"## {i}. {s['title']} ({s['scenario']})", "",
              f"### {i}.1 테스트 대상", ""]
        it = _instance_table(s)
        L += it + [""] if it else [f"A `{s['on']}` ↔ B `{s['off']}`", ""]
        L += [f"부하 조건: {_cond(s)}.", ""]
        if s["conditions"].get("scale") is not None:
            L += [f"부하 파라미터 id 범위 scale `{s['conditions']['scale']}` "
                  f"(시딩 scale과 별개 — 절대 TPS를 prod 용량으로 해석하면 안 된다).", ""]
        L += [f"### {i}.2 결과", ""] + _levels_table(s) + [""]

        v = s.get("verdict")
        if v:
            n = len(v["ratios"])
            L += [f"에러 {v['errors']:,}건 (유효 {n}레벨 합계), 목표 달성률 최저 "
                  f"{v['min_hit_pct']}%.", ""]
            if v["all_below_one"]:
                L += [f"**HT 비활성화가 성능을 깎지 않았다.**", "",
                      f"- `cpu_ms/txn`이 {_lv(n)} 1.0 미만 — 같은 일에 CPU를 "
                      f"**{_rng(*v['saving_pct_range'])}% 덜** 쓴다"]
            else:
                over = [r for r in v["ratios"] if r >= 1.0]
                L += [f"**유효 {n}레벨 중 {len(over)}개에서 HT-off가 CPU를 더 썼다** "
                      f"(비율 {', '.join(str(r) for r in over)}).", ""]
            if v["p99_ratios"]:
                pr = v["p99_ratios"]
                # The ratio alone oversells the tail. w1's 0.068 is not "15x better
                # tail latency" -- it is a single 111ms outlier on the HT-on side
                # against 7.6ms, and stating it as an improvement claims something
                # the measurement does not support. So print both absolutes, and
                # when the ratio is extreme say plainly that one side's tail made it.
                vl = [lv for lv in s["levels"] if lv["valid"] and lv["p99_ratio"]]
                det = " · ".join(
                    f"{_n(lv['target_tps'])} TPS {_n(lv['on'].get('p99_ms'))} → "
                    f"{_n(lv['off'].get('p99_ms'))}ms ({lv['p99_ratio']}배)" for lv in vl)
                if all(x < 1.0 for x in pr):
                    L += [f"- p99는 B가 더 낮다 ({det})"]
                elif max(pr) < 1.2:
                    L += [f"- p99는 대체로 동등하다 ({det})"]
                else:
                    L += [f"- p99는 일부 레벨에서 B가 높다 ({det})"]
                if any(x < 0.5 or x > 2.0 for x in pr):
                    L += ["- 다만 배율이 극단적인 레벨은 **한쪽의 꼬리 이상치**가 만든 "
                          "값이다. 체계적인 개선/악화로 읽지 말고 절대값으로 볼 것 — "
                          "p50은 위 트랜잭션별 표를 봐야 한다"]
            L += [f"- 목표 달성률 양쪽 {v['min_hit_pct']}% 이상 → 처리량 손실 없음", ""]

        pt = s.get("per_txn")
        if pt and pt["rows"]:
            L += [f"트랜잭션별 (유효 상한 {pt['target_tps']:,} TPS, p50 ms):", "",
                  f"| 트랜잭션 | 비중 | {s['on']} | {s['off']} |", "|---|---|---|---|"]
            for r in pt["rows"]:
                faster = r["off_p50"] < r["on_p50"]
                L.append(f"| `{r['name']}` | {r['share_pct']}% | {r['on_p50']} | "
                         f"{'**' if faster else ''}{r['off_p50']}{'**' if faster else ''} |")
            L.append("")

        if s["invalid_levels"] or s.get("one_sided"):
            L += [f"### {i}.3 상한 — 무효 레벨과 그 이유", ""]
            for os_ in s.get("one_sided", []):
                r = os_["row"]
                side = "A" if os_["side"] == "on" else "B"
                L += [f"**{_n(os_['target_tps'])} TPS — {side}({r['inst']}) 한쪽만 실행됐고, "
                      f"그 한쪽이 목표에 미달했다.**", "",
                      f"반대편 런이 없으므로 HT 비교에는 쓸 수 없다. 다만 이 레벨이 "
                      f"래더의 상한인 이유는 측정에 남아 있다:", ""]
                L += [f"- {x}" for x in os_["reasons"]]
                L += [f"- {x}" for x in os_["evidence"]]
                L += ["",
                      "| | 달성 TPS | hit% | p50 / p99 (ms) | 에러 | CPU% | 소비 vCPU |",
                      "|---|---|---|---|---|---|---|",
                      f"| {side} ({r['inst']}) | {_n(r.get('tps'))} | {_n(r.get('hit_pct'))} | "
                      f"{_n(r.get('p50_ms'))} / {_n(r.get('p99_ms'))} | {_n(r.get('errors'))} | "
                      f"{_n(r.get('cpu_pct'))} | {_n(r.get('cpu_vcpu'))} |",
                      f"| 반대편 | — 런 없음 | – | – | – | – | – |", ""]
            for lv in s["invalid_levels"]:
                L += [f"**{_n(lv['target_tps'])} TPS는 무효다.**", ""]
                L += [f"- {r}" for r in lv["reasons"]]
                L.append("")
                L += ["| | 달성 TPS | hit% | p50 / p99 (ms) | 에러 | CPU% | 소비 vCPU |",
                      "|---|---|---|---|---|---|---|"]
                for side, key in (("A", "on"), ("B", "off")):
                    r = lv[key]
                    L.append(f"| {side} ({r['inst']}) | {_n(r.get('tps'))} | "
                             f"{_n(r.get('hit_pct'))} | "
                             f"{_n(r.get('p50_ms'))} / {_n(r.get('p99_ms'))} | "
                             f"{_n(r.get('errors'))} | {_n(r.get('cpu_pct'))} | "
                             f"{_n(r.get('cpu_vcpu'))} |")
                L.append("")
                ev = lv.get("evidence") or {}
                for inst, items in ev.items():
                    if items:
                        L += [f"`{inst}` 측정 근거:", ""]
                        L += [f"- {x}" for x in items]
                        L.append("")
                L += ["미달·실패한 쪽은 CPU가 낮게 찍혀 오히려 유리해 보이므로 이 레벨은 "
                      "비교에서 제외했다.", ""]

    # --- measurement reliability
    n = len(scs) + 2
    L += ["---", "", f"## {n}. 측정 신뢰성", ""]
    L += [f"### {n}.1 유효성 판정 규칙", "",
          f"각 레벨은 자동으로 검사한다 — 어느 한쪽이라도 `hit% < {HIT_MIN:g}` 또는 "
          f"`err% ≥ {ERR_MAX:g}`이면 그 레벨은 `유효 = n`이고 결론에서 제외된다. "
          f"실패 트랜잭션은 CPU를 덜 쓰면서 커넥션 재수립으로 레이턴시만 올리므로 "
          f"어느 방향으로도 HT 비교에 쓸 수 없다.", "",
          "steady 구간은 EM 5초 스냅샷에서 **런의 시각**으로 자른다 "
          "(`started_at_utc + warmup` ~ `min(설정 종료, ended_at_utc)`). 처리량 지표를 "
          "문턱으로 쓰면 부하가 무너진 런에서 조용히 틀린다 — 붕괴로 IOPS가 급락하면 "
          "문턱을 넘긴 포인트만 남아 CPU가 과대평가된다. 아래 `EM 표본` 열이 창에 남은 "
          "포인트 수이므로 표본이 얇아지면 드러난다.", ""]

    L += ["| 런 | 인스턴스 | 목표 | 달성 | hit% | CPU% | EM 표본 | 측정 구간 |",
          "|---|---|---|---|---|---|---|---|"]
    for s in scs:
        for lv in s["levels"]:
            for key in ("on", "off"):
                r = lv[key]
                L.append(f"| `{r['run_id']}` | {r['inst']} | {_n(lv['target_tps'])} | "
                         f"{_n(r.get('tps'))} | {_n(r.get('hit_pct'))} | "
                         f"{_n(r.get('cpu_pct'))} | {_n(r.get('em_n'))} | "
                         f"{r.get('steady_seconds', '–')}s |")
    L.append("")

    sym = [(s, s["symmetry"]) for s in scs if s.get("symmetry")]
    if sym:
        L += [f"### {n}.2 쌍 대칭성", ""]
        for s, items in sym:
            bad = [x for x in items if not x["same"]]
            head = "✅ 일치" if not bad else f"⚠️ **{len(bad)}개 불일치**"
            L += [f"**{s['title']}** — {head}", ""]
            L += ["| 항목 | A | B | |", "|---|---|---|---|"]
            for x in items:
                L.append(f"| {x['field']} | {x['on']} | {x['off']} | "
                         f"{'✓' if x['same'] else '**✗**'} |")
            L.append("")

    documented = {os_["row"]["run_id"] for s in scs for os_ in s.get("one_sided", [])}
    dropped = (report["skipped"] + report["discarded"]
               + [{**u, "skip": u["skip"] + (" — 상한 근거로 §상한에 기록"
                                             if u["run_id"] in documented else "")}
                  for s in scs for u in s["unpaired"]]
               + [{"run_id": x["run_id"], "skip": f"같은 설정의 후속 런 `{x['kept']}`로 대체"}
                  for s in scs for x in s["superseded"]]
               + [{"run_id": u["run_id"], "skip": f"HT 쌍 비교에 안 쓰임 ({u['mode']} 모드)"}
                  for u in report["unused_runs"]])
    if dropped:
        L += [f"### {n}.3 집계에서 제외된 런 (숨기지 않고 기록)", "",
              "| 런 | 제외 사유 |", "|---|---|"]
        # A run excluded for a specific reason (unpaired, quarantined) is also in
        # unused_runs; the first, more specific reason is the one to print.
        seen: set[str] = set()
        for d in dropped:
            if d["run_id"] in seen:
                continue
            seen.add(d["run_id"])
            L.append(f"| `{d['run_id']}` | {d.get('skip', '–')} |")
        L.append("")

    L += ["---", "", f"## {n + 1}. 원자료", "", "| | |", "|---|---|"]
    for s in scs:
        for lv in s["levels"]:
            for key in ("on", "off"):
                r = lv[key]
                L.append(f"| {r['inst']} @ {_n(lv['target_tps'])} TPS | "
                         f"`runs/{r['run_id']}/` (meta·summary·timeseries·rds_metrics) |")
    ids = " ".join(f"{lv[k]['run_id']}" for s in scs for lv in s["levels"]
                   for k in ("on", "off"))
    L += ["| 측정 조건 정의 | `loadgen/presets.py` (단일 정보원) |",
          "| 비교 쌍·vCPU·게이트 | `comparisons.yaml` |",
          "| 사용한 워크로드 | 각 런의 `meta.json` 안에 스냅샷으로 들어 있다 |",
          "| CPU 지표 | 런 폴더의 `rds_metrics.json` (RDS Enhanced Monitoring) 또는 "
          "`cpu.json` (직접 입력). EM 원본은 30일 후 만료되므로 런마다 스냅샷을 남긴다 |",
          "| 비교표 재산출 | `python tools/iso_tps_compare.py --mode open --csv out.csv` |",
          # --run-ids 없이 --mode만 쓰면 runs/ 전체에서 배치를 넘어 짝을 맺어,
          # 과거 런이 조용히 표에 다시 들어온다.
          f"| 이 문서 재생성 | `python tools/iso_tps_compare.py --mode open "
          f"--run-ids {ids} --md out.md` |", "",
          "> 재생성은 의존성이 설치된 환경에서 해야 한다. boto3가 없으면 RDS 조회가 "
          "실패하고 인스턴스 표가 조용히 추정값으로 내려앉는다 (그때는 위 경고 배너가 "
          "함께 뜬다).", ""]
    return "\n".join(L) + "\n"
