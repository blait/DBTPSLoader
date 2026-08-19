"""트랜잭션 믹스 모델만 제공한다.

워크로드는 대상 DB의 스키마에서 생성되어 파일로 저장되므로, 여기에 내장 목록이
없다. 조립은 `loadgen.workload.store.build_mix()`가 담당한다.
"""
from .base import Mix, Txn  # noqa: F401
