"""비교 쌍 설정 — `comparisons.yaml`을 읽는다.

원본에서는 비교 쌍과 vCPU가 코드에 하드코딩돼 있었다(`analysis.VCPU`,
`analysis.PAIRS`). 라벨이 그 목록에 없으면 런이 조용히 리포트에서 탈락하므로,
다른 인스턴스로 쓰려면 코드를 고쳐야 했다. 설정으로 옮긴다.

vCPU를 반드시 적어야 하는 이유: CPU%는 *가용 vCPU에 대한 비율*이라 32 vCPU와
16 vCPU에서 같은 단위가 아니다. 정규화하지 않고 원값을 비교하면 결론이 뒤집힌다.
"""
from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

CONFIG_NAMES = ("comparisons.local.yaml", "comparisons.yaml")
ROOT = Path(__file__).resolve().parent.parent

# 설정이 없을 때 쓰는 값. 쌍이 비어 있으면 리포트는 쌍 비교 없이
# 런별 처리량·레이턴시만 낸다.
DEFAULTS: dict = {
    "pairs": [],
    "gates": {"hit_min_pct": 95.0, "err_max_pct": 1.0},
    "cpu": {"source": "rds_em", "basis": "max"},
}


def _find() -> Path | None:
    for name in CONFIG_NAMES:
        p = ROOT / name
        if p.exists():
            return p
    return None


def load() -> dict:
    """설정을 읽는다. 파일이 없거나 PyYAML이 없으면 기본값."""
    p = _find()
    if p is None:
        return dict(DEFAULTS)
    try:
        import yaml
    except ImportError:
        log.warning("PyYAML이 없어 %s를 읽지 못했다 — 기본값으로 진행한다", p.name)
        return dict(DEFAULTS)
    try:
        raw = yaml.safe_load(p.read_text()) or {}
    except Exception as exc:  # noqa: BLE001
        log.warning("%s 파싱 실패: %s — 기본값으로 진행한다", p.name, exc)
        return dict(DEFAULTS)
    return {
        "pairs": raw.get("pairs") or [],
        "gates": {**DEFAULTS["gates"], **(raw.get("gates") or {})},
        "cpu": {**DEFAULTS["cpu"], **(raw.get("cpu") or {})},
        "_path": str(p),
    }


def vcpu_map(cfg: dict | None = None) -> dict[str, int]:
    """{라벨: vCPU} — 설정된 모든 쌍의 양쪽을 모은다."""
    cfg = cfg or load()
    out: dict[str, int] = {}
    for pair in cfg["pairs"]:
        for side in ("a", "b"):
            s = pair.get(side) or {}
            if s.get("label") and s.get("vcpu"):
                out[s["label"]] = int(s["vcpu"])
    return out


def pairs(cfg: dict | None = None) -> list[dict]:
    """정규화된 쌍 목록."""
    cfg = cfg or load()
    out = []
    for i, p in enumerate(cfg["pairs"]):
        a, b = p.get("a") or {}, p.get("b") or {}
        if not (a.get("label") and b.get("label")):
            log.warning("쌍 #%d에 label이 없어 건너뜀", i)
            continue
        out.append({
            "name": p.get("name") or f"{a['label']} vs {b['label']}",
            "a_label": a["label"], "b_label": b["label"],
            "a_vcpu": a.get("vcpu"), "b_vcpu": b.get("vcpu"),
            "workload": p.get("workload"),
            "tps": p.get("tps") or [],
            "note": p.get("note", ""),
        })
    return out
