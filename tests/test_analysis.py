"""분석 계층의 순수 함수 테스트 — DB·AWS 불필요.

`analysis.py`는 리포트 신뢰의 근거인데, 여기서 집계 버그가 나면 표 전체가 조용히
틀린다. 실제로 그런 일이 있었다: 부하 구간을 처리량 임계값으로 자르던 규칙이
락 컨보이 런에서 CPU를 6배 과대보고했고(25.2% vs 실제 4.2%), 그 결과가 "CPU가
한가하니 락 문제다"라는 증거를 뒤집었다. 그래서 구간 선택과 게이트 판정을
고정해 둔다.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loadgen.analysis import (  # noqa: E402
    ERR_MAX, HIT_MIN, bucket, label_key, row_reasons, scenario_key, steady_window,
)


# ------------------------------------------------------------------ 라벨 파싱

def test_label_key_prefixed():
    # "scenario: inst" 형태에서 등록된 인스턴스 키를 뽑는다
    import loadgen.analysis as A
    A.VCPU["inst-a"] = 32
    assert label_key("p7: inst-a") == "inst-a"
    assert label_key("inst-a") == "inst-a"


def test_label_key_unknown_passes_through():
    # 등록되지 않은 라벨은 그대로 — 호출부가 "vCPU 미등록"으로 처리한다
    assert label_key("전혀-모르는-라벨") == "전혀-모르는-라벨"


def test_scenario_key():
    assert scenario_key("p7: inst-a") == "p7"
    assert scenario_key("inst-a") == ""
    assert scenario_key("inst-a", default="none") == "none"


# ------------------------------------------------------------------ 부하 구간

def _meta(warmup=30, duration=180, started="2026-01-01T00:00:00Z"):
    return {"started_at_utc": started,
            "config": {"warmup_sec": warmup, "duration_sec": duration}}


def test_steady_window_excludes_warmup():
    w = steady_window(_meta(warmup=30, duration=180), {})
    assert w is not None
    start, end = w
    # 시작은 warmup 이후, 길이는 duration
    assert end - start == 180


def test_steady_window_clipped_by_actual_end():
    # 설정은 210초인데 34초에 죽은 런. 유휴 꼬리를 포함하면 CPU가 희석된다.
    meta = _meta(warmup=30, duration=180)
    summary = {"ended_at_utc": "2026-01-01T00:01:00Z"}   # 시작 후 60초
    w = steady_window(meta, summary)
    assert w is not None
    start, end = w
    assert end - start == 30     # warmup 30초 이후 ~ 60초 = 30초만 남는다


def test_steady_window_none_when_untimed():
    assert steady_window({"config": {}}, {}) is None


def test_steady_window_none_when_end_before_start():
    # 종료가 warmup보다 이르면 유효한 구간이 없다
    meta = _meta(warmup=30, duration=180)
    summary = {"ended_at_utc": "2026-01-01T00:00:10Z"}   # warmup 도중 종료
    assert steady_window(meta, summary) is None


# --------------------------------------------------------------- 유효성 게이트

def test_row_reasons_passes_clean_run():
    assert row_reasons({"inst": "a", "hit_pct": 99.9, "err_pct": 0.0, "errors": 0}) == []


def test_row_reasons_flags_missed_target():
    # 목표 미달은 "같은 TPS" 전제를 깨뜨린다. 미달한 쪽은 CPU가 낮게 찍혀
    # 오히려 유리해 보이므로 반드시 걸러야 한다.
    r = row_reasons({"inst": "a", "hit_pct": HIT_MIN - 1, "err_pct": 0.0, "errors": 0})
    assert len(r) == 1 and "목표 미달" in r[0]


def test_row_reasons_flags_error_rate():
    r = row_reasons({"inst": "a", "hit_pct": 99.0,
                     "err_pct": ERR_MAX, "errors": 1234})
    assert len(r) == 1 and "에러율" in r[0]


def test_row_reasons_flags_both():
    r = row_reasons({"inst": "a", "hit_pct": 50.0, "err_pct": 5.0, "errors": 99})
    assert len(r) == 2


def test_row_reasons_ignores_missing_fields():
    # closed 모드에는 hit_pct가 없다 — 없는 값으로 탈락시키면 안 된다
    assert row_reasons({"inst": "a"}) == []


# ------------------------------------------------------------------- 짝 맺기

def _row(**kw):
    base = {"profile": "w", "mode": "open", "target_tps": 1000, "conns": 128,
            "read_pct": 50, "batch": "ladder x"}
    base.update(kw)
    return base


def test_bucket_same_settings_match():
    assert bucket(_row()) == bucket(_row())


def test_bucket_differs_on_batch():
    # 같은 설정이라도 실행 배치가 다르면 짝이 아니다. 코드 버전이 바뀌면
    # 같은 설정에서 처리량이 크게 달라질 수 있고, 그것을 인스턴스 차이로
    # 읽으면 결론이 틀린다.
    assert bucket(_row(batch="ladder x")) != bucket(_row(batch="ladder y"))


def test_bucket_differs_on_load_settings():
    assert bucket(_row(target_tps=1000)) != bucket(_row(target_tps=2000))
    assert bucket(_row(conns=128)) != bucket(_row(conns=256))
    assert bucket(_row(read_pct=50)) != bucket(_row(read_pct=9))
