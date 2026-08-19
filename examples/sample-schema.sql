-- 예제 스키마 — 도구를 시험해보기 위한 것
--
-- 대상 DB 두 대에 이 스크립트를 적용하고 데이터는 비워 두면, 도구가 스키마를
-- 조회해 시딩 플랜과 워크로드를 만든다. 실제로는 자신의 스키마를 쓸 것이다.
--
-- 도구의 판정 로직을 골고루 태우도록 구성했다:
--   · dbo 아닌 스키마 (sales)
--   · 예약어 테이블·컬럼명 ([Order], [User], [Key], [LineNo])
--   · FK 체인 (Account <- Order <- OrderLine)
--   · 복합 PK (OrderLine) → 도구가 부하 대상에서 제외해야 한다
--   · GUID PK (Session) → 같은 이유로 제외 대상
--   · 유니크 제약 (Account.Email) → 값 생성 시 순번을 섞어야 한다
--   · decimal 다양한 precision/scale
--   · nullable / computed / IDENTITY
--   · append-only 로그 (AuditLog, 가장 큰 몫을 받아야 한다)
--   · NC 인덱스 (조회 초안의 근거)
--   · CHECK 제약 (Payment) → 시딩에서 제외되어야 한다
--   · varbinary(MAX) → 시딩에는 포함, 부하 조회에서는 제외

IF SCHEMA_ID('sales') IS NULL EXEC('CREATE SCHEMA sales');
GO

-- 마스터 (많이 참조됨 → 엔티티로 분류되어 적은 행수)
CREATE TABLE sales.Account (
    Id           int IDENTITY(1,1) PRIMARY KEY,
    Email        nvarchar(200) NOT NULL,
    Nickname     nvarchar(60)  NOT NULL,
    FirstName    nvarchar(40)  NULL,
    LastName     nvarchar(40)  NULL,
    FullName     AS (LTRIM(RTRIM(ISNULL(FirstName,'') + ' ' + ISNULL(LastName,'')))),
    Balance      decimal(18,2) NOT NULL,
    Rate         decimal(5,4)  NOT NULL,   -- 최대 9.9999 — precision 준수 시험
    Country      char(2)       NOT NULL,
    IsActive     bit           NOT NULL,
    CreatedAt    datetime2     NOT NULL,
    UpdatedAt    datetime2     NULL,
    CONSTRAINT UQ_Account_Email UNIQUE (Email)
);
CREATE INDEX IX_Account_Country ON sales.Account(Country);
GO

-- 코드성 룩업 (컬럼 적고 많이 참조됨)
CREATE TABLE sales.Currency (
    Id    tinyint      NOT NULL PRIMARY KEY,   -- IDENTITY 아님 → 순번을 써야 한다
    [Key] char(3)      NOT NULL,               -- 예약어
    Name  nvarchar(40) NOT NULL
);
GO

-- 거래 (예약어 테이블명)
CREATE TABLE sales.[Order] (
    Id          bigint IDENTITY(1,1) PRIMARY KEY,
    AccountId   int          NOT NULL,
    CurrencyId  tinyint      NOT NULL,
    [User]      nvarchar(60) NOT NULL,          -- 예약어 컬럼
    Total       decimal(12,2) NOT NULL,
    Status      tinyint      NOT NULL,
    Note        nvarchar(400) NULL,
    Payload     varbinary(MAX) NULL,            -- 시딩엔 포함, 조회엔 제외
    PlacedAt    datetimeoffset NOT NULL,
    CreatedAt   datetime2    NOT NULL,
    CONSTRAINT FK_Order_Account  FOREIGN KEY (AccountId)  REFERENCES sales.Account(Id),
    CONSTRAINT FK_Order_Currency FOREIGN KEY (CurrencyId) REFERENCES sales.Currency(Id)
);
CREATE INDEX IX_Order_AccountId ON sales.[Order](AccountId);
CREATE INDEX IX_Order_Status    ON sales.[Order](Status) WHERE Status = 1;  -- 필터 인덱스
GO

-- 상세 라인 (복합 PK → 부하 대상에서 제외되어야 한다)
CREATE TABLE sales.OrderLine (
    OrderId  bigint        NOT NULL,
    [LineNo] int           NOT NULL,     -- LineNo 도 예약어다
    Sku      nvarchar(40)  NOT NULL,
    Qty      int           NOT NULL,
    Price    decimal(10,3) NOT NULL,
    CONSTRAINT PK_OrderLine PRIMARY KEY (OrderId, [LineNo]),
    CONSTRAINT FK_OrderLine_Order FOREIGN KEY (OrderId) REFERENCES sales.[Order](Id)
);
GO

-- CHECK 제약 → 시딩에서 제외되어야 한다
CREATE TABLE dbo.Payment (
    Id        int IDENTITY(1,1) PRIMARY KEY,
    OrderId   bigint        NOT NULL,
    Amount    decimal(12,2) NOT NULL,
    CreatedAt datetime2     NOT NULL,
    CONSTRAINT CK_Payment_Amount CHECK (Amount > 0)
);
GO

-- GUID PK → 부하 대상에서 제외되어야 한다
CREATE TABLE dbo.Session (
    Id        uniqueidentifier NOT NULL PRIMARY KEY,
    AccountId int              NOT NULL,
    Token     nvarchar(200)    NOT NULL,
    IssuedAt  datetime2        NOT NULL
);
GO

-- 자기참조 FK
CREATE TABLE dbo.Category (
    Id       int IDENTITY(1,1) PRIMARY KEY,
    ParentId int          NULL,
    Name     nvarchar(80) NOT NULL,
    CONSTRAINT FK_Category_Parent FOREIGN KEY (ParentId) REFERENCES dbo.Category(Id)
);
GO

-- append-only 로그 (IDENTITY + 날짜 → 가장 큰 몫)
CREATE TABLE dbo.AuditLog (
    Id        bigint IDENTITY(1,1) PRIMARY KEY,
    AccountId int           NULL,
    Action    nvarchar(40)  NOT NULL,
    Detail    nvarchar(500) NULL,
    IpText    varchar(45)   NULL,
    AtTime    time(7)       NOT NULL,
    CreatedAt datetime2     NOT NULL
);
CREATE INDEX IX_AuditLog_AccountId ON dbo.AuditLog(AccountId, Id DESC);
GO

CREATE TABLE dbo.EventLog (
    Id        bigint IDENTITY(1,1) PRIMARY KEY,
    Kind      smallint      NOT NULL,
    Source    nvarchar(60)  NOT NULL,
    Message   nvarchar(1000) NULL,
    CreatedAt datetime2     NOT NULL
);
CREATE INDEX IX_EventLog_Kind ON dbo.EventLog(Kind, CreatedAt);
GO

-- IDENTITY만 있는 테이블 → 넣을 컬럼이 없어 0행이 되어야 한다
CREATE TABLE dbo.Ticket (
    Id bigint IDENTITY(1,1) PRIMARY KEY
);
GO
