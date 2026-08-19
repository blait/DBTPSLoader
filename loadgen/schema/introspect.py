"""라이브 DB에서 스키마를 읽는다.

이 도구는 스키마 파일을 받지 않는다. 사용자가 준비한 DB에 붙어 `sys.*` 카탈로그를
직접 조회하고, 그 결과로 시딩 플랜과 트랜잭션 믹스를 만든다.

권한이 부족해 조회가 비는 경우와 테이블이 실제로 없는 경우를 구분해 보고한다 —
둘을 뭉치면 "빈 DB"로 오인해 엉뚱한 플랜을 만든다.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..config import TargetDB
from ..db import connect

log = logging.getLogger(__name__)

# 부하 파라미터로 쓸 수 있는 PK 타입. 순번을 뽑아 넣을 수 있어야 한다.
NUMERIC_TYPES = ("int", "bigint", "smallint", "tinyint", "decimal", "numeric")

# INSERT 컬럼 목록에서 항상 빠지는 타입.
_NEVER_INSERT = ("timestamp", "rowversion")


@dataclass
class Column:
    name: str
    type: str
    max_length: int
    precision: int
    scale: int
    nullable: bool
    identity: bool
    computed: bool
    default: str | None = None
    generated: bool = False   # GENERATED ALWAYS (temporal 테이블의 period 컬럼)
    collation: str | None = None

    @property
    def insertable(self) -> bool:
        """INSERT 컬럼 목록에 넣을 수 있는가.

        temporal 테이블의 period 컬럼(GENERATED ALWAYS)을 반드시 제외해야 한다 —
        SQL Server가 직접 삽입을 거부하므로, 포함하면 해당 테이블의 시딩이 전부
        실패한다.
        """
        return (not self.identity and not self.computed and not self.generated
                and self.type not in _NEVER_INSERT)


@dataclass
class ForeignKey:
    name: str
    columns: list[str]
    ref_schema: str
    ref_name: str
    ref_columns: list[str]

    @property
    def ref_table(self) -> str:
        """"schema.table" — ctx 키와 워크로드 `of` 참조에 쓴다.

        스키마와 이름을 따로 보관하는 이유: 이름 자체에 점이 들어갈 수 있고
        (`dbo.My.Table`은 유효하다), 합친 문자열을 rpartition으로 되쪼개면
        `schema='dbo.My'`가 되어 존재하지 않는 객체를 가리킨다.
        """
        return f"{self.ref_schema}.{self.ref_name}"


@dataclass
class Index:
    name: str
    columns: list[str]
    included: list[str]
    unique: bool
    primary_key: bool
    filtered: bool = False   # 필터 인덱스 — seek 키로 쓰면 조용히 스캔이 된다


@dataclass
class Table:
    database: str
    schema: str
    name: str
    columns: list[Column] = field(default_factory=list)
    primary_key: list[str] = field(default_factory=list)
    foreign_keys: list[ForeignKey] = field(default_factory=list)
    indexes: list[Index] = field(default_factory=list)
    row_count: int = 0
    has_trigger: bool = False
    has_check: bool = False
    temporal: bool = False    # SYSTEM_VERSIONED — 시딩 대상에서 제외해야 한다

    @property
    def qualified(self) -> str:
        return f"{self.schema}.{self.name}"

    @property
    def single_pk(self) -> str | None:
        """단일 컬럼 PK 이름. 복합이거나 없으면 None."""
        return self.primary_key[0] if len(self.primary_key) == 1 else None

    @property
    def numeric_single_pk(self) -> str | None:
        """단일 컬럼 **숫자형** PK. 부하 파라미터로 쓸 수 있는 유일한 형태다.

        uniqueidentifier나 문자열 PK를 걸러내는 것이 요점이다. 그런 PK에 정수를
        넣으면 조회가 항상 0행을 돌려주는데, 그것도 "성공한 트랜잭션"으로 집계되어
        서버는 일을 거의 하지 않고 TPS만 높게 나온다 — 부하가 조용히 무의미해진다.
        """
        pk = self.single_pk
        if not pk:
            return None
        col = next((c for c in self.columns if c.name == pk), None)
        return pk if col and col.type in NUMERIC_TYPES else None

    @property
    def identity_pk(self) -> str | None:
        """IDENTITY인 단일 숫자형 PK — append-only 판정의 기준."""
        pk = self.numeric_single_pk
        if not pk:
            return None
        col = next((c for c in self.columns if c.name == pk), None)
        return pk if col and col.identity else None


# --------------------------------------------------------------------- 조회 SQL

# temporal_type: 0=비temporal, 1=이력 테이블, 2=시스템 버전 관리 테이블.
# 이력 테이블(1)은 아예 제외한다 — 사용자가 직접 쓰는 테이블이 아니고 삽입도 막혀 있다.
_Q_TABLES = """
SELECT s.name, t.name,
       (SELECT SUM(p.rows) FROM sys.partitions p
        WHERE p.object_id = t.object_id AND p.index_id IN (0, 1)),
       (SELECT COUNT(*) FROM sys.triggers tr WHERE tr.parent_id = t.object_id),
       (SELECT COUNT(*) FROM sys.check_constraints cc WHERE cc.parent_object_id = t.object_id),
       t.temporal_type
FROM sys.tables t
JOIN sys.schemas s ON s.schema_id = t.schema_id
WHERE t.is_ms_shipped = 0 AND t.temporal_type <> 1
ORDER BY s.name, t.name
"""

_Q_COLUMNS = """
SELECT s.name, t.name, c.name, ty.name, c.max_length, c.precision, c.scale,
       c.is_nullable, c.is_identity, c.is_computed, dc.definition,
       c.generated_always_type, c.collation_name
FROM sys.columns c
JOIN sys.tables t ON t.object_id = c.object_id
JOIN sys.schemas s ON s.schema_id = t.schema_id
JOIN sys.types ty ON ty.user_type_id = c.user_type_id
LEFT JOIN sys.default_constraints dc ON dc.object_id = c.default_object_id
WHERE t.is_ms_shipped = 0 AND t.temporal_type <> 1
ORDER BY s.name, t.name, c.column_id
"""

# i.type: 1=clustered rowstore, 2=nonclustered rowstore, 3=XML, 4=spatial,
# 5=CCI, 6=NCCI, 7=hash. rowstore만 남긴다 — 나머지는 `WHERE col = ?` 형태의
# seek 대상이 아니고, columnstore 컬럼을 seek 키로 쓰면 스캔이 된다.
# has_filter도 함께 읽는다: 필터 인덱스를 평범한 seek 키로 쓰면 임의 파라미터가
# 필터 조건을 벗어나 조용히 스캔이 된다.
_Q_INDEXES = """
SELECT s.name, t.name, i.name, i.is_unique, i.is_primary_key,
       c.name, ic.is_included_column, ic.key_ordinal, i.has_filter
FROM sys.indexes i
JOIN sys.tables t ON t.object_id = i.object_id
JOIN sys.schemas s ON s.schema_id = t.schema_id
JOIN sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id
JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
WHERE t.is_ms_shipped = 0 AND i.type IN (1, 2) AND t.temporal_type <> 1
ORDER BY s.name, t.name, i.name, ic.is_included_column, ic.key_ordinal
"""

_Q_FKS = """
SELECT s.name, t.name, fk.name, pc.name, rs.name, rt.name, rc.name, fkc.constraint_column_id
FROM sys.foreign_keys fk
JOIN sys.tables t ON t.object_id = fk.parent_object_id
JOIN sys.schemas s ON s.schema_id = t.schema_id
JOIN sys.tables rt ON rt.object_id = fk.referenced_object_id
JOIN sys.schemas rs ON rs.schema_id = rt.schema_id
JOIN sys.foreign_key_columns fkc ON fkc.constraint_object_id = fk.object_id
JOIN sys.columns pc ON pc.object_id = fkc.parent_object_id
                   AND pc.column_id = fkc.parent_column_id
JOIN sys.columns rc ON rc.object_id = fkc.referenced_object_id
                   AND rc.column_id = fkc.referenced_column_id
ORDER BY s.name, t.name, fk.name, fkc.constraint_column_id
"""


def introspect(target: TargetDB, database: str) -> dict[str, Table]:
    """{"schema.table": Table} — 한 데이터베이스의 전체 스키마.

    조회는 4번의 왕복으로 끝낸다 (테이블·컬럼·인덱스·FK). 테이블마다 질의하면
    200개 테이블에서 800번이 되므로, 전체를 한 번에 읽어 파이썬에서 묶는다.
    """
    tables: dict[str, Table] = {}
    with connect(target, database, autocommit=True) as conn:
        cur = conn.cursor()

        cur.execute(_Q_TABLES)
        for sch, name, rows, trig, chk, temporal in cur.fetchall():
            tables[f"{sch}.{name}"] = Table(
                database=database, schema=sch, name=name,
                row_count=int(rows or 0), has_trigger=bool(trig), has_check=bool(chk),
                temporal=(temporal == 2))

        cur.execute(_Q_COLUMNS)
        for (sch, tname, cname, ctype, mlen, prec, scale, nul, ident, comp,
             dflt, gen, coll) in cur.fetchall():
            t = tables.get(f"{sch}.{tname}")
            if t is None:
                continue
            t.columns.append(Column(
                name=cname, type=ctype, max_length=mlen, precision=prec, scale=scale,
                nullable=bool(nul), identity=bool(ident), computed=bool(comp),
                default=dflt, generated=bool(gen), collation=coll))

        cur.execute(_Q_INDEXES)
        idx_acc: dict[tuple[str, str], Index] = {}
        for (sch, tname, iname, uniq, is_pk, cname, included, _ord,
             filt) in cur.fetchall():
            key = (f"{sch}.{tname}", iname)
            ix = idx_acc.get(key)
            if ix is None:
                ix = idx_acc[key] = Index(name=iname, columns=[], included=[],
                                          unique=bool(uniq), primary_key=bool(is_pk),
                                          filtered=bool(filt))
            (ix.included if included else ix.columns).append(cname)
        for (tkey, _), ix in idx_acc.items():
            t = tables.get(tkey)
            if t is None:
                continue
            t.indexes.append(ix)
            if ix.primary_key:
                t.primary_key = list(ix.columns)

        cur.execute(_Q_FKS)
        fk_acc: dict[tuple[str, str], ForeignKey] = {}
        for sch, tname, fkname, col, ref_s, ref_n, ref_c, _ord in cur.fetchall():
            key = (f"{sch}.{tname}", fkname)
            fk = fk_acc.get(key)
            if fk is None:
                fk = fk_acc[key] = ForeignKey(name=fkname, columns=[],
                                              ref_schema=ref_s, ref_name=ref_n,
                                              ref_columns=[])
            fk.columns.append(col)
            fk.ref_columns.append(ref_c)
        for (tkey, _), fk in fk_acc.items():
            t = tables.get(tkey)
            if t is not None:
                t.foreign_keys.append(fk)

    return tables


def to_dict(tables: dict[str, Table]) -> dict:
    """UI/JSON용 직렬화."""
    return {
        "tables": [
            {
                "database": t.database, "schema": t.schema, "name": t.name,
                "qualified": t.qualified, "row_count": t.row_count,
                "primary_key": t.primary_key,
                "identity_pk": t.identity_pk,
                "has_trigger": t.has_trigger, "has_check": t.has_check,
                "temporal": t.temporal,
                "numeric_single_pk": t.numeric_single_pk,
                "columns": [
                    {"name": c.name, "type": c.type, "max_length": c.max_length,
                     "nullable": c.nullable, "identity": c.identity,
                     "computed": c.computed, "insertable": c.insertable,
                     "has_default": c.default is not None}
                    for c in t.columns
                ],
                "foreign_keys": [
                    {"name": fk.name, "columns": fk.columns,
                     "ref_table": fk.ref_table, "ref_columns": fk.ref_columns}
                    for fk in t.foreign_keys
                ],
                "indexes": [
                    {"name": ix.name, "columns": ix.columns, "included": ix.included,
                     "unique": ix.unique, "primary_key": ix.primary_key}
                    for ix in t.indexes
                ],
            }
            for t in sorted(tables.values(), key=lambda x: x.qualified)
        ],
    }


def fingerprint(tables: dict[str, Table]) -> dict[str, tuple]:
    """비교용 지문 — 두 인스턴스의 스키마가 같은지 판정할 때 쓴다.

    행수는 일부러 제외한다. 시딩 전이라 양쪽 다 0이고, 시딩 후에는 다를 수 있는데
    그건 스키마 문제가 아니라 데이터 문제(`schema.verify`가 따로 본다)다.
    """
    return {
        key: (
            # collation을 포함한다. 한쪽이 CI_AS, 다른 쪽이 CS_AS면 비교 의미와
            # 인덱스 선택도·정렬 비용이 달라지는데, 그게 바로 측정 대상이다.
            tuple(sorted((c.name, c.type, c.max_length, c.nullable,
                          c.identity, c.computed, c.collation or "")
                         for c in t.columns)),
            tuple(t.primary_key),
            tuple(sorted((fk.ref_table, tuple(fk.columns)) for fk in t.foreign_keys)),
            tuple(sorted((tuple(ix.columns), tuple(ix.included), ix.unique)
                         for ix in t.indexes)),
        )
        for key, t in tables.items()
    }
