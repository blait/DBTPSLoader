# mssql-loadgen

SQL Server 인스턴스 두 대를 **공정하게 비교**하기 위한 부하 생성기.

대상 DB의 스키마를 조회해 시딩 플랜과 트랜잭션 믹스를 자동으로 만들고, 사용자가
수정한 뒤 양쪽에 같은 부하를 걸어 리소스 사용량을 비교한다. 인스턴스 클래스 변경,
vCPU 조정, 스토리지 변경 같은 결정을 실측으로 뒷받침하는 데 쓴다.

특정 스키마에 묶여 있지 않다. 어떤 SQL Server DB든 연결 정보만 주면 된다.

---

## 이 도구가 하는 일과 하지 않는 일

**사용자가 준비하는 것**

원본 DB에서 스키마를 추출해, **스키마만 있고 데이터는 빈 DB**를 비교 대상 양쪽에
만들어 둔다. `CREATE DATABASE`와 스키마 적용은 이 도구가 하지 않는다.

**도구가 하는 것**

```
빈 DB 2대 (연결 정보만 제공)
      ↓
[1] 스키마 조회        sys.tables / columns / FK / indexes
      ↓             양쪽 스키마가 동일한지도 확인
[2] 시딩 플랜 추천     FK 위상 정렬 + 행수 추정  ← UI에서 수정
      ↓
[3] 시딩 (양쪽)        결정적 생성 → 양쪽에 같은 데이터
      ↓
[4] 동일성 검증        COUNT(*) · CHECKSUM_AGG 대조
      ↓
[5] 워크로드 초안      PK·FK·인덱스에서 SQL 생성  ← UI에서 수정
      ↓
[6] 쿼리 검증          각 SQL을 1회 실행 (쓰기는 롤백)
      ↓             문법·0행 반환·실행 계획 확인
[7] 부하 실행 (양쪽)   같은 TPS로 페이싱
      ↓
[8] 리포트             vCPU 정규화 비교 + 유효성 판정
```

### ⚠️ 빈 DB에만 쓴다

행이 하나라도 있는 DB에는 시딩과 부하를 **거부한다**. 쓰기 부하를 거는 도구이므로,
운용 중인 DB에 실수로 붙는 경로를 아예 없앴다. 검사는 API 핸들러가 아니라 쓰기를
수행하는 함수 안쪽에 있어 CLI로도 우회되지 않는다.

---

## 빠른 시작

### 1. 실행 — Docker (권장)

ODBC 드라이버 설치가 이 도구의 최대 진입장벽이므로 이미지에 담아 뒀다.

```bash
docker compose up --build
# → http://localhost:8010
```

<details>
<summary>직접 설치하려면</summary>

```bash
# ODBC Driver 18 for SQL Server
# macOS
brew tap microsoft/mssql-release https://github.com/Microsoft/homebrew-mssql-release
brew install msodbcsql18
# Debian/Ubuntu → https://learn.microsoft.com/sql/connect/odbc/linux-mac/

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn loadgen.app:app --host 127.0.0.1 --port 8010
```
</details>

### ⚠️ 인증

부하를 거는 도구이므로 **패스워드 없이는 루프백에서만 동작한다.**
다른 주소에서 접근하면 403으로 차단된다.

```bash
LOADGEN_PASSWORD='<강한-패스워드>' docker compose up
```

| 환경변수 | 기본값 | 용도 |
|---|---|---|
| `LOADGEN_PASSWORD` | (없음) | 미설정 시 루프백 전용 |
| `LOADGEN_SESSION_TTL` | `28800` (8시간) | 세션 만료 |
| `LOADGEN_COOKIE_SECURE` | `0` | TLS 뒤에 둘 때 `1` |

공유 패스워드 하나뿐이므로 다중 사용자 환경에는 적합하지 않다. 그런 경우
리버스 프록시에서 인증을 처리할 것.

### 2. 대상 DB 준비 (사용자 몫)

비교할 두 인스턴스에 같은 스키마를 적용하고 데이터는 비워 둔다. 로컬 검증용:

```bash
docker run -d --name mssql-a -e ACCEPT_EULA=Y \
  -e "MSSQL_SA_PASSWORD=<password>" -e MSSQL_PID=Developer \
  -p 1433:1433 mcr.microsoft.com/mssql/server:2022-latest
docker run -d --name mssql-b ... -p 1434:1433 ...
```

### 3. 비교 쌍 설정


`comparisons.yaml`을 `comparisons.local.yaml`로 복사해 편집한다 (후자가 우선하고
`.gitignore` 대상이다).

```yaml
pairs:
  - name: "인스턴스 A/B"
    a: {label: inst-a, vcpu: 32}
    b: {label: inst-b, vcpu: 16}
    workload: draft
    tps: [500, 1000, 2000]
```

`vcpu`를 반드시 적을 것 — 이유는 아래 **왜 CPU%를 그대로 비교하면 안 되는가** 참조.

### 4. 사용

브라우저에서 `http://localhost:8010` — 왼쪽 패널 순서가 곧 작업 순서다.

배치 실행:

```bash
python tools/run_ladder.py --list          # 정의된 쌍 확인
python tools/run_ladder.py --pair 0        # 래더 실행
python tools/iso_tps_compare.py --mode open --md report.md
```

---

## 왜 CPU%를 그대로 비교하면 안 되는가

CPU%는 **가용 vCPU에 대한 비율**이다. 32 vCPU 인스턴스의 20%와 16 vCPU
인스턴스의 20%는 같은 양의 CPU가 아니다. 원값을 비교하면 결론이 뒤집힌다.

그래서 모든 비교를 정규화한다:

```
소비 vCPU   = CPU% / 100 × vCPU
cpu_ms/txn  = 소비 vCPU × 1000 / TPS      ← 공정 비교 지표
비율        = B의 cpu_ms/txn ÷ A의 cpu_ms/txn
```

비율이 1.0 미만이면 B가 **같은 일에 CPU를 덜 썼다**는 뜻이다.

**CPU는 평균이 아니라 최댓값을 쓴다.** 사이징을 정하는 것은 피크이고, 평균은 짧은
스파이크를 지운다. 두 기준의 차이를 감출 수 없도록 평균값도 모든 행에 함께 남긴다.

### 유효성 게이트

측정이 전제를 만족하지 않으면 리포트가 그 레벨을 **이유와 함께 제외**한다.

| 게이트 | 기준 | 이유 |
|---|---|---|
| 목표 달성률 | ≥ 95% | 한쪽이 미달하면 "같은 TPS" 전제가 깨진다. 미달한 쪽은 CPU가 낮게 찍혀 오히려 유리해 보인다 |
| 에러율 | < 1% | 실패한 트랜잭션은 CPU를 덜 쓰면서 레이턴시를 부풀린다 — 어느 방향으로도 쓸 수 없다 |

리포트는 **답하지 못한 것도 스스로 밝힌다**: CPU가 포화되지 않았다면 무엇이 먼저
막았는지(예: 프로비저닝 IOPS), 인스턴스 세대가 다르면 그 효과가 섞였다는 것,
읽기가 디스크에 닿지 않았다면 워킹셋이 작았을 가능성까지 명시한다.

---

## AWS 없이 쓸 수 있는가

부하 생성과 시딩은 AWS와 무관하다. CPU 지표만 출처를 고른다:

| `cpu.source` | 동작 |
|---|---|
| `rds_em` | RDS Enhanced Monitoring (5초). AWS 자격증명 필요 |
| `manual` | 런 폴더에 `cpu.json`을 두어 직접 입력 |
| `none` | CPU 없이 처리량·레이턴시만 비교 |

`none`이어도 리포트는 나온다. TPS와 p50/p99 비교는 그 자체로 유효하고, "CPU를 보지
않았다"는 사실을 리포트가 명시한다.

**필요한 IAM 권한** (`rds_em`을 쓸 때만):
`rds:DescribeDBInstances`, `logs:GetLogEvents`, `cloudwatch:GetMetricData`

---

## 구조

```
loadgen/
  app.py              FastAPI 컨트롤 플레인 (REST + WebSocket)
  comparisons.py      comparisons.yaml 로더
  presets.py          부하 기본값 (단일 정보원)
  schema/
    introspect.py     라이브 DB 스키마 조회
    guard.py          빈 DB 검사 · 양쪽 스키마 대조
    plan.py           시딩 플랜 추천 (FK 위상 정렬 + 행수 추정)
    values.py         컬럼별 값 전략
    verify.py         시딩 후 데이터 동일성 대조
    ranges.py         MAX(id) 조회 → 부하 파라미터 범위
  workload/
    draft.py          스키마 → 트랜잭션 믹스 초안
    store.py          워크로드 JSON 저장 · Mix 조립
  seed/
    seeder.py         플랜 실행 (결정적 배치 INSERT)
    datagen.py        값 생성기
  runner/             coordinator(프로세스) / worker(스레드) / pacing(토큰버킷)
  metrics/            collector(히스토그램) / export(runs/ 아티팩트)
  analysis.py         정규화 지표 · 유효성 게이트 · 짝 맺기
  report_md.py        마크다운 리포트
  cpu_source.py       CPU 지표 출처 플러그인
  static/index.html   웹 UI (Chart.js, 빌드 단계 없음)
tools/                run_ladder(배치) / iso_tps_compare(CLI 리포트) / smoke_run
tests/                순수 함수 테스트 (DB 불필요)
```

**아티팩트**: 런마다 `runs/<run_id>/`에 `meta.json`(설정 + 워크로드 스냅샷),
`timeseries.jsonl/csv`, `summary.json`, `rds_metrics.json`이 남는다. 리포트는 이
파일들만 읽는다 — 어떤 수치도 문서에서 옮겨 적지 않는다.

---

## 자동 생성의 한계

**스키마만으로는 실제 쿼리 패턴을 알 수 없다.** 초안은 PK·FK·인덱스에서 "그럴듯한"
쿼리를 만들지만, 실제 애플리케이션이 그렇게 조회한다는 보장은 없다. 그래서:

- 각 트랜잭션에 **판단 근거**가 붙는다 (UI에서 이름에 마우스를 올리면 보인다)
- 인덱스 근거 없이 만든 조회는 **경고로 표시**된다 (큰 테이블에서 풀스캔이 된다)
- 잘린 테이블 수를 **밝힌다** — 조용히 자르면 "전체를 덮었다"로 읽힌다

**행수 추정도 추천값이다.** 스키마 구조(FK 참조 관계, IDENTITY + 날짜 컬럼 유무,
컬럼 수)로 테이블 성격을 추정해 분배하지만 실제 분포와 다를 수 있다. 수정 없이
진행하면 리포트에 "기본 추정값 사용"이 기록된다.

**INSERT 부하는 DB를 계속 키운다.** 런을 반복하면 나중 런이 더 큰 테이블을
상대하므로, `run_ladder.py`는 양쪽을 번갈아 실행해 편향을 상쇄한다.

### 자동 생성을 건너뛰는 경우

부하가 조용히 무의미해지는 것보다 아무것도 만들지 않는 편이 낫다. 아래 경우는
초안에서 제외하고 이유를 표시하므로, 필요하면 직접 SQL을 작성해야 한다.

**시딩에서 제외** (플랜이 0행으로 두고 `excluded`에 사유를 남긴다)

| 대상 | 이유 |
|---|---|
| 값을 만들 수 없는 타입 (`sql_variant` 등) | NOT NULL 컬럼에 NULL이 가면 5000행 배치가 통째로 죽는다 |
| 복합 FK | 값 조합을 맞출 수 없어 고아 행이 남는다 (삽입 중 제약이 꺼져 있다) |
| CHECK 제약 | 합성값이 거부되면 배치 전체가 실패한다 |
| 트리거 | 삽입 결과를 예측할 수 없다 (INSTEAD OF는 행을 버릴 수도 있다) |
| temporal 테이블 | 이력 테이블에 쓰기가 중복된다 |
| IDENTITY·계산 컬럼뿐인 테이블 | 넣을 컬럼이 없다 |

**부하 SQL에서 제외**

| 대상 | 이유 |
|---|---|
| 복합 PK, GUID·문자열 PK | 단일 정수를 넣으면 조회가 항상 0행인데 "성공"으로 집계된다 |
| 복합 FK 조인 | 값 조합을 스키마만 보고 맞출 수 없다 |
| 필터 인덱스 | 임의 파라미터가 필터 조건을 벗어나 조용히 스캔이 된다 |
| columnstore·XML·공간 인덱스 | seek 대상이 아니다 |
| 유니크 컬럼 (UPDATE 대상) | 임의값을 넣으면 중복 키 위반 |
| `xml`·`geography`·`varbinary` 등 | 드라이버 변환 실패, 또는 전송 비용이 측정을 지배한다 |

`varbinary`는 **시딩에는 포함**한다 — 행 크기와 페이지 밀도가 실제와 같아야 I/O가
재현된다. 부하 조회에서만 뺀다.

`dbo`가 아닌 스키마, 예약어 이름(`[Order]`, `[User]`), 대괄호·비ASCII 문자를 포함한
이름, 순환 FK, 자기참조 FK는 모두 정상 처리된다.

### 값 생성이 지키는 것

| 항목 | 왜 |
|---|---|
| `decimal(p,s)`의 precision 준수 | `decimal(5,4)`에 0~10000을 넣으면 99%가 산술 오버플로다 |
| 유니크 컬럼에 행 순번 주입 | 이름 생성기의 조합은 1,225개뿐 — 1만 행이면 99%가 중복이다 |
| 비IDENTITY 정수 PK는 순번 | 난수를 쓰면 100만 행에서 37%가 충돌한다 |
| nullable은 일부만 NULL | 전부 NULL이면 인덱스를 타지 않고 행 크기가 비현실적이 된다 |
| `time`·`datetimeoffset`에 변동 부여 | 상수를 넣으면 해당 인덱스의 선택도가 0이 된다 |
| FK 범위는 부모의 `schema.table` 기준 | 테이블명만 쓰면 스키마가 다른 동명 테이블과 충돌해 좁은 범위가 이긴다 |

### 부하 전에 쿼리를 검증한다

Workload 패널의 **쿼리 검증**은 각 SQL을 한 번씩 실행해 결과를 보여준다.
쓰기는 `BEGIN TRAN` → `ROLLBACK`이므로 데이터를 건드리지 않는다.

```
✓ order_by_pk           12행   0.8ms   seek
✓ order_join_account    20행   2.1ms   seek
⚠️ task_by_status         0행   0.3ms   ← 행을 못 맞힘
⚠️ log_by_detail      1,204행  84.0ms   scan  ← 인덱스를 안 탄다
✗ doc_by_pk               –      –            잘못된 열 이름
```

**0행 반환을 여기서 잡는 것이 중요하다.** 그건 에러가 아니라 정상 응답이므로
부하 중에는 "성공한 트랜잭션"으로 집계된다 — 에러율 게이트에도 걸리지 않고
TPS만 높게 나온다. 검증 없이는 발견할 방법이 사실상 없다.

실행 계획은 추정이 아니라 **실제 계획**(`SET STATISTICS XML ON`)을 읽는다.
추정 계획은 파라미터 스니핑을 하지 않아 실제 실행과 다른 계획을 낼 수 있다.

### 시딩 결과를 검증한다

계획 행수와 실제 삽입을 대조해, 부족하면 `incomplete`로 표시하고 이유를 남긴다.
제약을 다시 켜지 못한 경우도 보고한다 — 그 DB는 미검증 상태이므로 이후 삽입에서
고아 행이 조용히 들어간다.

---

## 테스트

```bash
.venv/bin/python -m pytest tests/ -q      # DB 불필요
```

DB가 필요한 검증(스키마 조회, 시딩, 부하)은 로컬 도커로 한다. 다만 **성능 측정은
도커로 할 수 없다** — 컨테이너가 먼저 한계에 닿고, 부하기와 DB가 같은 CPU를 나눠
쓰면 측정이 오염된다. 실제 처리량·CPU 비교는 별도 인스턴스에서 해야 한다.

## 라이선스

MIT
