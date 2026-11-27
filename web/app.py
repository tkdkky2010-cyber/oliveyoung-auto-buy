"""로컬 웹 대시보드 (FastAPI) — http://127.0.0.1:8000

· 접속(GET)만으로는 아무 작업도 시작하지 않는다. 실행은 '실행' 버튼(POST)으로만.
· 127.0.0.1 에서만 듣고, Host 헤더가 로컬이 아니면 거절(DNS 리바인딩 방지).
· 상태를 바꾸는 요청(POST)은 대시보드 전용 헤더가 있어야 한다 — 다른 웹사이트가 몰래 구매를 시작하지 못하게(CSRF).
· 실시간 상태는 SSE(/api/stream). 브라우저를 닫아도 서버와 작업은 계속된다.
"""
from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "phone_auto"))

from fastapi import FastAPI, HTTPException, Request  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

import adb_manager as adb  # noqa: E402
import ocr_engine  # noqa: E402
import order_manager as om  # noqa: E402
import storage as db  # noqa: E402
import task_queue  # noqa: E402

WEB = Path(__file__).resolve().parent
CSRF_HEADER = "x-oy-dashboard"
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]", "testserver"}
STARTED = time.time()

STATUS = {"ocr": "loading", "ocr_error": "", "adb": {}, "adb_checked": 0.0}
manager: task_queue.TaskManager | None = None


def get_manager() -> task_queue.TaskManager:
    global manager
    if manager is None:
        db.init()
        manager = task_queue.TaskManager()
        manager.recover()
    return manager


def _preload_ocr() -> None:
    try:
        eng = ocr_engine.get_engine()
        import numpy as np
        eng.ocr(np.full((64, 256, 3), 255, np.uint8), use_cache=False)   # 첫 추론 지연을 미리
        STATUS["ocr"] = "ready"
    except Exception as e:
        STATUS["ocr"], STATUS["ocr_error"] = "error", str(e)


def _adb_monitor(stop: threading.Event) -> None:
    """3초마다 기기 목록 확인. 선택한 기기가 끊기면 이벤트로 알린다."""
    was = None
    while not stop.is_set():
        info = adb.info()
        STATUS["adb"], STATUS["adb_checked"] = info, time.time()
        if info["selected"]:
            if was is True and not info["connected"]:
                db.add_event("error", "adb", f"기기 연결 끊김: {info['selected']}")
            if was is False and info["connected"]:
                db.add_event("info", "adb", f"기기 다시 연결됨: {info['selected']}")
            was = info["connected"]
        stop.wait(3)


_stop = threading.Event()


@asynccontextmanager
async def lifespan(_app):
    _startup()
    yield
    _stop.set()


def _startup() -> None:
    get_manager()
    try:
        adb.adb_path()
        devs = [d for d in adb.list_devices() if d["state"] == "device"]
        if not adb.serial() and len(devs) == 1:
            adb.select(devs[0]["serial"])
    except adb.AdbError as e:
        STATUS["adb"] = {"error": str(e), "devices": [], "selected": "", "connected": False}
    threading.Thread(target=_preload_ocr, daemon=True, name="ocr-preload").start()
    threading.Thread(target=_adb_monitor, args=(_stop,), daemon=True, name="adb-monitor").start()
    db.add_event("info", "server", "서버 시작")


app = FastAPI(title="올리브영 자동구매", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=WEB / "static"), name="static")


@app.middleware("http")
async def _guard(request: Request, call_next):
    host = (request.headers.get("host") or "").rsplit(":", 1)[0]
    if host not in ALLOWED_HOSTS:
        return JSONResponse({"detail": "로컬 접속만 허용"}, status_code=403)
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        if request.headers.get(CSRF_HEADER) != "1":
            return JSONResponse({"detail": "대시보드 요청이 아님"}, status_code=403)
        origin = request.headers.get("origin")
        if origin and origin.split("://", 1)[-1].rsplit(":", 1)[0] not in ALLOWED_HOSTS:
            return JSONResponse({"detail": "다른 사이트의 요청 거절"}, status_code=403)
    resp = await call_next(request)
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Frame-Options"] = "DENY"
    return resp


def _err(e: Exception, code: int = 400):
    raise HTTPException(status_code=code, detail=str(e))


# ---------- 페이지 ----------

@app.get("/")
def index():
    return FileResponse(WEB / "templates" / "index.html")


@app.get("/api/health")
def health():
    return {"ok": True, "app": "oliveyoung-auto-buy", "uptime": round(time.time() - STARTED)}


def status_payload() -> dict:
    m = get_manager()
    active = db.query(f"SELECT id FROM runs WHERE status IN {task_queue.ACTIVE_RUN + ('paused', 'blocked')} "
                      "ORDER BY id DESC LIMIT 5")
    sim_today, real_today = om.today_totals(True), om.today_totals(False)
    unknown = db.query("SELECT * FROM payments WHERE status IN ('PENDING','UNKNOWN') AND simulated=0 ORDER BY id")
    return {"server": "running", "uptime": round(time.time() - STARTED),
            "ocr": {**ocr_engine.status(), "state": STATUS["ocr"], "load_error": STATUS["ocr_error"]},
            "adb": STATUS["adb"], "runs": [m.snapshot(r["id"]) for r in active],
            "today": {"real": real_today, "simulated": sim_today}, "unknown_payments": unknown,
            "settings": db.get_settings()}


@app.get("/api/status")
def status():
    return status_payload()


# ---------- 계정 (비밀번호는 절대 내보내지 않는다) ----------

@app.get("/api/accounts")
def accounts():
    import phone_login as pl
    try:
        accts = pl.load_accounts()
    except ValueError as e:
        return {"accounts": [], "error": str(e)}
    out = []
    for no, a in sorted(accts.items(), key=lambda kv: (len(kv[0]), kv[0])):
        if not isinstance(a, dict):
            continue
        i = a.get("id", "")
        out.append({"no": no, "id_masked": (i[:3] + "*" * max(0, len(i) - 3)) if i else "", "ready": bool(i and a.get("pw"))})
    return {"accounts": out, "error": ""}


# ---------- 기기 ----------

class DeviceSelect(BaseModel):
    serial: str = Field(min_length=1, max_length=200)


class DeviceConnect(BaseModel):
    address: str = Field(min_length=3, max_length=100)


@app.get("/api/devices")
def devices():
    info = adb.info()
    STATUS["adb"] = info
    if info["selected"]:
        try:
            info["size"] = adb.screen_size() if info["connected"] else None
        except adb.AdbError:
            pass
    return info


@app.post("/api/devices/select")
def device_select(body: DeviceSelect):
    if any(t.is_alive() for t in get_manager().workers.values()):
        _err(RuntimeError("작업 실행 중에는 기기를 바꿀 수 없음"), 409)
    try:
        adb.select(body.serial)
        return {"ok": True, "size": adb.screen_size()}
    except adb.AdbError as e:
        _err(e)


@app.post("/api/devices/connect")
def device_connect(body: DeviceConnect):
    try:
        return {"ok": adb.connect(body.address)}
    except adb.AdbError as e:
        _err(e)


# ---------- 작업 ----------

class RunIn(BaseModel):
    link: str = Field(max_length=500)
    accounts: str = Field(max_length=500)
    card: str
    mode: str = "prepare"
    option: int | None = None
    quantity: int = 1
    max_amount_per_account: int = 0
    max_total_amount: int = 0
    address_keyword: str = Field(default="", max_length=100)
    pin_mode: str = "auto"
    stop_on_failure: bool = True
    allow_repeat: bool = False
    account_delay: int = Field(default=30, ge=0, le=600)
    device: str = ""


@app.post("/api/runs")
def create_run(body: RunIn):
    try:
        run_id = get_manager().register(body.model_dump())
    except (ValueError, KeyError) as e:
        _err(e)
    return get_manager().snapshot(run_id)


@app.get("/api/runs")
def list_runs(limit: int = 30):
    return {"runs": db.query("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (min(limit, 200),))}


@app.get("/api/runs/{run_id}")
def get_run(run_id: int):
    try:
        return get_manager().snapshot(run_id)
    except KeyError as e:
        _err(e, 404)


@app.post("/api/runs/{run_id}/{action}")
def control(run_id: int, action: str):
    m = get_manager()
    fn = {"start": m.start, "pause": m.pause, "resume": m.resume, "stop": m.stop, "retry": m.retry_failed}.get(action)
    if fn is None:
        _err(ValueError("알 수 없는 동작"), 404)
    try:
        fn(run_id)
    except KeyError as e:
        _err(e, 404)
    except (RuntimeError, ValueError, adb.AdbError) as e:
        _err(e, 409)
    return m.snapshot(run_id)


# ---------- 결제·주문 ----------

class Resolve(BaseModel):
    paid: bool
    order_no: str = ""
    note: str = Field(default="", max_length=300)


@app.get("/api/orders")
def orders(limit: int = 100, include_simulated: bool = False):
    sql = "SELECT * FROM payments" + ("" if include_simulated else " WHERE simulated=0") + " ORDER BY id DESC LIMIT ?"
    return {"orders": db.query(sql, (min(limit, 500),))}


@app.post("/api/payments/{payment_id}/resolve")
def resolve(payment_id: int, body: Resolve):
    try:
        p = om.resolve(payment_id, body.paid, body.order_no.strip(), body.note)
    except om.OrderRejected as e:
        _err(e)
    db.add_event("info", "payment", f"{p['account_no']}번 결제 확인: {'결제됨 ' + p['order_no'] if body.paid else '결제 안 됨'}",
                 p["run_id"], p["job_id"])
    if p["run_id"]:
        run = db.one("SELECT * FROM runs WHERE id=?", (p["run_id"],))
        if run and run["status"] == "blocked" and not db.one(
                "SELECT 1 FROM payments WHERE run_id=? AND status IN ('PENDING','UNKNOWN')", (run["id"],)):
            db.execute("UPDATE runs SET status='paused', error='', updated_at=? WHERE id=?", (db.now(), run["id"]))
    return p


# ---------- 실시간 (SSE) ----------

@app.get("/api/events")
def events(after: int = 0, run_id: int | None = None, limit: int = 300):
    if after <= 0:   # 처음 열 때는 최근 것만
        last = db.one("SELECT COALESCE(MAX(id),0) m FROM events")["m"]
        after = max(0, last - limit)
    rows = db.events_after(after, limit)
    if run_id:
        rows = [r for r in rows if r["run_id"] == run_id]
    return {"events": rows}


@app.get("/api/stream")
async def stream(request: Request):
    async def gen():
        last = db.one("SELECT COALESCE(MAX(id),0) m FROM events")["m"]
        last = max(0, last - 200)
        tick = 0
        while not await request.is_disconnected():
            rows = await asyncio.to_thread(db.events_after, last, 200)
            for r in rows:
                last = r["id"]
                yield f"id: {r['id']}\nevent: log\ndata: {json.dumps(r, ensure_ascii=False)}\n\n"
            if tick % 4 == 0:
                st = await asyncio.to_thread(status_payload)
                yield f"event: status\ndata: {json.dumps(st, ensure_ascii=False, default=str)}\n\n"
            tick += 1
            await asyncio.sleep(0.5)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
