# 예제

## sample-schema.sql

도구를 시험해보기 위한 스키마다. 비교할 두 DB에 각각 적용하고 데이터는 비워 둔다.

```bash
sqlcmd -S <host> -U <user> -P <pw> -d <empty-db> -i sample-schema.sql
```

도구의 판정 로직을 골고루 태우도록 구성했다. 각 요소가 무엇을 시험하는지:

| 요소 | 기대 동작 |
|---|---|
| `sales` 스키마 | `dbo`가 아닌 스키마도 정상 처리 |
| `[Order]`, `[User]`, `[Key]` | 예약어 이름이 대괄호로 인용된다 |
| `Account` ← `Order` ← `OrderLine` | FK 위상 정렬로 부모부터 삽입 |
| `OrderLine` (복합 PK) | 부하 대상에서 제외 — 단일 값으로는 행을 맞힐 수 없다 |
| `Session` (GUID PK) | 같은 이유로 제외 |
| `Account.Email` (UNIQUE) | 값에 행 순번을 섞어 중복을 피한다 |
| `Rate decimal(5,4)` | 최대 9.9999 — precision을 넘지 않는 값이 생성된다 |
| `Payment` (CHECK 제약) | 시딩에서 제외 — 합성값이 거부되면 배치가 죽는다 |
| `IX_Order_Status` (필터 인덱스) | 조회 초안에 쓰이지 않는다 — 임의 파라미터가 필터를 벗어난다 |
| `Order.Payload varbinary(MAX)` | 시딩에는 포함(행 크기 재현), 조회에서는 제외 |
| `Category.ParentId` (자기참조 FK) | 정렬이 무한 루프에 빠지지 않는다 |
| `AuditLog`, `EventLog` | IDENTITY + 날짜 → append-only 로그로 분류되어 가장 큰 몫 |
| `Ticket` (IDENTITY만) | 넣을 컬럼이 없어 0행 |
| `Account.FullName` (computed) | INSERT 목록에서 제외 |

적용 후 UI에서 **Schema → 조회**를 누르면 위 판정이 표에 나타난다.
**Seed Plan → 초안 생성**을 하면 `excluded`에 제외 사유가 표시된다.
