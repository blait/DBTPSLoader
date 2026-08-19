"""워크로드 JSON의 저장·로드와 Mix 조립.

워크로드는 파일에 남는다. 사용자가 UI에서 SQL과 가중치를 고칠 수 있으므로,
실행된 워크로드를 그대로 보존하지 않으면 나중에 무엇을 측정했는지 재현되지 않는다.

파라미터는 함수가 아니라 **선언적 명세**로 적는다. JSON에 함수를 담을 수 없고,
워커 프로세스는 spawn으로 뜨기 때문에 부모의 클로저를 물려받지도 못한다.
`build_mix()`가 명세를 읽어 실행 시점에 생성 함수로 조립한다.

명세 형태:

    {"name": "my-workload",
     "txns": [
       {"name": "account_by_pk", "kind": "read", "weight": 30,
        "database": "Sales", "tables": ["Account"],
        "sql": ["SELECT Id, Email FROM dbo.Account WHERE Id = ?"],
        "params": [[{"gen": "skewed_id", "of": "account"}]]},
       ...
     ]}

`params`는 [문장][파라미터] 2차원이다 — 문장 하나당 파라미터 튜플 하나.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

from ..profiles.base import Mix, Txn
from ..seed.datagen import Gen

WORKLOAD_DIR = Path(__file__).resolve().parent.parent.parent / "workloads"


# --------------------------------------------------------------------- 값 생성

def _value(spec: dict, g: Gen, ctx: dict):
    """파라미터 명세 하나를 실제 값으로.

    `skewed_id`와 `uniform_id`의 구분이 이 함수의 핵심이다. 읽기는 핫 로우가
    생기도록 편중시켜야 실제 워크로드를 닮지만, UPDATE 대상에 같은 분포를 쓰면
    모든 커넥션이 같은 행을 잠그려 들어 락 컨보이가 생긴다 — 서버는 한가한데
    처리량이 평탄해지고, 원인이 부하기 쪽에 있다는 게 지표에 드러나지 않는다.
    """
    kind = spec.get("gen", "int")

    if kind in ("skewed_id", "uniform_id"):
        max_id = ctx.get(spec.get("of", "").lower(), 0)
        if max_id < 1:
            # 범위를 모르면 1을 쓴다. 조용히 0이나 음수를 내보내면 조회가
            # 0행을 돌려주고도 성공으로 집계된다.
            return 1
        if kind == "uniform_id":
            return g.uniform_id(max_id)
        return g.skewed_id(max_id, skew=spec.get("skew", 4.0))

    if kind == "int":
        return g.i(spec.get("min", 1), spec.get("max", 1000))
    if kind == "decimal":
        return g.dec(spec.get("min", 0), spec.get("max", 10000), spec.get("q", 2))
    if kind == "text":
        return g.text(spec.get("bytes", 80))
    if kind == "token":
        return g.token(spec.get("len", 16))
    if kind == "email":
        return g.email(g.i(1, spec.get("max", 10**6)))
    if kind == "name":
        return g.name()
    if kind == "word":
        return g.word()
    if kind == "datetime":
        return g.dt()
    if kind == "bit":
        return g.bit(spec.get("true_pct", 50))
    if kind == "ip":
        return g.ip()
    if kind == "uuid":
        return str(uuid.UUID(int=g.rng.getrandbits(128)))
    if kind == "const":
        return spec.get("value")
    raise ValueError(f"알 수 없는 파라미터 생성기: {kind}")


def _param_fn(param_specs: list[list[dict]]):
    """[문장][파라미터] 명세 → param_fn(g, ctx) -> [튜플, ...]"""
    def fn(g: Gen, ctx: dict):
        return [tuple(_value(s, g, ctx) for s in stmt) for stmt in param_specs]
    return fn


# ------------------------------------------------------------------ Mix 조립

def build_mix(workload: dict) -> Mix:
    """워크로드 dict → Mix. 워커 프로세스에서 호출된다."""
    txns = []
    for t in workload.get("txns", []):
        if t.get("disabled"):
            continue
        sql = t["sql"]
        params = t.get("params") or [[] for _ in sql]
        if len(params) != len(sql):
            raise ValueError(
                f"{t['name']}: SQL {len(sql)}개인데 파라미터 명세는 {len(params)}개다")
        txns.append(Txn(
            name=t["name"],
            kind=t["kind"],
            weight=int(t.get("weight", 1)),
            database=t["database"],
            sql=sql,
            param_fn=_param_fn(params),
            # 문장이 2개 이상인 쓰기는 원자적이어야 한다.
            explicit_tran=t.get("explicit_tran", t["kind"] == "write" and len(sql) > 1),
        ))
    if not txns:
        raise ValueError("워크로드에 활성 트랜잭션이 없다")
    return Mix(workload.get("name", "workload"), txns)


# --------------------------------------------------------------------- 영속화

def workload_path(name: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in name)
    return WORKLOAD_DIR / f"{safe}.json"


def save(workload: dict) -> Path:
    WORKLOAD_DIR.mkdir(parents=True, exist_ok=True)
    p = workload_path(workload["name"])
    p.write_text(json.dumps(workload, indent=2, ensure_ascii=False, default=str))
    return p


def load(name: str) -> dict:
    p = workload_path(name)
    if not p.exists():
        raise FileNotFoundError(f"워크로드를 찾을 수 없다: {name}")
    return json.loads(p.read_text())


def list_names() -> list[str]:
    if not WORKLOAD_DIR.exists():
        return []
    return sorted(p.stem for p in WORKLOAD_DIR.glob("*.json"))
