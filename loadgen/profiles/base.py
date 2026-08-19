"""트랜잭션 믹스 모델.

Txn은 작업 한 단위다 — 하나의 커넥션에서 실행되는 SQL 한 개 이상(다중 문장
쓰기는 명시적 트랜잭션으로 감싼다). `param_fn(g, ctx)`는 문장 수만큼의 파라미터
튜플을 돌려주고, `ctx`는 대상 DB에서 읽은 id 범위다 (`schema.ranges.id_ranges`).

이 모듈에는 특정 스키마에 대한 지식이 없다. 실제 Txn 목록은
`loadgen.workload.store.build_mix()`가 저장된 워크로드 JSON에서 조립한다.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Callable, Literal, Sequence

from ..seed.datagen import Gen


@dataclass(frozen=True)
class Txn:
    name: str
    kind: Literal["read", "write"]
    weight: int
    database: str
    sql: Sequence[str]
    param_fn: Callable[[Gen, dict], Sequence[tuple]]
    explicit_tran: bool = False  # wrap multi-statement writes in BEGIN/COMMIT


class Mix:
    def __init__(self, name: str, txns: Sequence[Txn]):
        self.name = name
        self.reads = [t for t in txns if t.kind == "read"]
        self.writes = [t for t in txns if t.kind == "write"]
        self._rw = {
            "read": ([t.weight for t in self.reads], self.reads),
            "write": ([t.weight for t in self.writes], self.writes),
        }

    def pick(self, rng: random.Random, read_pct: int) -> Txn:
        kind = "read" if rng.uniform(0, 100) < read_pct else "write"
        weights, txns = self._rw[kind]
        if not txns:
            # 한쪽만 있는 워크로드(읽기 전용 등)에서는 반대쪽이 비어 있다.
            # 그대로 두면 rng.choices가 IndexError를 낸다.
            kind = "write" if kind == "read" else "read"
            weights, txns = self._rw[kind]
            if not txns:
                raise ValueError("워크로드에 실행할 트랜잭션이 없다")
        return rng.choices(txns, weights=weights, k=1)[0]
