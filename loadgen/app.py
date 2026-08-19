"""FastAPI control plane for the load generator.

Run:  uvicorn loadgen.app:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               RedirectResponse)
from pydantic import BaseModel

from . import comparisons, presets
from .analysis import build_report, scan_runs
from .config import RunConfig, SeedConfig, TargetDB
from .db import connect
from .metrics.export import RUNS_DIR, list_runs, load_run
from .rds_facts import facts_for_rows, vcpu_warnings
from .report_md import render_markdown
from .runner.coordinator import Run
from .schema import guard, introspect as schema_introspect
from .schema.plan import draft_plan, load_plan, plan_names, save_plan
from .schema.values import attach_factories
from .schema.verify import compare_data
from .seed.seeder import seed_plan
from .workload import store as workload_store
from .workload.draft import draft_workload

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = FastAPI(title="mssql HT PoC loadgen")

STATIC = Path(__file__).parent / "static"

# ---------------------------------------------------------------- app state

state: dict = {
    "targets": {},          # label -> TargetDB
    "run": None,            # active Run
    "seed_status": None,    # dict updated by seed thread
    "clients": set(),       # per-websocket asyncio.Queue
    "loop": None,
}


def _emit(kind: str, data: dict) -> None:
    """Thread-safe broadcast of an event to all websocket clients."""
    loop = state.get("loop")
    if not loop:
        return
    evt = {"kind": kind, **data}

    def _push():
        for q in state["clients"]:
            if q.qsize() < 1000:
                q.put_nowait(evt)

    loop.call_soon_threadsafe(_push)


@app.on_event("startup")
async def _startup():
    state["loop"] = asyncio.get_running_loop()
    _load_targets_file()


def _load_targets_file() -> None:
    """Pre-register targets from LOADGEN_TARGETS_FILE (JSON list of TargetDB)."""
    path = os.environ.get("LOADGEN_TARGETS_FILE")
    if not path or not Path(path).exists():
        return
    for item in json.loads(Path(path).read_text()):
        t = TargetDB(**item)
        state["targets"][t.label] = t
        log.info("pre-registered target %s (%s)", t.label, t.host)


# ------------------------------------------------------------ 패스워드 게이트
#
# 이 도구는 DB에 쓰기 부하를 거는 도구다. 인증 없이 네트워크에 열면 누구나
# 대상 DB에 부하를 걸 수 있다.
#
# 패스워드가 없으면 **루프백에서만** 동작한다. 원래는 패스워드 미설정 시 인증이
# 통째로 비활성됐는데, 환경변수 하나를 빼먹으면 완전히 열리는 구조였다.
# 편의(로컬 개발)와 안전(외부 노출 차단)을 바인딩 주소로 구분한다.

GATE_PASSWORD = os.environ.get("LOADGEN_PASSWORD", "")
SESSION_TTL = int(os.environ.get("LOADGEN_SESSION_TTL", 8 * 3600))
# 쿠키에 Secure를 붙일지. TLS 종단 뒤에 두면 1로 설정한다.
COOKIE_SECURE = os.environ.get("LOADGEN_COOKIE_SECURE", "0") == "1"

_sessions: dict[str, float] = {}       # 토큰 -> 만료 epoch
_login_fails: dict[str, list[float]] = {}   # IP -> 최근 실패 시각
_LOGIN_WINDOW, _LOGIN_MAX = 300.0, 10      # 5분에 10회

_LOOPBACK = {"127.0.0.1", "::1", "localhost"}

LOGIN_HTML = """<!DOCTYPE html><html lang="ko"><head><meta charset="utf-8"><title>loadgen 로그인</title>
<style>body{background:#0f1419;color:#d8dee6;font:15px sans-serif;display:flex;align-items:center;justify-content:center;height:100vh}
form{background:#1a2129;border:1px solid #2c3742;border-radius:8px;padding:32px;width:300px}
input{width:100%;padding:8px;margin:8px 0;background:#0d1116;color:#d8dee6;border:1px solid #2c3742;border-radius:5px;box-sizing:border-box}
button{width:100%;padding:9px;background:#4da3ff;color:#fff;border:0;border-radius:5px;cursor:pointer}
p{font-size:12px;color:#5c6773}</style></head>
<body><form method="post" action="/login"><h3>mssql-loadgen</h3>
<input type="password" name="password" placeholder="패스워드" autofocus>
<button>로그인</button>__MSG__</form></body></html>"""

BLOCKED_HTML = """<!DOCTYPE html><html lang="ko"><head><meta charset="utf-8"><title>loadgen</title>
<style>body{background:#0f1419;color:#d8dee6;font:15px sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;text-align:center}
div{max-width:520px;padding:28px;background:#1a2129;border:1px solid #e5534b;border-radius:8px}
code{background:#0d1116;padding:2px 6px;border-radius:4px}</style></head>
<body><div><h3>인증이 설정되지 않았다</h3>
<p>이 도구는 대상 DB에 쓰기 부하를 걸 수 있으므로, 루프백이 아닌 주소에서는
패스워드 없이 사용할 수 없다.</p>
<p><code>LOADGEN_PASSWORD</code> 환경변수를 설정해 다시 시작하거나,
<code>127.0.0.1</code>로 접속할 것.</p></div></body></html>"""


def _peer_ip(request: Request) -> str:
    return request.client.host if request.client else ""


def _is_loopback(request: Request) -> bool:
    """리버스 프록시 헤더를 신뢰하지 않는다 — 위조하면 인증을 우회할 수 있다.

    TCP 피어 주소만 본다. 프록시 뒤에 둘 경우 피어는 프록시가 되므로 루프백으로
    보일 수 있는데, 그런 배치에서는 패스워드를 설정하는 것이 전제다.
    """
    return _peer_ip(request) in _LOOPBACK


def _prune_sessions(now: float) -> None:
    """만료된 세션을 지운다. 원래는 제거 경로가 없어 무한히 쌓였다."""
    for tok in [t for t, exp in _sessions.items() if exp <= now]:
        _sessions.pop(tok, None)


def _authed(request: Request) -> bool:
    now = time.time()
    _prune_sessions(now)
    tok = request.cookies.get("loadgen_session", "")
    return bool(tok) and _sessions.get(tok, 0) > now


@app.middleware("http")
async def gate(request: Request, call_next):
    path = request.url.path
    if path in ("/login", "/favicon.ico", "/healthz"):
        return await call_next(request)

    if not GATE_PASSWORD:
        # 패스워드가 없으면 루프백만 허용한다. fail-open이 아니라 fail-closed다.
        if _is_loopback(request):
            return await call_next(request)
        log.warning("인증 미설정 상태에서 외부 접근 차단: %s", _peer_ip(request))
        if path.startswith("/api"):
            return JSONResponse(
                {"detail": "LOADGEN_PASSWORD가 설정되지 않았다 — 루프백에서만 사용 가능"},
                status_code=403)
        return HTMLResponse(BLOCKED_HTML, status_code=403)

    if not _authed(request):
        if path.startswith("/api"):
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
        # 미인증 페이지에 200을 주면 캐시·모니터가 정상으로 오인한다.
        return HTMLResponse(LOGIN_HTML.replace("__MSG__", ""), status_code=401)
    return await call_next(request)


@app.get("/healthz")
async def healthz():
    """인증 없이 접근 가능한 상태 확인 — 컨테이너 헬스체크용."""
    return {"ok": True, "auth": "password" if GATE_PASSWORD else "loopback-only"}


@app.post("/login")
async def login(request: Request):
    ip = _peer_ip(request)
    now = time.time()
    # 실패 횟수 제한. 공유 패스워드 하나를 무제한으로 추측하게 두면 안 된다.
    fails = [t for t in _login_fails.get(ip, []) if now - t < _LOGIN_WINDOW]
    if len(fails) >= _LOGIN_MAX:
        _login_fails[ip] = fails
        log.warning("로그인 시도 제한 초과: %s", ip)
        return HTMLResponse(
            LOGIN_HTML.replace("__MSG__", "<p>시도가 너무 많다. 잠시 후 다시 시도할 것.</p>"),
            status_code=429)

    if not GATE_PASSWORD:
        return HTMLResponse(BLOCKED_HTML, status_code=403)

    form = await request.form()
    if hmac.compare_digest(str(form.get("password", "")), GATE_PASSWORD):
        _login_fails.pop(ip, None)
        token = secrets.token_urlsafe(32)
        _prune_sessions(now)
        _sessions[token] = now + SESSION_TTL
        log.info("로그인 성공: %s", ip)
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie("loadgen_session", token, httponly=True, samesite="lax",
                        secure=COOKIE_SECURE, max_age=SESSION_TTL)
        return resp

    fails.append(now)
    _login_fails[ip] = fails
    log.warning("로그인 실패: %s (%d/%d)", ip, len(fails), _LOGIN_MAX)
    return HTMLResponse(
        LOGIN_HTML.replace("__MSG__", "<p>패스워드가 맞지 않다.</p>"), status_code=401)


@app.post("/logout")
async def logout(request: Request):
    tok = request.cookies.get("loadgen_session", "")
    _sessions.pop(tok, None)
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie("loadgen_session")
    return resp


# ------------------------------------------------------------------ targets

class TargetIn(BaseModel):
    label: str
    host: str
    port: int = 1433
    user: str = "sa"
    password: str
    encrypt: bool = True
    trust_server_certificate: bool = True


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/targets")
async def get_targets():
    return [
        {"label": t.label, "host": t.host, "port": t.port, "user": t.user}
        for t in state["targets"].values()
    ]


@app.post("/api/targets")
async def add_target(t: TargetIn):
    target = TargetDB(**t.model_dump())
    state["targets"][t.label] = target
    return {"ok": True}


@app.post("/api/targets/{label}/test")
async def test_target(label: str):
    target = state["targets"].get(label)
    if not target:
        raise HTTPException(404, "unknown target")

    def _test():
        with connect(target, "master") as conn:
            cur = conn.cursor()
            cur.execute("SELECT @@VERSION, SERVERPROPERTY('MachineName')")
            row = cur.fetchone()
            return {"version": row[0][:120]}

    try:
        return await asyncio.to_thread(_test)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e)[:300])


# ------------------------------------------------------------------- schema

def _introspect_all(target: TargetDB, databases: list[str]) -> dict:
    return {db: schema_introspect.introspect(target, db) for db in databases}


def _databases_of(target: TargetDB, given: Optional[list[str]]) -> list[str]:
    """조회 대상 DB 목록. 지정이 없으면 서버의 사용자 DB 전부."""
    if given:
        return given
    def _list():
        with connect(target, "master") as conn:
            cur = conn.cursor()
            cur.execute("SELECT name FROM sys.databases WHERE database_id > 4 "
                        "AND state = 0 ORDER BY name")
            return [r[0] for r in cur.fetchall()]
    return _list()


@app.get("/api/schema/{label}")
async def get_schema(label: str, databases: Optional[str] = None):
    """대상 DB의 스키마 + 전제조건 검사 결과."""
    target = state["targets"].get(label)
    if not target:
        raise HTTPException(404, "unknown target")
    dbs = [d for d in (databases or "").split(",") if d] or None

    def _work():
        names = _databases_of(target, dbs)
        tables = _introspect_all(target, names)
        state.setdefault("schemas", {})[label] = tables
        return {
            "label": label,
            "databases": names,
            "guard": guard.check_empty(target, names, tables=tables),
            **{db: schema_introspect.to_dict(t) for db, t in tables.items()},
        }

    try:
        return await asyncio.to_thread(_work)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e)[:300])


class CompareIn(BaseModel):
    a: str
    b: str
    databases: Optional[list[str]] = None


@app.post("/api/schema/compare")
async def compare_schema(body: CompareIn):
    """두 대상의 스키마 대조 — 쌍 비교는 스키마가 같아야 성립한다."""
    ta, tb = state["targets"].get(body.a), state["targets"].get(body.b)
    if not ta or not tb:
        raise HTTPException(404, "unknown target")

    def _work():
        names = _databases_of(ta, body.databases)
        a_all = _introspect_all(ta, names)
        b_all = _introspect_all(tb, names)
        flat = lambda d: {f"{db}|{k}": v for db, t in d.items() for k, v in t.items()}  # noqa: E731
        return guard.compare_schemas(flat(a_all), flat(b_all), ta.label, tb.label)

    try:
        return await asyncio.to_thread(_work)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e)[:300])


# --------------------------------------------------------------- seed plan

class PlanDraftIn(BaseModel):
    label: str
    total_rows: int = 1_000_000
    databases: Optional[list[str]] = None
    name: str = "draft"


@app.post("/api/plan/draft")
async def plan_draft(body: PlanDraftIn):
    """스키마 조회 → 시딩 플랜 초안. 사용자가 UI에서 행수를 고친 뒤 저장한다."""
    target = state["targets"].get(body.label)
    if not target:
        raise HTTPException(404, "unknown target")

    def _work():
        names = _databases_of(target, body.databases)
        tables = _introspect_all(target, names)
        state.setdefault("schemas", {})[body.label] = tables
        return draft_plan(tables, total_rows=body.total_rows, name=body.name)

    try:
        return await asyncio.to_thread(_work)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e)[:300])


@app.get("/api/plans")
async def list_plans():
    return plan_names()


@app.get("/api/plan/{name}")
async def get_plan(name: str):
    try:
        return load_plan(name)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))


@app.put("/api/plan/{name}")
async def put_plan(name: str, plan: dict):
    plan["name"] = name
    plan["edited"] = True   # 사용자가 손댔음을 기록 — 리포트가 이 사실을 표시한다
    save_plan(plan)
    return {"ok": True, "total_rows_planned": sum(
        t.get("rows", 0) for t in plan.get("tables", []))}


# --------------------------------------------------------------------- seed

class SeedIn(BaseModel):
    label: str
    plan_name: str
    workers: int = 4
    batch_size: int = 5000


@app.post("/api/seed")
async def start_seed(s: SeedIn):
    """시딩 플랜을 실행한다. 스키마·DB 생성은 도구 밖 — 사용자가 미리 준비한다."""
    target = state["targets"].get(s.label)
    if not target:
        raise HTTPException(404, "unknown target")
    if state["seed_status"] and state["seed_status"].get("status") == "running":
        raise HTTPException(409, "seed already running")
    try:
        plan = load_plan(s.plan_name)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))

    status = {"status": "running", "phase": "prepare", "label": s.label,
              "plan": s.plan_name, "tables": {}, "errors": []}
    state["seed_status"] = status

    def progress(table: str, done: int, total: int):
        status["tables"][table] = {"done": done, "total": total}
        _emit("seed", {"table": table, "done": done, "total": total})

    def runner():
        try:
            # 팩토리는 JSON에 담을 수 없어 플랜 파일에 없다. 실행 시점에 스키마를
            # 다시 조회해 조립한다 — 그 사이 스키마가 바뀌었으면 여기서 드러난다.
            status["phase"] = "introspect"
            _emit("seed_phase", {"phase": "introspect"})
            tables = _introspect_all(target, plan["databases"])
            runnable = attach_factories(plan, tables)

            status["phase"] = "data"
            _emit("seed_phase", {"phase": "data"})
            cfg = SeedConfig(batch_size=s.batch_size, workers=s.workers)
            status["summary"] = seed_plan(target, runnable, cfg, progress=progress)
            status["status"] = "finished"
        except Exception as e:  # noqa: BLE001
            log.exception("seed failed")
            status["status"] = "failed"
            status["errors"].append(str(e)[:500])
        _emit("seed_phase", {"phase": status["status"]})

    threading.Thread(target=runner, daemon=True).start()
    return {"ok": True}


class VerifyIn(BaseModel):
    a: str
    b: str
    plan_name: str


@app.post("/api/seed/verify")
async def verify_seed(body: VerifyIn):
    """시딩 후 두 대상의 데이터가 같은지 대조 — 쌍 비교의 전제."""
    ta, tb = state["targets"].get(body.a), state["targets"].get(body.b)
    if not ta or not tb:
        raise HTTPException(404, "unknown target")
    try:
        plan = load_plan(body.plan_name)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    try:
        return await asyncio.to_thread(compare_data, ta, tb, plan)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e)[:300])


# ----------------------------------------------------------------- workload

class WorkloadDraftIn(BaseModel):
    label: str
    databases: Optional[list[str]] = None
    name: str = "draft"
    max_tables: int = 12


@app.post("/api/workload/draft")
async def workload_draft(body: WorkloadDraftIn):
    """스키마 → 트랜잭션 믹스 초안. 사용자가 가중치·SQL을 고친 뒤 저장한다."""
    target = state["targets"].get(body.label)
    if not target:
        raise HTTPException(404, "unknown target")

    def _work():
        names = _databases_of(target, body.databases)
        tables = _introspect_all(target, names)
        return draft_workload(tables, name=body.name, max_tables=body.max_tables)

    try:
        return await asyncio.to_thread(_work)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e)[:300])


@app.get("/api/workloads")
async def list_workloads():
    return workload_store.list_names()


@app.get("/api/workload/{name}")
async def get_workload(name: str):
    try:
        return workload_store.load(name)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))


@app.put("/api/workload/{name}")
async def put_workload(name: str, workload: dict):
    workload["name"] = name
    workload["edited"] = True
    # 저장 전에 조립해본다 — 파라미터 개수가 SQL과 맞지 않으면 여기서 걸린다.
    try:
        workload_store.build_mix(workload)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"워크로드가 유효하지 않다: {e}")
    workload_store.save(workload)
    return {"ok": True}


@app.get("/api/seed/status")
async def seed_status():
    return state["seed_status"] or {"status": "idle"}


# --------------------------------------------------------------------- runs

class RunIn(BaseModel):
    """기본값은 `loadgen.presets.RUN_DEFAULTS` 하나에서만 온다 (중복 정의 금지)."""

    label: str
    workload_name: str
    mode: str = presets.RUN_DEFAULTS["mode"]
    duration_sec: int = presets.RUN_DEFAULTS["duration_sec"]
    warmup_sec: int = presets.RUN_DEFAULTS["warmup_sec"]
    processes: int = presets.RUN_DEFAULTS["processes"]
    threads_per_process: int = presets.RUN_DEFAULTS["threads_per_process"]
    target_tps: Optional[int] = presets.RUN_DEFAULTS["target_tps"]
    read_pct: int = presets.RUN_DEFAULTS["read_pct"]
    note: str = ""


@app.get("/api/presets")
async def run_presets():
    """Run 폼 기본값 + 사용자가 정의한 비교 쌍 (comparisons.yaml)."""
    cfg = comparisons.load()
    return {
        "defaults": presets.RUN_DEFAULTS,
        "pairs": comparisons.pairs(cfg),
        "gates": cfg["gates"],
        "cpu": cfg["cpu"],
        "config_path": cfg.get("_path"),
    }


class RunUpdate(BaseModel):
    read_pct: Optional[int] = None
    target_tps: Optional[int] = None


@app.post("/api/run")
async def start_run(r: RunIn):
    target = state["targets"].get(r.label)
    if not target:
        raise HTTPException(404, "unknown target")
    active: Run = state["run"]
    if active and active.status in ("running", "stopping"):
        raise HTTPException(409, "run already active")

    try:
        workload = workload_store.load(r.workload_name)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))

    cfg = RunConfig(**{k: v for k, v in r.model_dump().items()
                       if k not in ("label", "workload_name")})
    run = Run(target, cfg, workload=workload,
              on_point=lambda p: _emit("point", {"point": p}))
    state["run"] = run
    await asyncio.to_thread(run.start)

    def _watch(run_ref=run):
        import time as _time

        while run_ref.status != "finished":
            _time.sleep(2)
        _time.sleep(90)  # EM log delivery lags ~30-60s; wait before snapshotting
        snapshot_run_metrics(run_ref.run_id)

    threading.Thread(target=_watch, daemon=True).start()
    return {"run_id": run.run_id}


@app.post("/api/run/update")
async def update_run(u: RunUpdate):
    run: Run = state["run"]
    if not run or run.status != "running":
        raise HTTPException(404, "no active run")
    run.update(read_pct=u.read_pct, target_tps=u.target_tps)
    return {"ok": True}


@app.post("/api/run/stop")
async def stop_run():
    run: Run = state["run"]
    if not run:
        raise HTTPException(404, "no active run")
    run.stop()
    return {"ok": True}


@app.get("/api/run/status")
async def run_status():
    run: Run = state["run"]
    if not run:
        return {"status": "idle"}
    return run.snapshot()


@app.get("/api/runs")
async def runs():
    return list_runs()


@app.get("/api/runs/{run_id}")
async def run_detail(run_id: str):
    return load_run(run_id)


def _run_window(data: dict) -> tuple[float, float]:
    from datetime import datetime, timezone

    def _parse(s):
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()

    meta = data["meta"]
    start = _parse(meta["started_at_utc"]) - 60
    ended = data.get("summary", {}).get("ended_at_utc")
    if ended:
        end = _parse(ended) + 60
    else:
        cfg = meta.get("config", {})
        end = start + cfg.get("warmup_sec", 0) + cfg.get("duration_sec", 0) + 180
    return start, end


def _fetch_rds_metrics(host: str, start: float, end: float) -> dict:
    """All EM fields (5s) + connections (CloudWatch 60s) for a time window.
    Blocking — call via asyncio.to_thread."""
    import boto3

    db_id = host.split(".")[0]
    region = host.split(".rds.amazonaws.com")[0].split(".")[-1]
    rds = boto3.client("rds", region_name=region)
    inst = rds.describe_db_instances(DBInstanceIdentifier=db_id)["DBInstances"][0]
    stream = inst["DbiResourceId"]
    logs = boto3.client("logs", region_name=region)
    points, token = [], None
    while True:
        kwargs = dict(logGroupName="RDSOSMetrics", logStreamName=stream,
                      startTime=int(start * 1000), endTime=int(end * 1000),
                      startFromHead=True)
        if token:
            kwargs["nextToken"] = token
        resp = logs.get_log_events(**kwargs)
        for ev in resp["events"]:
            m = json.loads(ev["message"])
            cpu = m.get("cpuUtilization", {})
            mem = m.get("memory", {})
            disk = (m.get("disks") or [{}])[0]
            net = (m.get("network") or [{}])[0]
            points.append({
                "ts": ev["timestamp"] / 1000,
                "cpu_total": round(100 - cpu.get("idle", 100), 2),
                "cpu_user": cpu.get("user", 0),
                "cpu_kern": cpu.get("kern", 0),
                "disk_riops": disk.get("rdCountPS", 0),
                "disk_wiops": disk.get("wrCountPS", 0),
                "disk_rmbps": round(disk.get("rdBytesPS", 0) / 1048576, 2),
                "disk_wmbps": round(disk.get("wrBytesPS", 0) / 1048576, 2),
                "net_mbps": round((net.get("rdBytesPS", 0) + net.get("wrBytesPS", 0)) / 1048576, 2),
                "sql_mem_gb": round(mem.get("sqlServerTotKb", 0) / 1048576, 2),
                "threads": m.get("system", {}).get("threads", 0),
            })
        if resp.get("nextForwardToken") == token or not resp["events"]:
            break
        token = resp.get("nextForwardToken")
    points.sort(key=lambda p: p["ts"])
    # connections (CloudWatch, 60s)
    conns = []
    try:
        from datetime import datetime, timezone

        cw = boto3.client("cloudwatch", region_name=region)
        resp = cw.get_metric_data(
            StartTime=datetime.fromtimestamp(start, tz=timezone.utc),
            EndTime=datetime.fromtimestamp(end, tz=timezone.utc),
            ScanBy="TimestampAscending",
            MetricDataQueries=[{
                "Id": "conns",
                "MetricStat": {"Metric": {"Namespace": "AWS/RDS", "MetricName": "DatabaseConnections",
                                          "Dimensions": [{"Name": "DBInstanceIdentifier", "Value": db_id}]},
                               "Period": 60, "Stat": "Average"},
            }],
        )["MetricDataResults"][0]
        conns = [{"ts": t.timestamp(), "conns": round(v, 1)}
                 for t, v in zip(resp["Timestamps"], resp["Values"])]
    except Exception:  # noqa: BLE001
        pass
    return {"db_id": db_id, "points": points, "connections": conns}


def snapshot_run_metrics(run_id: str) -> None:
    """Persist the run's RDS metrics to runs/<id>/rds_metrics.json (EM logs
    expire after 30 days; the file makes the run folder self-contained)."""
    from .metrics.export import run_dir

    try:
        data = load_run(run_id)
        host = (data.get("meta") or {}).get("host", "")
        if ".rds.amazonaws.com" not in host:
            return
        start, end = _run_window(data)
        result = _fetch_rds_metrics(host, start, end)
        (run_dir(run_id) / "rds_metrics.json").write_text(json.dumps(result))
        log.info("rds_metrics.json saved for %s (%d pts)", run_id, len(result["points"]))
    except Exception as e:  # noqa: BLE001
        log.warning("rds metrics snapshot failed for %s: %s", run_id, e)


@app.get("/api/runs/{run_id}/enhanced")
async def run_enhanced(run_id: str):
    """RDS metrics for a past run — served from the persisted snapshot when
    available, else fetched live from CloudWatch (and cached to file)."""
    f = RUNS_DIR / run_id / "rds_metrics.json"
    if f.exists():
        return json.loads(f.read_text())
    data = load_run(run_id)
    meta = data.get("meta")
    if not meta:
        raise HTTPException(404, "no meta for run")
    host = meta.get("host", "")
    if ".rds.amazonaws.com" not in host:
        return {"points": [], "connections": []}
    start, end = _run_window(data)
    try:
        result = await asyncio.to_thread(_fetch_rds_metrics, host, start, end)
        if data.get("summary"):  # finished run -> cache to file
            f.write_text(json.dumps(result))
        return result
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e)[:300])


# ------------------------------------------------------------------- report

def _discarded_runs() -> list[dict]:
    """Runs quarantined into runs/_*/ subfolders, so exclusions stay visible."""
    out = []
    if not RUNS_DIR.exists():
        return out
    for q in sorted(p for p in RUNS_DIR.iterdir() if p.is_dir() and p.name.startswith("_")):
        for d in sorted(p for p in q.iterdir() if p.is_dir()):
            out.append({"run_id": d.name,
                        "skip": f"`{q.name}/`로 격리됨 — 집계 대상 아님"})
    return out


def _build(mode: Optional[str], run_ids: Optional[list[str]]) -> dict:
    rows, skipped = scan_runs(RUNS_DIR, mode=mode, run_ids=run_ids)
    facts = facts_for_rows(rows)
    report = build_report(rows, skipped, instances=facts,
                         discarded=_discarded_runs())
    report["warnings"] = vcpu_warnings(rows, facts)
    return report


@app.get("/api/report")
async def report(mode: Optional[str] = "open", runs: Optional[str] = None):
    """The HT comparison report, computed from run artifacts on disk.

    `runs`: optional comma-separated run ids to restrict the report to (the UI
    passes the checked rows). Omitted -> every run in runs/.
    """
    ids = [x for x in (runs or "").split(",") if x] or None
    return await asyncio.to_thread(_build, mode, ids)


@app.get("/api/report.md")
async def report_md(mode: Optional[str] = "open", runs: Optional[str] = None):
    ids = [x for x in (runs or "").split(",") if x] or None
    data = await asyncio.to_thread(_build, mode, ids)
    return HTMLResponse(render_markdown(data), media_type="text/markdown; charset=utf-8")


# --------------------------------------------------------------- cloudwatch

@app.get("/api/cloudwatch/{label}")
async def cloudwatch_metrics(label: str, minutes: int = 20):
    """Live RDS metrics for a target. Instance id = first hostname component
    (only works for *.rds.amazonaws.com targets; local Docker returns empty)."""
    target = state["targets"].get(label)
    if not target:
        raise HTTPException(404, "unknown target")
    if ".rds.amazonaws.com" not in target.host:
        return {"points": []}
    db_id = target.host.split(".")[0]

    def _fetch():
        from datetime import datetime, timedelta, timezone

        import boto3

        region = target.host.split(".rds.amazonaws.com")[0].split(".")[-1]
        cw = boto3.client("cloudwatch", region_name=region)
        now = datetime.now(timezone.utc)
        resp = cw.get_metric_data(
            StartTime=now - timedelta(minutes=minutes),
            EndTime=now,
            ScanBy="TimestampAscending",
            MetricDataQueries=[
                {
                    "Id": q_id,
                    "MetricStat": {
                        "Metric": {
                            "Namespace": "AWS/RDS",
                            "MetricName": metric,
                            "Dimensions": [{"Name": "DBInstanceIdentifier", "Value": db_id}],
                        },
                        "Period": 60,
                        "Stat": stat,
                    },
                }
                for q_id, metric, stat in [
                    ("cpu", "CPUUtilization", "Average"),
                    ("conns", "DatabaseConnections", "Average"),
                    ("readiops", "ReadIOPS", "Average"),
                    ("writeiops", "WriteIOPS", "Average"),
                ]
            ],
        )
        series = {r["Id"]: dict(zip([t.timestamp() for t in r["Timestamps"]], r["Values"]))
                  for r in resp["MetricDataResults"]}
        all_ts = sorted({ts for s in series.values() for ts in s})
        return {
            "db_id": db_id,
            "points": [
                {"ts": ts, **{k: round(series[k].get(ts, 0), 2) for k in series}}
                for ts in all_ts
            ],
        }

    try:
        return await asyncio.to_thread(_fetch)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e)[:300])


_em_cache: dict = {}  # label -> (resource_id, region)


@app.get("/api/enhanced/{label}")
async def enhanced_metrics(label: str, minutes: int = 10):
    """RDS Enhanced Monitoring OS metrics (5s granularity, near-realtime).
    Reads the RDSOSMetrics CloudWatch Logs stream for the instance."""
    target = state["targets"].get(label)
    if not target:
        raise HTTPException(404, "unknown target")
    if ".rds.amazonaws.com" not in target.host:
        return {"points": []}
    db_id = target.host.split(".")[0]

    def _fetch():
        import time as _time

        import boto3

        region = target.host.split(".rds.amazonaws.com")[0].split(".")[-1]
        if label not in _em_cache:
            rds = boto3.client("rds", region_name=region)
            inst = rds.describe_db_instances(DBInstanceIdentifier=db_id)["DBInstances"][0]
            _em_cache[label] = inst["DbiResourceId"]
        stream = _em_cache[label]
        logs = boto3.client("logs", region_name=region)
        now_ms = int(_time.time() * 1000)
        events = logs.get_log_events(
            logGroupName="RDSOSMetrics", logStreamName=stream,
            startTime=now_ms - minutes * 60_000, endTime=now_ms,
            startFromHead=False, limit=int(minutes * 60 / 5) + 20,
        )["events"]
        points = []
        for ev in events:
            m = json.loads(ev["message"])
            cpu = m.get("cpuUtilization", {})
            mem = m.get("memory", {})
            disk = (m.get("disks") or [{}])[0]  # rdsdbdata volume
            net = (m.get("network") or [{}])[0]
            points.append({
                "ts": ev["timestamp"] / 1000,
                # RDS SQL Server EM is Windows: user + kern (no iowait/loadavg)
                "cpu_total": round(100 - cpu.get("idle", 100), 2),
                "cpu_user": cpu.get("user", 0),
                "cpu_kern": cpu.get("kern", 0),
                "sql_mem_gb": round(mem.get("sqlServerTotKb", 0) / 1024 / 1024, 2),
                "mem_avail_gb": round(mem.get("physAvailKb", 0) / 1024 / 1024, 1),
                "disk_riops": disk.get("rdCountPS", 0),
                "disk_wiops": disk.get("wrCountPS", 0),
                "disk_rmbps": round(disk.get("rdBytesPS", 0) / 1024 / 1024, 2),
                "disk_wmbps": round(disk.get("wrBytesPS", 0) / 1024 / 1024, 2),
                "net_mbps": round((net.get("rdBytesPS", 0) + net.get("wrBytesPS", 0)) / 1024 / 1024, 2),
                "threads": m.get("system", {}).get("threads", 0),
            })
        points.sort(key=lambda p: p["ts"])
        return {"db_id": db_id, "points": points}

    try:
        return await asyncio.to_thread(_fetch)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e)[:300])


# ---------------------------------------------------------------- websocket

@app.websocket("/ws")
async def ws(sock: WebSocket):
    # 미들웨어는 WebSocket 핸드셰이크를 덮지 않으므로 여기서 다시 검사한다.
    if GATE_PASSWORD:
        tok = sock.cookies.get("loadgen_session", "")
        if _sessions.get(tok, 0) <= time.time():
            await sock.close(code=4401)
            return
    elif (sock.client.host if sock.client else "") not in _LOOPBACK:
        await sock.close(code=4403)
        return
    await sock.accept()
    q: asyncio.Queue = asyncio.Queue()
    state["clients"].add(q)
    try:
        while True:
            evt = await q.get()
            await sock.send_text(json.dumps(evt, default=str))
    except WebSocketDisconnect:
        pass
    finally:
        state["clients"].discard(q)
