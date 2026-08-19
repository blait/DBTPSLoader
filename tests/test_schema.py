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


def _fk(name, cols, ref, ref_cols):
    """ForeignKey 생성 — ref는 "schema.table" 형식."""
    sch, _, nm = ref.partition(".")
    return ForeignKey(name=name, columns=list(cols), ref_schema=sch or "dbo",
                      ref_name=nm or ref, ref_columns=list(ref_cols))


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
        fks=[_fk("FK_order_account", ["AccountId"], "dbo.account", ["Id"])],
        indexes=[Index("IX_order_AccountId", ["AccountId"], [], False, False)])
    item = _table("order_item", [
        _col("Id", "int", identity=True),
        _col("OrderId"),
        _col("Qty"),
    ], pk=["Id"],
        fks=[_fk("FK_item_order", ["OrderId"], "dbo.order", ["Id"])])
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
               fks=[_fk("fk1", ["BId"], "dbo.b", ["Id"])])
    b = _table("b", [_col("Id", identity=True), _col("AId")], pk=["Id"],
               fks=[_fk("fk2", ["AId"], "dbo.a", ["Id"])])
    order = _topo_order({"dbo.a": a, "dbo.b": b})
    assert sorted(order) == ["dbo.a", "dbo.b"]     # 둘 다 포함, 순서는 임의


def test_topo_order_self_reference():
    t = _table("node", [_col("Id", identity=True), _col("ParentId", nullable=True)],
               pk=["Id"], fks=[_fk("fk", ["ParentId"], "dbo.node", ["Id"])])
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


def test_draft_plan_blocks_trigger_and_check():
    # CHECK 제약 위반은 5000행 배치를 통째로 죽인다. 트리거는 삽입 결과를
    # 예측할 수 없게 만든다. 둘 다 0행으로 두고 사유를 남긴다.
    schema = _simple_schema()
    schema["dbo.order"].has_trigger = True
    schema["dbo.order"].has_check = True
    plan = draft_plan({"db": schema}, total_rows=10_000)
    o = next(t for t in plan["tables"] if t["table"] == "order")
    assert o["rows"] == 0
    assert any("트리거" in b for b in o["blockers"])
    assert any("CHECK" in b for b in o["blockers"])
    assert any(x["table"] == "dbo.order" for x in plan["excluded"])


def test_draft_plan_blocks_ungeneratable_types():
    # 값을 만들 수 없는 타입에 NULL을 넣으면 NOT NULL 컬럼에서 배치가 죽는다
    t = _table("blob_tbl", [
        _col("Id", "int", identity=True),
        _col("Payload", "sql_variant", max_length=8016),
    ], pk=["Id"])
    plan = draft_plan({"db": {"dbo.blob_tbl": t}}, total_rows=10_000)
    e = plan["tables"][0]
    assert e["rows"] == 0
    assert any("값을 만들 수 없는 타입" in b for b in e["blockers"])


def test_draft_plan_blocks_composite_fk():
    # 복합 FK는 값 조합을 맞출 수 없어 고아 행이 남는다 (제약이 꺼진 상태로 삽입)
    parent = _table("po", [_col("A"), _col("B")], pk=["A", "B"])
    child = _table("po_line", [
        _col("Id", "int", identity=True), _col("A"), _col("B"),
    ], pk=["Id"], fks=[_fk("fk", ["A", "B"], "dbo.po", ["A", "B"])])
    plan = draft_plan({"db": {"dbo.po": parent, "dbo.po_line": child}},
                      total_rows=10_000)
    line = next(t for t in plan["tables"] if t["table"] == "po_line")
    assert line["rows"] == 0
    assert any("복합 FK" in b for b in line["blockers"])


def test_draft_plan_blocks_temporal():
    t = _table("acct", [
        _col("Id", "int", identity=True),
        _col("Name", "nvarchar", max_length=100),
    ], pk=["Id"], temporal=True)
    plan = draft_plan({"db": {"dbo.acct": t}}, total_rows=10_000)
    assert plan["tables"][0]["rows"] == 0


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
        fks=[_fk("fk", ["OrderId", "LineNo"], "dbo.order_line", ["OrderId", "LineNo"])])
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
    assert any("삽입 가능한 컬럼이 없다" in b for b in tk["blockers"])


def test_insert_draft_includes_fk_parents_in_tables():
    # FK 부모가 tables에 없으면 id 범위가 조회되지 않아 error 547이 된다
    schema = _simple_schema()
    w = draft_workload({"db": schema}, max_tables=10)
    ins = [x for x in w["txns"] if x["name"] == "order_insert"]
    if ins:
        assert "dbo.account" in ins[0]["tables"]


# ============================================================================
# 감사에서 나온 항목 — 조용히 잘못된 데이터가 가장 위험하다
# ============================================================================

from loadgen.schema.values import GENERATABLE, can_generate  # noqa: E402


def test_decimal_respects_precision():
    # decimal(5,4)의 최대는 9.9999다. precision을 무시하고 0~10000을 생성하면
    # 거의 모든 행이 "Arithmetic overflow"로 죽는다.
    g = Gen(seed=1)
    for prec, scale in ((5, 4), (5, 2), (9, 6), (18, 2), (38, 10)):
        c = _col("Amt", "decimal", precision=prec, scale=scale)
        limit = 10 ** (prec - scale)
        for i in range(200):
            v = value_for(c, g, i, 100, {})
            assert abs(v) < limit, f"decimal({prec},{scale}) 범위 초과: {v}"


def test_money_within_range():
    g = Gen(seed=1)
    c = _col("Price", "money", precision=19, scale=4)
    for i in range(100):
        assert abs(value_for(c, g, i, 100, {})) < 10**6


def test_unknown_type_raises_not_null():
    # NULL을 돌려주면 NOT NULL 컬럼에서 5000행 배치가 통째로 죽는다.
    # 예외를 내면 플랜 단계에서 걸러진다.
    c = _col("Weird", "some_clr_type", max_length=8)
    assert not can_generate(c)
    try:
        value_for(c, Gen(seed=1), 1, 10, {})
        assert False, "미지원 타입인데 예외가 나지 않았다"
    except ValueError:
        pass


def test_generatable_types_never_return_none_when_not_null():
    # NOT NULL 컬럼에 NULL이 가면 배치가 죽는다
    g = Gen(seed=7)
    for t in sorted(GENERATABLE):
        c = _col("C", t, max_length=100, precision=18, scale=4, nullable=False)
        v = value_for(c, g, 5, 100, {})
        assert v is not None, f"{t}에서 None이 나왔다"


def test_time_column_has_cardinality():
    # 상수를 넣으면 이 컬럼 인덱스의 선택도가 0이 된다
    c = _col("At", "time", max_length=5)
    g = Gen(seed=1)
    vals = {value_for(c, g, i, 100, {}) for i in range(100)}
    assert len(vals) > 50


def test_varbinary_max_is_not_one_byte():
    # 1바이트만 넣으면 행 크기가 실제와 자릿수 단위로 달라져 페이지 밀도가 어긋난다
    c = _col("Blob", "varbinary", max_length=-1)
    v = value_for(c, Gen(seed=1), 1, 10, {})
    assert isinstance(v, bytes) and len(v) > 100


def test_datetimeoffset_is_tz_aware():
    c = _col("At", "datetimeoffset", max_length=10)
    v = value_for(c, Gen(seed=1), 1, 10, {})
    assert v.tzinfo is not None


def test_unique_text_has_no_duplicates():
    # g.name()은 조합이 1225개뿐 — 1만 행이면 99%가 중복이다. 삽입 중에는 제약이
    # 꺼져 있어 중복이 그대로 들어가고 인덱스 선택도가 왜곡된다.
    t = _table("account", [
        _col("Id", "int", identity=True),
        _col("Nickname", "nvarchar", max_length=100),
    ], pk=["Id"], indexes=[Index("UQ_Nick", ["Nickname"], [], True, False)])
    f = make_factory(t, ["Nickname"])
    g = Gen(seed=1)
    vals = [f(g, i, 10_000, {})[0] for i in range(1, 10_001)]
    assert len(set(vals)) == len(vals), f"중복 {len(vals) - len(set(vals))}건"


def test_non_identity_int_pk_is_sequential():
    # 난수를 쓰면 100만 행에서 37%가 PK 충돌이다
    t = _table("code", [_col("Id"), _col("Label", "nvarchar", max_length=50)],
               pk=["Id"])
    f = make_factory(t, ["Id", "Label"])
    g = Gen(seed=1)
    ids = [f(g, i, 100_000, {})[0] for i in range(1, 100_001)]
    assert len(set(ids)) == len(ids)


def test_ctx_key_is_schema_qualified():
    # archive.Invoice(1000만) 와 sales.Invoice(120) 가 충돌하면 좁은 범위가 이겨
    # 큰 테이블 조회가 전부 캐시에서 처리되고 TPS만 좋아 보인다
    child = _table("line", [_col("Id", "int", identity=True), _col("InvId")],
                   pk=["Id"], fks=[_fk("fk", ["InvId"], "archive.Invoice", ["Id"])])
    f = make_factory(child, ["InvId"])
    g = Gen(seed=1)
    ctx = {"archive.invoice": 10_000_000, "sales.invoice": 120}
    vals = [f(g, i, 1000, ctx)[0] for i in range(1, 500)]
    assert max(vals) > 120, "좁은 스키마의 범위를 썼다"


def test_fk_ref_table_handles_dotted_name():
    # 이름 자체에 점이 있으면 합친 문자열을 rpartition으로 되쪼갤 수 없다
    fk = ForeignKey(name="fk", columns=["X"], ref_schema="dbo",
                    ref_name="My.Table", ref_columns=["Id"])
    assert fk.ref_schema == "dbo" and fk.ref_name == "My.Table"


def test_filtered_index_not_used_as_seek_key():
    # 필터 인덱스를 평범한 seek 키로 쓰면 임의 파라미터가 필터를 벗어나 스캔이 된다
    t = _table("task", [
        _col("Id", "int", identity=True),
        _col("Status", "nvarchar", max_length=20),
        _col("CreatedAt", "datetime2", max_length=8),
    ], pk=["Id"], rows=1000,
        indexes=[Index("IX_Active", ["Status"], [], False, False, filtered=True)])
    w = draft_workload({"db": {"dbo.task": t}}, max_tables=5)
    assert not [x for x in w["txns"] if "_by_status" in x["name"]]


def test_collation_difference_detected():
    # CI_AS 와 CS_AS 는 비교 의미·인덱스 선택도·정렬 비용이 다르다 — 측정 대상이다
    from loadgen.schema.guard import compare_schemas
    a = _table("t", [_col("Name", "nvarchar", max_length=100,
                          collation="SQL_Latin1_General_CP1_CI_AS")])
    b = _table("t", [_col("Name", "nvarchar", max_length=100,
                          collation="SQL_Latin1_General_CP1_CS_AS")])
    r = compare_schemas({"dbo.t": a}, {"dbo.t": b})
    assert not r["same"]
    assert "dbo.t" in r["differing"]


def test_draft_workload_uses_plan_rows_when_empty_db():
    # 빈 DB에서는 row_count가 전부 0이라 정렬이 무의미하고 알파벳 순으로 뽑힌다
    schema = _simple_schema()
    for t in schema.values():
        t.row_count = 0
    plan = draft_plan({"db": schema}, total_rows=1_000_000)
    w = draft_workload({"db": schema}, max_tables=1, plan=plan)
    # audit_log가 가장 큰 몫을 받으므로 그것이 선택돼야 한다
    assert all("audit_log" in t["name"] for t in w["txns"]), \
        [t["name"] for t in w["txns"]]


def test_draft_workload_warns_without_plan():
    w = draft_workload({"db": _simple_schema()}, max_tables=2)
    assert any("플랜" in x for x in w["warnings"])


# ============================================================================
# 드라이런 — 부하 전에 SQL이 동작하는지 확인한다
# ============================================================================

from loadgen.workload.dryrun import _plan_verdict  # noqa: E402


def test_plan_verdict_detects_scan():
    # 스캔만 쓰는 조회는 작은 테이블에서는 빠르지만 실규모에서 전혀 다르다
    v, note = _plan_verdict('<ShowPlanXML><Table Scan /></ShowPlanXML>')
    assert v == "scan" and "Table Scan" in note


def test_plan_verdict_detects_seek():
    v, _ = _plan_verdict('<ShowPlanXML><Index Seek /></ShowPlanXML>')
    assert v == "seek"


def test_plan_verdict_mixed():
    v, _ = _plan_verdict('<ShowPlanXML><Index Seek /><Table Scan /></ShowPlanXML>')
    assert v == "mixed"


def test_plan_verdict_no_plan():
    assert _plan_verdict("")[0] == "unknown"


def test_dryrun_reports_param_failure_without_db():
    # id 범위가 없으면 파라미터 생성부터 실패한다. 연결 전에 걸려야 한다 —
    # 시딩을 건너뛴 상태가 여기서 드러난다.
    from loadgen.config import TargetDB
    from loadgen.workload.dryrun import dryrun
    wl = {"name": "t", "txns": [{
        "name": "x", "kind": "read", "weight": 1, "database": "db",
        "tables": ["dbo.t"], "sql": ["SELECT 1 FROM dbo.t WHERE Id = ?"],
        "params": [[{"gen": "skewed_id", "of": "dbo.t"}]],
    }]}
    t = TargetDB(label="none", host="127.0.0.1", port=1, password="x",
                 login_timeout=1)
    r = dryrun(t, wl, ctx={})       # ctx 비어 있음
    assert r["ready"] is False
    assert r["results"][0]["status"] == "error"
    assert "파라미터 생성 실패" in r["results"][0]["error"]


# ============================================================================
# 실제 RDS에서 발견한 버그 — 로컬 테스트로는 안 잡혔다
# ============================================================================

def test_composite_pk_last_column_is_sequential():
    # 복합 PK의 각 컬럼이 난수면 조합이 충돌한다. 실제 RDS에서 PK_OrderLine
    # 위반으로 배치가 죽었다.
    t = _table("order_line", [
        _col("OrderId", "bigint"), _col("LineNo"), _col("Sku", "nvarchar", max_length=80),
    ], pk=["OrderId", "LineNo"])
    f = make_factory(t, ["OrderId", "LineNo", "Sku"])
    g = Gen(seed=1)
    keys = [tuple(f(g, i, 6000, {})[:2]) for i in range(1, 6001)]
    assert len(set(keys)) == len(keys), f"복합 PK 중복 {len(keys)-len(set(keys))}건"


def test_narrow_int_pk_stays_in_range():
    # tinyint(0~255) PK에 300행을 요청하면 "Numeric value out of range"로 죽는다
    t = _table("currency", [_col("Id", "tinyint", max_length=1),
                            _col("Code", "char", max_length=3)], pk=["Id"])
    f = make_factory(t, ["Id", "Code"])
    g = Gen(seed=1)
    vals = [f(g, i, 400, {})[0] for i in range(1, 401)]
    assert max(vals) <= 255, f"tinyint 범위 초과: {max(vals)}"


def test_plan_caps_rows_by_pk_type():
    # 값 생성이 되접기로 버티더라도 유일성이 깨진다. 플랜에서 막는 것이 정답이다.
    cur = _table("currency", [_col("Id", "tinyint", max_length=1),
                              _col("Code", "char", max_length=3)], pk=["Id"])
    log = _table("log", [_col("Id", "bigint", identity=True),
                         _col("Msg", "nvarchar", max_length=200),
                         _col("At", "datetime2", max_length=8)], pk=["Id"])
    plan = draft_plan({"db": {"dbo.currency": cur, "dbo.log": log}},
                      total_rows=500_000)
    c = next(t for t in plan["tables"] if t["table"] == "currency")
    assert c["rows"] <= 255
    assert any("PK 타입 최대값" in w for w in c["warnings"])
    # IDENTITY PK는 SQL Server가 관리하므로 제한하지 않는다
    lg = next(t for t in plan["tables"] if t["table"] == "log")
    assert lg["rows"] > 255


def test_decimal_stays_realistic():
    # precision만 보면 decimal(18,2)가 1조를 넘는 값을 만든다. 문법상 유효하지만
    # 비현실적이고, 큰 정수부는 pyodbc 변환에서 "out of range"가 된다.
    g = Gen(seed=1)
    for prec, scale in ((18, 2), (12, 2), (38, 10)):
        c = _col("Amt", "decimal", precision=prec, scale=scale)
        mx = max(value_for(c, g, i, 100, {}) for i in range(300))
        assert mx < 10_000_000, f"decimal({prec},{scale})가 {mx:,.0f}를 만들었다"


def test_insert_param_respects_decimal_precision():
    # decimal(5,4)에 max 10000을 넣으면 매 INSERT가 산술 오버플로다
    from loadgen.workload.draft import _param_for
    spec = _param_for(_col("Rate", "decimal", precision=5, scale=4))
    assert spec["max"] < 10.0 and spec["q"] == 4


def test_insert_param_unique_column_uses_varying_gen():
    # 유니크 컬럼에 email 생성기를 쓰면 중복 키 위반이 쌓인다
    from loadgen.workload.draft import _param_for
    c = _col("Email", "nvarchar", max_length=400)
    assert _param_for(c, unique=False)["gen"] == "email"
    assert _param_for(c, unique=True)["gen"] == "token"


def test_insert_param_none_for_unsupported_not_null():
    # NOT NULL time 컬럼에 NULL을 넣으면 23000으로 배치가 죽는다
    from loadgen.workload.draft import _param_for
    assert _param_for(_col("AtTime", "time", max_length=5, nullable=False)) is None
    assert _param_for(_col("AtTime", "time", max_length=5, nullable=True)) is not None


def test_insert_draft_skipped_when_not_null_unsupported():
    # 값을 만들 수 없는 NOT NULL 컬럼이 있으면 INSERT를 만들지 않는다
    t = _table("audit", [
        _col("Id", "bigint", identity=True),
        _col("AtTime", "time", max_length=5),      # NOT NULL, 생성 불가
        _col("CreatedAt", "datetime2", max_length=8),
    ], pk=["Id"], rows=1000)
    w = draft_workload({"db": {"dbo.audit": t}}, max_tables=5)
    assert not [x for x in w["txns"] if x["name"].endswith("_insert")]
