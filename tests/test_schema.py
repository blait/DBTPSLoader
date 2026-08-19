"""스키마 조회 계층의 순수 함수 테스트 — DB 불필요.

FK 위상 정렬, 행수 추정, 컬럼별 값 전략, 워크로드 초안을 가짜 스키마로 검증한다.
실제 DB 연결이 필요한 부분(introspect 질의, 안전 검사)은 도커 검증에서 다룬다.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loadgen.schema.introspect import Column, ForeignKey, Index, Table  # noqa: E402
from loadgen.schema.plan import _topo_order, draft_plan  # noqa: E402
from loadgen.schema.values import make_factory, value_for  # noqa: E402
from loadgen.seed.datagen import Gen  # noqa: E402
from loadgen.workload.draft import draft_workload  # noqa: E402
from loadgen.workload.store import build_mix  # noqa: E402


# ------------------------------------------------------------------ 픽스처

def _col(name, type="int", **kw):
    d = dict(max_length=4, precision=10, scale=0, nullable=False,
             identity=False, computed=False)
    d.update(kw)
    return Column(name=name, type=type, **d)


def _table(name, cols, pk=None, fks=(), indexes=(), rows=0, **kw):
    return Table(database="db", schema="dbo", name=name, columns=list(cols),
                 primary_key=list(pk or []), foreign_keys=list(fks),
                 indexes=list(indexes), row_count=rows, **kw)


def _simple_schema():
    """account(마스터) <- order(거래) <- order_item(자식), audit_log(로그)"""
    account = _table("account", [
        _col("Id", "int", identity=True),
        _col("Email", "nvarchar", max_length=200),
        _col("CreatedAt", "datetime2", max_length=8),
    ], pk=["Id"], rows=0)
    order = _table("order", [
        _col("Id", "int", identity=True),
        _col("AccountId"),
        _col("Total", "decimal", scale=2),
        _col("CreatedAt", "datetime2", max_length=8),
    ], pk=["Id"],
        fks=[ForeignKey("FK_order_account", ["AccountId"], "dbo.account", ["Id"])],
        indexes=[Index("IX_order_AccountId", ["AccountId"], [], False, False)])
    item = _table("order_item", [
        _col("Id", "int", identity=True),
        _col("OrderId"),
        _col("Qty"),
    ], pk=["Id"],
        fks=[ForeignKey("FK_item_order", ["OrderId"], "dbo.order", ["Id"])])
    audit = _table("audit_log", [
        _col("Id", "bigint", identity=True),
        _col("Detail", "nvarchar", max_length=-1, nullable=True),
        _col("CreatedAt", "datetime2", max_length=8),
    ], pk=["Id"])
    return {"dbo.account": account, "dbo.order": order,
            "dbo.order_item": item, "dbo.audit_log": audit}


# ------------------------------------------------------------- FK 위상 정렬

def test_topo_order_parents_first():
    order = _topo_order(_simple_schema())
    assert order.index("dbo.account") < order.index("dbo.order")
    assert order.index("dbo.order") < order.index("dbo.order_item")


def test_topo_order_handles_cycle():
    # 순환 FK가 있어도 실패하지 않는다 — 삽입 중 FK 검사를 끄기 때문에
    # 순서가 완벽하지 않아도 진행된다.
    a = _table("a", [_col("Id", identity=True), _col("BId")], pk=["Id"],
               fks=[ForeignKey("fk1", ["BId"], "dbo.b", ["Id"])])
    b = _table("b", [_col("Id", identity=True), _col("AId")], pk=["Id"],
               fks=[ForeignKey("fk2", ["AId"], "dbo.a", ["Id"])])
    order = _topo_order({"dbo.a": a, "dbo.b": b})
    assert sorted(order) == ["dbo.a", "dbo.b"]     # 둘 다 포함, 순서는 임의


def test_topo_order_self_reference():
    t = _table("node", [_col("Id", identity=True), _col("ParentId", nullable=True)],
               pk=["Id"], fks=[ForeignKey("fk", ["ParentId"], "dbo.node", ["Id"])])
    assert _topo_order({"dbo.node": t}) == ["dbo.node"]


# ------------------------------------------------------------------ 시딩 플랜

def test_draft_plan_distributes_rows_unevenly():
    plan = draft_plan({"db": _simple_schema()}, total_rows=1_000_000)
    rows = {t["table"]: t["rows"] for t in plan["tables"]}
    # 로그성 테이블이 마스터보다 많아야 append-only 쓰기가 의미를 갖는다
    assert rows["audit_log"] > rows["account"]
    # 자식이 부모보다 많아야 조인이 현실적인 행수를 돌려준다
    assert rows["order_item"] > rows["order"]


def test_draft_plan_excludes_identity_and_computed():
    schema = _simple_schema()
    schema["dbo.account"].columns.append(
        _col("FullName", "nvarchar", max_length=200, computed=True))
    plan = draft_plan({"db": schema}, total_rows=10_000)
    acct = next(t for t in plan["tables"] if t["table"] == "account")
    assert "Id" not in acct["columns"]          # IDENTITY
    assert "FullName" not in acct["columns"]    # 계산 컬럼
    skipped = {s["name"] for s in acct["skipped_columns"]}
    assert {"Id", "FullName"} <= skipped        # 왜 빠졌는지 남는다


def test_draft_plan_warns_on_trigger_and_check():
    schema = _simple_schema()
    schema["dbo.order"].has_trigger = True
    schema["dbo.order"].has_check = True
    plan = draft_plan({"db": schema}, total_rows=10_000)
    o = next(t for t in plan["tables"] if t["table"] == "order")
    # 조용히 넘기지 않는다 — 합성값이 거부될 수 있다
    assert any("트리거" in w for w in o["warnings"])
    assert any("CHECK" in w for w in o["warnings"])


def test_draft_plan_marks_unedited():
    plan = draft_plan({"db": _simple_schema()}, total_rows=10_000)
    # 사용자가 손대지 않았음이 기록돼야 리포트가 "기본 추정값"임을 밝힐 수 있다
    assert plan["edited"] is False


# ------------------------------------------------------------------ 값 전략

def test_value_for_nullable_is_not_always_null():
    # 원본 구현은 nullable이면 타입도 보지 않고 NULL을 반환했다. 그러면 인덱스를
    # 타지 않고 행 크기도 비현실적이 된다.
    c = _col("Note", "nvarchar", max_length=200, nullable=True)
    g = Gen(seed=1)
    vals = [value_for(c, g, i, 1000, {}) for i in range(200)]
    assert any(v is not None for v in vals), "nullable 컬럼이 전부 NULL이다"
    assert any(v is None for v in vals), "nullable인데 NULL이 하나도 없다"


def test_value_for_respects_max_length():
    c = _col("Code", "nvarchar", max_length=20)   # 20바이트 = 10문자
    g = Gen(seed=1)
    for i in range(50):
        v = value_for(c, g, i, 100, {})
        assert isinstance(v, str) and len(v) <= 10


def test_value_for_fk_uses_parent_range():
    c = _col("AccountId")
    g = Gen(seed=1)
    vals = [value_for(c, g, i, 100, {"account": 500}, fk_parent="account")
            for i in range(200)]
    assert all(1 <= v <= 500 for v in vals)


def test_value_for_fk_empty_parent():
    # 부모 범위를 모를 때 0이나 음수를 내보내면 FK 위반이 된다
    c = _col("AccountId")
    g = Gen(seed=1)
    assert value_for(c, g, 1, 10, {}, fk_parent="missing") == 1


def test_value_for_dates_are_monotonic():
    # append-only 테이블의 시간 분포를 재현해야 한다
    c = _col("CreatedAt", "datetime2", max_length=8)
    g = Gen(seed=1)
    seq = [value_for(c, g, i, 100, {}) for i in range(1, 101)]
    assert seq == sorted(seq)


def test_make_factory_arity_matches_columns():
    t = _simple_schema()["dbo.order"]
    cols = [c.name for c in t.columns if c.insertable]
    factory = make_factory(t, cols)
    row = factory(Gen(seed=1), 1, 100, {"account": 50})
    assert len(row) == len(cols)


# --------------------------------------------------------------- 워크로드 초안

def test_draft_workload_creates_reads_and_writes():
    w = draft_workload({"db": _simple_schema()}, max_tables=10)
    assert w["read_count"] > 0 and w["write_count"] > 0


def test_draft_workload_builds_valid_mix():
    # 초안이 곧바로 실행 가능해야 한다 — 파라미터 개수가 SQL과 맞아야 조립된다
    w = draft_workload({"db": _simple_schema()}, max_tables=10)
    mix = build_mix(w)
    assert mix.reads and mix.writes


def test_draft_workload_update_uses_uniform_id():
    # UPDATE 대상에 편중 분포를 쓰면 모든 커넥션이 같은 행을 잠그려 들어
    # 직렬화된다. 이 구분은 타협 대상이 아니다.
    w = draft_workload({"db": _simple_schema()}, max_tables=10)
    updates = [t for t in w["txns"] if t["name"].endswith("_update")]
    assert updates, "UPDATE 트랜잭션이 생성되지 않았다"
    for t in updates:
        gens = [p["gen"] for stmt in t["params"] for p in stmt]
        assert "uniform_id" in gens
        assert "skewed_id" not in gens


def test_draft_workload_reads_use_skewed_id():
    w = draft_workload({"db": _simple_schema()}, max_tables=10)
    pk_reads = [t for t in w["txns"] if t["name"].endswith("_by_pk")]
    assert pk_reads
    for t in pk_reads:
        gens = [p["gen"] for stmt in t["params"] for p in stmt]
        assert "skewed_id" in gens


def test_draft_workload_reports_dropped_tables():
    # 조용히 자르면 "전체를 덮었다"로 읽힌다
    w = draft_workload({"db": _simple_schema()}, max_tables=2)
    assert any("제외" in x for x in w["warnings"])


def test_draft_workload_prefers_larger_tables():
    schema = _simple_schema()
    schema["dbo.audit_log"].row_count = 10_000_000
    schema["dbo.account"].row_count = 100
    w = draft_workload({"db": schema}, max_tables=1)
    assert all("audit_log" in t["name"] for t in w["txns"])


# ============================================================================
# 실전 스키마 대응 — 오픈소스 배포에서는 사용자 스키마를 고를 수 없다
# ============================================================================

from loadgen.schema.ident import qualify, quote  # noqa: E402


def test_quote_escapes_closing_bracket():
    # SQL Server는 [My]Table] 같은 이름을 허용한다. 이스케이프하지 않으면 인용이
    # 조기에 끝나 문법 오류가 나거나 의도하지 않은 SQL이 실행된다.
    assert quote("My]Table") == "[My]]Table]"
    assert quote("Order") == "[Order]"


def test_qualify_non_dbo_schema():
    assert qualify("sales", "Invoice") == "[sales].[Invoice]"
    assert qualify(None, "T") == "[dbo].[T]"


def test_reserved_words_are_quoted_in_generated_sql():
    # [Order], [Select] 같은 예약어는 실제 스키마에 흔하다
    t = _table("Order", [
        _col("Id", "int", identity=True),
        _col("Select", "nvarchar", max_length=100),
        _col("CreatedAt", "datetime2", max_length=8),
    ], pk=["Id"], rows=100)
    w = draft_workload({"db": {"dbo.Order": t}}, max_tables=5)
    for txn in w["txns"]:
        for sql in txn["sql"]:
            assert "[Order]" in sql or "[Select]" in sql
            # 인용 없는 맨 이름이 SQL에 남으면 문법 오류가 된다
            assert " Order " not in sql.replace("[Order]", "")


def test_non_dbo_schema_in_generated_sql():
    t = _table("Invoice", [
        _col("Id", "int", identity=True),
        _col("Amount", "decimal", scale=2),
        _col("CreatedAt", "datetime2", max_length=8),
    ], pk=["Id"], rows=100)
    t.schema = "sales"
    w = draft_workload({"db": {"sales.Invoice": t}}, max_tables=5)
    assert w["txns"], "스키마가 dbo가 아니면 트랜잭션이 생성되지 않았다"
    for txn in w["txns"]:
        for sql in txn["sql"]:
            assert "[sales].[Invoice]" in sql
        # ranges/ctx가 찾을 수 있도록 스키마 포함 참조여야 한다
        assert "sales.Invoice" in txn["tables"]


def test_composite_pk_produces_no_id_based_txn():
    # 복합 PK에 단일 값을 넣으면 조회가 항상 0행이고, 그것도 성공으로 집계된다
    t = _table("order_line", [
        _col("OrderId"), _col("LineNo"), _col("Qty"),
    ], pk=["OrderId", "LineNo"], rows=1000)
    assert t.single_pk is None
    assert t.numeric_single_pk is None
    w = draft_workload({"db": {"dbo.order_line": t}}, max_tables=5)
    assert not [x for x in w["txns"] if x["name"].endswith("_by_pk")]
    assert not [x for x in w["txns"] if x["name"].endswith("_update")]


def test_guid_pk_is_not_used_as_id_range():
    # uniqueidentifier PK에 정수를 넣으면 0행 조회가 된다
    t = _table("session", [
        _col("Id", "uniqueidentifier", max_length=16),
        _col("Token", "nvarchar", max_length=200),
    ], pk=["Id"], rows=1000)
    assert t.single_pk == "Id"
    assert t.numeric_single_pk is None      # 숫자형이 아니므로 제외
    w = draft_workload({"db": {"dbo.session": t}}, max_tables=5)
    assert not [x for x in w["txns"] if x["name"].endswith("_by_pk")]


def test_composite_fk_skipped_in_join_draft():
    parent = _table("order_line", [_col("OrderId"), _col("LineNo")],
                    pk=["OrderId", "LineNo"], rows=100)
    child = _table("shipment", [
        _col("Id", "int", identity=True), _col("OrderId"), _col("LineNo"),
        _col("CreatedAt", "datetime2", max_length=8),
    ], pk=["Id"], rows=100,
        fks=[ForeignKey("fk", ["OrderId", "LineNo"], "dbo.order_line",
                        ["OrderId", "LineNo"])])
    w = draft_workload({"db": {"dbo.order_line": parent, "dbo.shipment": child}},
                       max_tables=5)
    # 복합 FK 조인은 만들지 않는다 (값 조합을 맞출 수 없다)
    assert not [x for x in w["txns"] if "_join_" in x["name"]]


def test_generated_always_column_not_insertable():
    # temporal 테이블의 period 컬럼은 SQL Server가 직접 삽입을 거부한다
    c = _col("ValidFrom", "datetime2", max_length=8, generated=True)
    assert c.insertable is False


def test_rowversion_not_insertable():
    assert _col("V", "timestamp", max_length=8).insertable is False


def test_check_constraint_blocks_write_draft():
    # 합성값이 CHECK를 만족하지 못하면 매 시도가 실패한다 — 실패를 성능으로
    # 읽는 것보다 쓰기를 안 만드는 것이 낫다
    t = _table("payment", [
        _col("Id", "int", identity=True),
        _col("Amount", "decimal", scale=2),
        _col("CreatedAt", "datetime2", max_length=8),
    ], pk=["Id"], rows=1000, has_check=True)
    w = draft_workload({"db": {"dbo.payment": t}}, max_tables=5)
    assert w["write_count"] == 0
    assert w["read_count"] > 0      # 읽기는 여전히 만든다


def test_trigger_blocks_write_draft():
    t = _table("audit", [
        _col("Id", "int", identity=True),
        _col("Detail", "nvarchar", max_length=200),
        _col("CreatedAt", "datetime2", max_length=8),
    ], pk=["Id"], rows=1000, has_trigger=True)
    assert draft_workload({"db": {"dbo.audit": t}}, max_tables=5)["write_count"] == 0


def test_unique_column_not_chosen_as_update_target():
    # 유니크 컬럼에 임의값을 넣으면 중복 키 위반이 난다
    t = _table("account", [
        _col("Id", "int", identity=True),
        _col("Email", "nvarchar", max_length=200),
        _col("Nickname", "nvarchar", max_length=100),
        _col("CreatedAt", "datetime2", max_length=8),
    ], pk=["Id"], rows=1000,
        indexes=[Index("UQ_Email", ["Email"], [], True, False)])
    w = draft_workload({"db": {"dbo.account": t}}, max_tables=5)
    upd = [x for x in w["txns"] if x["name"].endswith("_update")]
    assert upd
    assert "[Email]" not in upd[0]["sql"][0]


def test_unreadable_types_excluded_from_select():
    # xml·geography는 pyodbc 변환에 실패하거나 페이로드가 과도하다
    t = _table("doc", [
        _col("Id", "int", identity=True),
        _col("Body", "xml", max_length=-1),
        _col("Loc", "geography", max_length=-1),
        _col("Title", "nvarchar", max_length=200),
        _col("CreatedAt", "datetime2", max_length=8),
    ], pk=["Id"], rows=1000)
    w = draft_workload({"db": {"dbo.doc": t}}, max_tables=5)
    for txn in w["txns"]:
        for sql in txn["sql"]:
            assert "[Body]" not in sql and "[Loc]" not in sql


def test_identity_only_table_gets_zero_rows():
    # 넣을 컬럼이 없으면 행수 배분에서도 빠져야 한다
    ident = _table("ticket", [_col("Id", "int", identity=True)], pk=["Id"])
    normal = _table("note", [_col("Id", "int", identity=True),
                             _col("Body", "nvarchar", max_length=200)], pk=["Id"])
    plan = draft_plan({"db": {"dbo.ticket": ident, "dbo.note": normal}},
                      total_rows=10_000)
    rows = {t["table"]: t["rows"] for t in plan["tables"]}
    assert rows["ticket"] == 0
    assert rows["note"] > 0
    tk = next(t for t in plan["tables"] if t["table"] == "ticket")
    assert any("삽입 가능한 컬럼이 없다" in w for w in tk["warnings"])


def test_insert_draft_includes_fk_parents_in_tables():
    # FK 부모가 tables에 없으면 id 범위가 조회되지 않아 error 547이 된다
    schema = _simple_schema()
    w = draft_workload({"db": schema}, max_tables=10)
    ins = [x for x in w["txns"] if x["name"] == "order_insert"]
    if ins:
        assert "dbo.account" in ins[0]["tables"]
