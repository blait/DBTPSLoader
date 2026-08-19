"""설정 모델."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

from .presets import RUN_DEFAULTS


class TargetDB(BaseModel):
    """접속 대상 — 비교할 인스턴스 하나."""

    label: str = Field(..., description="비교 표에 쓰이는 이름 (예: inst-a / inst-b)")
    host: str
    port: int = 1433
    user: str = "sa"
    password: str
    encrypt: bool = True
    trust_server_certificate: bool = True
    login_timeout: int = 15


class SeedConfig(BaseModel):
    """시딩 실행 옵션. 무엇을 몇 행 넣을지는 시딩 플랜이 정한다."""

    batch_size: int = 5000
    workers: int = 4


class RunConfig(BaseModel):
    """부하 실행 설정. 기본값은 `loadgen.presets.RUN_DEFAULTS` 하나에서만 온다.

    `mode`가 open이면 목표 TPS로 페이싱한다. 쌍 비교에서는 open이 기본인데,
    양쪽에 같은 TPS를 걸어야 "같은 일을 같은 양만큼" 했다고 말할 수 있고
    그때 남는 차이를 리소스 효율 차이로 읽을 수 있기 때문이다.
    """

    mode: Literal["closed", "open"] = RUN_DEFAULTS["mode"]
    duration_sec: int = RUN_DEFAULTS["duration_sec"]
    warmup_sec: int = RUN_DEFAULTS["warmup_sec"]
    processes: int = RUN_DEFAULTS["processes"]
    threads_per_process: int = RUN_DEFAULTS["threads_per_process"]
    target_tps: Optional[int] = Field(
        RUN_DEFAULTS["target_tps"], description="open 모드 전용. 전체 워커 합계 기준")
    read_pct: int = Field(RUN_DEFAULTS["read_pct"], ge=0, le=100)
    note: str = ""


class ControlMsg(BaseModel):
    """실행 중 워커에 브로드캐스트 (재시작 불필요)."""

    read_pct: Optional[int] = None
    target_tps: Optional[int] = None
    stop: bool = False
