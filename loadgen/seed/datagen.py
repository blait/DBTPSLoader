"""Synthetic value generators for seeding.

Deterministic-ish (seeded per table) so re-runs produce comparable datasets
across the r6i and r7i instances — important for a fair comparison.
"""
from __future__ import annotations

import random
import string
from datetime import datetime, timedelta

EPOCH_START = datetime(2021, 1, 1)
EPOCH_END = datetime(2026, 7, 1)
_SPAN_SEC = int((EPOCH_END - EPOCH_START).total_seconds())

_WORDS = (
    "alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima "
    "mike november oscar papa quebec romeo sierra tango uniform victor whiskey "
    "xray yankee zulu poker stake event flight player member table seat chip"
).split()



class Gen:
    """Per-table generator with its own RNG stream."""

    def __init__(self, seed: int):
        self.rng = random.Random(seed)

    def dt(self, start: datetime = EPOCH_START) -> datetime:
        return start + timedelta(seconds=self.rng.randrange(_SPAN_SEC))

    def dt_seq(self, i: int, total: int) -> datetime:
        """Monotonic timestamps spread over the epoch — matches append-only tables."""
        return EPOCH_START + timedelta(seconds=int(_SPAN_SEC * (i / max(total, 1))))

    def email(self, i: int) -> str:
        return f"user{i}@loadtest.example.com"

    def name(self) -> str:
        return self.rng.choice(_WORDS).capitalize() + " " + self.rng.choice(_WORDS).capitalize()

    def word(self) -> str:
        return self.rng.choice(_WORDS)

    def text(self, nbytes: int) -> str:
        """~nbytes of nvarchar payload (2 bytes/char in SQL Server)."""
        nchars = max(1, nbytes // 2)
        out = []
        n = 0
        while n < nchars:
            w = self.rng.choice(_WORDS)
            out.append(w)
            n += len(w) + 1
        return " ".join(out)[:nchars]

    def token(self, n: int) -> str:
        return "".join(self.rng.choices(string.ascii_lowercase + string.digits, k=n))

    def dec(self, lo: float = 0, hi: float = 10000, q: int = 2) -> float:
        return round(self.rng.uniform(lo, hi), q)

    def pct(self) -> float:
        return round(self.rng.uniform(0, 100), 8)

    def i(self, lo: int, hi: int) -> int:
        return self.rng.randint(lo, hi)

    def bit(self, true_pct: float = 50) -> bool:
        return self.rng.uniform(0, 100) < true_pct

    def skewed_id(self, max_id: int, skew: float = 4.0) -> int:
        """[1, max_id]에서 편중된 id — 핫 로우에 트래픽이 몰리게 한다.

        테이블 크기에 맞춰 **스케일된다**. [0,1) 난수를 거듭제곱해 0 근처로 밀고
        max_id를 곱하므로, 100행이든 1억 행이든 "하위 몇 %가 트래픽의 몇 %를
        받는가"가 일정하게 유지된다. `skew`가 클수록 뾰족하다 (1.0 = 균등,
        기본 4.0 ≈ 하위 10%가 트래픽의 절반).

        구현 주의 — `int(paretovariate())`를 그대로 쓰고 max_id 초과분만 버리는
        방식은 쓰면 안 된다. 테이블 크기와 무관하게 언제나 같은 수백 개 id만
        나오므로, 읽기 워킹셋이 몇백 행에 갇혀 전부 버퍼 캐시에서 처리되고
        인덱스도 디스크도 실제로 시험하지 못한다.
        """
        if max_id < 1:
            return 1
        return int(self.rng.random() ** skew * max_id) + 1

    def uniform_id(self, max_id: int) -> int:
        """[1, max_id] 균등 — UPDATE 대상용. 락 컨보이를 피한다.

        편중 분포를 UPDATE 대상에 쓰면 모든 커넥션이 같은 소수의 행을 잠그려
        들어 직렬화된다. 실측 사례: 512 커넥션에서 쓰기 p50이 1.6초인데 서버
        CPU는 3%였고 대기 통계는 LCK_M_U / LCK_M_S가 지배했다. 96 커넥션
        이상에서는 처리량이 아예 평탄해졌다 — 서버가 아니라 부하기가 만든
        병목이라 지표만 보면 원인을 알기 어렵다.

        UPDATE는 어느 행에 걸어도 같은 일이므로, 대상을 흩뿌리면 읽기가 의존하는
        데이터 분포를 건드리지 않고 컨보이만 없앨 수 있다.
        """
        if max_id < 1:
            return 1
        return self.rng.randint(1, max_id)

    def ip(self) -> str:
        return ".".join(str(self.rng.randint(1, 254)) for _ in range(4))
