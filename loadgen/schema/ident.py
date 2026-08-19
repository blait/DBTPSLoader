"""SQL Server 식별자 인용 — 스키마명·테이블명·컬럼명을 안전하게 SQL에 넣는다.

한 곳에 모아두는 이유가 세 가지 있다.

**1. dbo가 아닌 스키마.** `dbo.[Table]`을 하드코딩하면 `sales.Invoice` 같은 스키마를
쓰는 DB에서 전부 실패한다. 사용자 스키마는 우리가 고를 수 없다.

**2. 예약어.** `Order`, `Select`, `Key`, `User` 같은 이름은 대괄호로 감싸지 않으면
문법 오류가 된다. 실제 스키마에 흔하다.

**3. 대괄호를 포함한 이름.** SQL Server는 `[My]Table]` 같은 이름을 허용하고, 이때
닫는 대괄호는 `]]`로 이스케이프해야 한다. 이스케이프하지 않으면 인용이 조기에
끝나 문법 오류가 나거나 — 더 나쁘게 — 의도하지 않은 SQL이 실행될 수 있다.
"""
from __future__ import annotations


def quote(name: str) -> str:
    """식별자 하나를 대괄호로 감싼다. 내부의 `]`는 `]]`로 이스케이프한다."""
    return "[" + str(name).replace("]", "]]") + "]"


def qualify(schema: str | None, name: str) -> str:
    """`[schema].[name]`. schema가 없으면 `[dbo]`를 쓴다.

    sys.* 조회는 항상 스키마를 함께 돌려주므로 정상 경로에서는 None이 오지 않는다.
    기본값은 직접 만든 호출을 위한 안전망이다.
    """
    return f"{quote(schema or 'dbo')}.{quote(name)}"


def object_name(schema: str | None, name: str) -> str:
    """OBJECT_ID()에 넘길 문자열. 파라미터로 바인딩할 값이므로 대괄호를 포함한다.

    `OBJECT_ID(N'[sales].[Order]')`는 유효하고, 대괄호가 있어야 예약어 이름도
    올바르게 해석된다.
    """
    return qualify(schema, name)
