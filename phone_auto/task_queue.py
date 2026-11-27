"""계정별 구매 작업 큐 — 등록 / 실행 / 일시정지 / 재개 / 중지, 진행 상태는 SQLite 에 저장.

한 작업(run) = 링크 1개 + 계정 여러 개. 계정마다 job 하나를 순서대로 처리한다(기기 1대에서 동시에 두 계정 불가).
각 job 은 state_machine 의 상태를 따라가며, 실제 화면 조작은 Flow 가 한다:
  · PhoneFlow — 기존 threads_buy / phone_pay 함수(실측 경로)를 그대로 호출
  · SimFlow   — 폰 없이 같은 상태·검증·DB 경로를 도는 모의 실행(테스트·dry-run 점검용, 결제 없음)

실행 모드: pay(실제 결제) / prepare(결제하기 직전까지 — 실제 결제 없음) / simulate(폰 없이 모의).
안전 규칙:
  · 결제하기를 누르기 직전 결제 기록(PENDING)을 먼저 남긴다. 인증 이후 결과를 못 보면 UNKNOWN →
    그 계정은 주문내역 확인(대시보드에서 확정) 전까지 다시 결제하지 않고, 작업 전체를 멈춘다.
  · 일시정지·중지는 결제 전 안전 지점에서만 걸린다.
  · ADB 가 끊기면 결제 전 단계에서만 재연결 후 그 계정을 처음부터 다시 한다(1회).
  · 서버가 다시 켜지면 실행 중이던 작업은 '일시정지'로 돌려 놓는다 — 자동으로 결제를 다시 시작하지 않는다.
"""
from __future__ import annotations

import io
import re
import sys
import threading
import time
import traceback

import adb_manager as adb
import order_manager as om
import storage as db
from state_machine import LABELS, Control, InvalidTransition, Paused, S, State, StateMachine, Stopped

MODES = ("pay", "prepare", "simulate")
CARDS = ("lotte", "hyundai")
ACTIVE_RUN = ("running", "pausing", "stopping")


# ---------- 로그: 작업 스레드의 print 를 이벤트로 ----------

class _Router(io.TextIOBase):
    """sys.stdout 대체. 작업 스레드에서 찍힌 줄은 그 작업의 이벤트로 저장하고, 비밀값은 가린다."""

    def __init__(self, orig):
        self.orig = orig
        self.sinks: dict[int, callable] = {}
        self.buf: dict[int, str] = {}

    def write(self, s: str) -> int:
        tid = threading.get_ident()
        sink = self.sinks.get(tid)
        if sink is None:
            return self.orig.write(s)
        data = self.buf.get(tid, "") + s
        *lines, rest = data.split("\n")
        self.buf[tid] = rest
        for line in lines:
            if line.strip():
                sink(line)
        return len(s)

    def flush(self):
        try:
            self.orig.flush()
        except ValueError:   # 원래 stdout 이 닫힘(테스트 종료 등)
            pass


_router: _Router | None = None


def _install_router() -> _Router:
    global _router
    if _router is None:
        _router = _Router(sys.stdout)
        sys.stdout = _router
    return _router


def _ocr_counts() -> tuple[int, int]:
    import ocr_engine
    s = ocr_engine.status()
    return s.get("calls", 0), s.get("cache_hits", 0)


def redact(text: str, secrets: list[str]) -> str:
    for s in secrets:
        if s and len(s) >= 3:
            text = text.replace(s, "***")
    return text


# ---------- Flow ----------

class PhoneFlow:
    """실제 폰. 기존 실측 함수를 순서대로 부른다."""
    simulated = False

    def __init__(self):
        import phone_login as pl
        import phone_pay as pp
        import threads_buy as tb
        self.pl, self.pp, self.tb = pl, pp, tb

    def bind(self, guard, pay_hook, auth_hook, pin_mode: str, cancel_check):
        self.pp.ORDER_GUARD, self.pp.PAY_HOOK, self.pl.AUTH_HOOK = guard, pay_hook, auth_hook
        self.pp.PIN_MODE = pin_mode
        self.pl.CANCEL_CHECK = cancel_check   # Cloudflare 대기 중 일시정지·중지에 바로 반응

    def unbind(self):
        self.pp.ORDER_GUARD = self.pp.PAY_HOOK = self.pl.AUTH_HOOK = self.pl.CANCEL_CHECK = None

    def ensure_device(self, serial: str):
        if serial:
            adb.select(serial)
        s = adb.ensure_device()
        adb.screen_size()
        return s

    def login(self, no: str):
        self.tb.relogin(no)

    def open_product(self, no: str, link: str):
        self.tb.open_product(no, link)

    def select_option(self, option, quantity: int):
        self.pp.tap_cjone(self.tb.buy_now(option, quantity))

    def prepare_payment(self, card: str):
        return self.pp.prepare_payment(card)

    def pay(self, card: str, button, amount: int) -> bool:
        return self.pp.pay_by_card(card, button, amount)

    def finish(self, amount: int) -> str:
        return self.pp.finish_order(amount)


class SimFlow:
    """폰 없이 모의 실행. scenario = {계정번호: "fail_login"|"auth"|"auth_wait:<초>"|"unknown"|"disconnect"|"amount:<원>"}.
    auth_wait:<초> = Cloudflare 인증이 끝내 안 되는 경우 — 실제와 같이 대기 중 일시정지·중지에 반응하고, 시간이 지나면 로그인 실패."""
    simulated = True

    def __init__(self, scenario: dict | None = None, step_sec: float = 0.05, amount: int = 16200):
        self.scenario, self.step, self.amount = scenario or {}, step_sec, amount
        self.no = ""
        self.disconnected_once = set()

    def bind(self, guard, pay_hook, auth_hook, pin_mode, cancel_check):
        self.guard, self.hook, self.auth, self.cancel = guard, pay_hook, auth_hook, cancel_check

    def unbind(self):
        pass

    def _s(self):
        time.sleep(self.step)
        return self.scenario.get(self.no, "")

    def ensure_device(self, serial):
        return "SIMULATOR"

    def login(self, no):
        self.no = no
        sc = self._s()
        if sc == "auth":
            self.auth("auth_required", "[모의] Cloudflare 인증 대기", deadline=time.time() + 60)
            time.sleep(self.step)
            self.auth("auth_done", "[모의] 인증 완료")
        if sc.startswith("auth_wait:"):
            wait = float(sc.split(":")[1])
            self.auth("auth_required", "[모의] Cloudflare 인증 대기", deadline=time.time() + wait)
            end = time.monotonic() + wait
            while time.monotonic() < end:
                self.cancel()
                time.sleep(0.05)
            self.auth("auth_timeout", "[모의] Cloudflare 인증 시간 초과")
            raise RuntimeError("[모의] 로그인 실패: CLOUDFLARE_TIMEOUT")
        if sc == "fail_login":
            raise RuntimeError("[모의] 로그인 실패: STOP_CONSEC_FAIL:LOGIN_REJECTED")
        print(f"[모의] {no}번 로그인")

    def open_product(self, no, link):
        self._s()
        if self.scenario.get(self.no) == "disconnect" and self.no not in self.disconnected_once:
            self.disconnected_once.add(self.no)
            raise adb.AdbDisconnected("[모의] 기기 연결 끊김")
        print("[모의] 상품 페이지")

    def select_option(self, option, quantity):
        self._s()
        print(f"[모의] 옵션 {option or 1} / 수량 {quantity}")

    def prepare_payment(self, card):
        self._s()
        sc = self.scenario.get(self.no, "")
        amount = int(sc.split(":")[1]) if sc.startswith("amount:") else self.amount
        info = {"amount": amount, "card": {"lotte": "롯데카드", "hyundai": "현대카드"}[card],
                "card_shown": {"lotte": "롯데카드", "hyundai": "현대카드"}[card], "installment": "일시불",
                "address_name": "1(홍길동)", "address_default": True,
                "texts": ["모의 상품", "1(홍길동)", "서울 강남구 역삼로 123 101동 202호", f"{amount:,}원"]}
        self.guard(info)
        return amount, (835, 2160)

    def pay(self, card, button, amount):
        self.hook("payment_pending", {"amount": amount, "card": card})
        self._s()
        self.hook("auth_required", {"message": "[모의] 결제 인증"})
        self.hook("auth_done", {})
        if self.scenario.get(self.no) == "unknown":
            from phone_pay import PaymentSubmitted
            raise PaymentSubmitted("[모의] 인증 후 완료 화면 미확인")
        return True

    def finish(self, amount):
        self._s()
        order = "Y" + str(int(time.time() * 1000))[-13:].rjust(13, "0")
        self.hook("order_complete", {"order_no": order, "amount": amount})
        return order


# ---------- 작업 관리 ----------

def parse_accounts(spec: str) -> list[str]:
    """'1-10', '1,3,5', '1-3,7' → ['1', ...] (중복 제거, 순서 유지)."""
    spec = (spec or "").replace(" ", "")
    if not re.fullmatch(r"\d+(-\d+)?(,\d+(-\d+)?)*", spec):
        raise ValueError(f"계정 형식 오류: {spec} (예: 1-10 또는 1,3,5)")
    out: list[str] = []
    for part in spec.split(","):
        a, _, b = part.partition("-")
        rng = range(int(a), int(b or a) + 1) if int(b or a) >= int(a) else []
        if not rng:
            raise ValueError(f"계정 범위 오류: {part}")
        for n in rng:
            if str(n) not in out:
                out.append(str(n))
    if len(out) > 200:
        raise ValueError("계정은 한 번에 200개까지")
    return out


LINK_RE = re.compile(r"https://((www\.)?threads\.(net|com)/|([a-z]+\.)?(oliveyoung\.co\.kr|oy\.run)/)\S+$")


class TaskManager:
    def __init__(self, flow_factory=None):
        self.flow_factory = flow_factory or (lambda run: SimFlow() if run["mode"] == "simulate" else PhoneFlow())
        self.lock = threading.RLock()
        self.workers: dict[int, threading.Thread] = {}
        self.controls: dict[int, Control] = {}
        self.locks: dict[int, adb.DeviceLock] = {}
        self.current: dict[int, dict] = {}   # run_id → {job_id, account_no, state}
        _install_router()

    # ----- 등록 -----

    def register(self, p: dict) -> int:
        mode = p.get("mode", "prepare")
        if mode not in MODES:
            raise ValueError("실행 모드 오류")
        card = p.get("card", "")
        if card not in CARDS:
            raise ValueError("카드는 lotte(롯데) 또는 hyundai(현대)만 — 삼성카드 사용 금지")
        link = (p.get("link") or "").strip()
        if not LINK_RE.match(link):
            raise ValueError("쓰레드 글 주소 또는 올리브영 상품 주소(https://…)를 넣어 주세요")
        nos = parse_accounts(p.get("accounts", ""))
        if mode != "simulate":
            import phone_login as pl
            accts = pl.load_accounts()
            bad = [n for n in nos if not (accts.get(n) or {}).get("id") or not (accts.get(n) or {}).get("pw")]
            if bad:
                raise ValueError(f"accounts.json 에 id/pw 가 없는 계정: {', '.join(bad)}")
        quantity = int(p["quantity"]) if p.get("quantity") is not None else 1
        if not 1 <= quantity <= 10:
            raise ValueError("수량은 1~10")
        option = p.get("option")
        option = int(option) if option not in (None, "", 0, "0") else None
        if option is not None and option < 1:
            raise ValueError("옵션 번호는 1부터")
        address = (p.get("address_keyword") or "").strip()
        if len(address.replace(" ", "")) < 2:
            raise ValueError("배송지 확인(수령인 이름 또는 주소 일부)은 필수입니다 — 2글자 이상")
        pin_mode = p.get("pin_mode", "auto")
        if pin_mode not in ("auto", "manual"):
            raise ValueError("PIN 방식 오류")
        delay = int(p["account_delay"]) if p.get("account_delay") is not None else 30
        if not 0 <= delay <= 600:
            raise ValueError("계정 사이 쉬는 시간은 0~600초")
        ts = db.now()
        run_id = db.execute(
            "INSERT INTO runs(link, card, option_no, quantity, accounts, mode, device, max_amount_per_account, "
            "max_total_amount, address_keyword, pin_mode, stop_on_failure, "
            "allow_repeat, account_delay, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'registered',?,?)",
            (link, card, option, quantity, ",".join(nos), mode, p.get("device") or "",
             int(p.get("max_amount_per_account") or 0), int(p.get("max_total_amount") or 0),
             address, pin_mode, int(bool(p.get("stop_on_failure", True))),
             int(bool(p.get("allow_repeat", False))), delay, ts, ts))
        for i, no in enumerate(nos, 1):
            db.execute("INSERT INTO jobs(run_id, seq, account_no, updated_at) VALUES (?,?,?,?)", (run_id, i, no, ts))
        if mode != "simulate" and not p.get("allow_repeat"):
            for job in db.query("SELECT * FROM jobs WHERE run_id=?", (run_id,)):
                if om.bought_before(job["account_no"], link):
                    self._job(job["id"], status="skipped", detail="이미 이 상품을 구매한 계정 (완료 목록)")
        db.add_event("info", "run", f"작업 #{run_id} 등록: 계정 {len(nos)}개, 모드 {mode}", run_id)
        return run_id

    # ----- 제어 -----

    def start(self, run_id: int) -> None:
        with self.lock:
            run = self._run(run_id)
            if run["status"] in ACTIVE_RUN or run_id in self.workers and self.workers[run_id].is_alive():
                raise RuntimeError("이미 실행 중인 작업")
            if run["status"] in ("completed", "stopped", "blocked"):
                raise RuntimeError(f"다시 시작할 수 없는 상태: {run['status']}")
            if run["mode"] != "simulate":
                for rid, t in self.workers.items():
                    other = self._run(rid)
                    if t.is_alive() and other["mode"] != "simulate":
                        raise RuntimeError(f"다른 작업(#{rid})이 기기를 쓰는 중")
                serial = run["device"] or adb.serial()
                if not serial:
                    serial = adb.ensure_device()
                    db.execute("UPDATE runs SET device=? WHERE id=?", (serial, run_id))
                lock = adb.DeviceLock(serial)
                if not lock.acquire():
                    raise RuntimeError("이 기기를 다른 프로그램(프로세스)이 쓰는 중")
                self.locks[run_id] = lock
            ctl = Control()
            self.controls[run_id] = ctl
            self._set_run(run_id, "running")
            t = threading.Thread(target=self._worker, args=(run_id, ctl), name=f"run-{run_id}", daemon=True)
            self.workers[run_id] = t
            t.start()

    def pause(self, run_id: int) -> None:
        ctl = self.controls.get(run_id)
        if not ctl or not self.workers.get(run_id) or not self.workers[run_id].is_alive():
            raise RuntimeError("실행 중인 작업이 아님")
        ctl.resume.clear()
        ctl.pause.set()
        self._set_run(run_id, "pausing")
        db.add_event("info", "control", "일시정지 요청 — 다음 안전 지점(결제 전)에서 멈춥니다", run_id)

    def resume(self, run_id: int) -> None:
        run = self._run(run_id)
        ctl = self.controls.get(run_id)
        alive = run_id in self.workers and self.workers[run_id].is_alive()
        if alive and ctl and ctl.pause.is_set():
            ctl.pause.clear()
            ctl.resume.set()
            self._set_run(run_id, "running")
            db.add_event("info", "control", "재개", run_id)
            return
        if run["status"] in ("paused", "registered", "failed"):
            db.execute("UPDATE jobs SET status='pending', state='IDLE', updated_at=? WHERE run_id=? AND status IN "
                       "('paused','running','stopped')", (db.now(), run_id))
            self.start(run_id)
            return
        raise RuntimeError(f"재개할 수 없는 상태: {run['status']}")

    def stop(self, run_id: int) -> None:
        ctl = self.controls.get(run_id)
        if ctl and run_id in self.workers and self.workers[run_id].is_alive():
            ctl.stop.set()
            ctl.resume.set()
            self._set_run(run_id, "stopping")
            db.add_event("info", "control", "중지 요청 — 결제 진행 중이면 결과 확인 후 멈춥니다", run_id)
        else:
            run = self._run(run_id)
            if run["status"] in ("completed", "stopped"):
                return
            self._set_run(run_id, "stopped")
            db.execute("UPDATE jobs SET status='stopped', updated_at=? WHERE run_id=? AND status IN ('pending','paused')",
                       (db.now(), run_id))

    def retry_failed(self, run_id: int) -> None:
        """실패한(결제 전) 계정만 다시 대기열로. 결과 불명 계정은 제외."""
        run = self._run(run_id)
        if run["status"] in ACTIVE_RUN:
            raise RuntimeError("실행 중에는 불가")
        db.execute("UPDATE jobs SET status='pending', state='IDLE', error='', updated_at=? WHERE run_id=? AND "
                   "status IN ('failed','stopped','paused')", (db.now(), run_id))
        self._set_run(run_id, "paused")

    def wait(self, run_id: int, timeout: float = 30) -> None:
        t = self.workers.get(run_id)
        if t:
            t.join(timeout)

    # ----- 복구 -----

    def recover(self) -> None:
        """서버 재시작 시: 실행 중이던 run 은 paused, 결제 기록이 PENDING 으로 남은 job 은 결과 불명."""
        for p in db.query("SELECT * FROM payments WHERE status='PENDING'"):
            om.mark(p["id"], "UNKNOWN", note="프로그램 종료로 결과 미확인")
            if p["job_id"]:
                db.execute("UPDATE jobs SET status='unknown', state='PAYMENT_RESULT_UNKNOWN', updated_at=? WHERE id=?",
                           (db.now(), p["job_id"]))
            db.add_event("error", "payment", f"{p['account_no']}번 결제 결과 불명(종료 중이었음) — 주문내역 확인 필요",
                         p["run_id"], p["job_id"])
        for r in db.query(f"SELECT * FROM runs WHERE status IN {ACTIVE_RUN}"):
            db.execute("UPDATE jobs SET status='pending', state='IDLE', updated_at=? WHERE run_id=? AND status='running'",
                       (db.now(), r["id"]))
            blocked = db.one("SELECT 1 FROM jobs WHERE run_id=? AND status='unknown'", (r["id"],))
            self._set_run(r["id"], "blocked" if blocked else "paused",
                          "프로그램 재시작 — 결과 불명 결제 확인 필요" if blocked else "프로그램 재시작으로 일시정지")

    # ----- 조회 -----

    def snapshot(self, run_id: int) -> dict:
        run = self._run(run_id)
        jobs = db.query("SELECT * FROM jobs WHERE run_id=? ORDER BY seq", (run_id,))
        for j in jobs:
            j["state_label"] = LABELS.get(State(j["state"]), j["state"]) if j["state"] in State.__members__ else j["state"]
        done = sum(j["status"] in ("completed", "prepared", "skipped", "failed", "unknown") for j in jobs)
        run["progress"] = round(done / len(jobs) * 100) if jobs else 0
        run["total_amount"] = sum(j["amount"] for j in jobs if j["status"] == "completed")
        run["current"] = self.current.get(run_id)
        run["alive"] = run_id in self.workers and self.workers[run_id].is_alive()
        return {"run": run, "jobs": jobs}

    # ----- 내부 -----

    def _run(self, run_id: int) -> dict:
        r = db.one("SELECT * FROM runs WHERE id=?", (run_id,))
        if not r:
            raise KeyError(f"작업 #{run_id} 없음")
        return r

    def _set_run(self, run_id: int, status: str, error: str | None = None) -> None:
        if error is None:
            db.execute("UPDATE runs SET status=?, updated_at=? WHERE id=?", (status, db.now(), run_id))
        else:
            db.execute("UPDATE runs SET status=?, error=?, updated_at=? WHERE id=?", (status, error, db.now(), run_id))

    def _job(self, job_id: int, **kw) -> None:
        kw["updated_at"] = db.now()
        cols = ", ".join(f"{k}=?" for k in kw)
        db.execute(f"UPDATE jobs SET {cols} WHERE id=?", (*kw.values(), job_id))

    def _worker(self, run_id: int, ctl: Control) -> None:
        final = "completed"
        try:
            while True:
                run = self._run(run_id)
                job = db.one("SELECT * FROM jobs WHERE run_id=? AND status IN ('pending','paused') ORDER BY seq LIMIT 1",
                             (run_id,))
                if job is None:
                    break
                outcome = self._run_job(run, job, ctl)
                if outcome == "paused":
                    if not self._wait_resume(run_id, ctl):
                        final = "stopped"
                        break
                    continue
                if outcome == "stopped":
                    final = "stopped"
                    break
                if outcome == "unknown":
                    final = "blocked"
                    db.add_event("error", "run", "결제 결과 불명 — 작업 전체를 멈춤. 주문내역 확인 후 처리하세요", run_id)
                    break
                if outcome == "failed" and run["stop_on_failure"]:
                    final = "failed"
                    db.add_event("error", "run", "계정 실패 — '실패 시 전체 중단' 설정으로 멈춤", run_id)
                    break
                if ctl.stop.is_set():
                    final = "stopped"
                    break
                # 계정 사이 쉬는 시간 — 폰을 실제로 쓴 계정 뒤에만, 다음 계정이 남아 있을 때만
                if outcome in ("completed", "prepared", "failed") and run["account_delay"] > 0 and db.one(
                        "SELECT 1 FROM jobs WHERE run_id=? AND status IN ('pending','paused')", (run_id,)):
                    rest = self._rest(run_id, ctl, run["account_delay"])
                    if rest == "stopped" or (rest == "paused" and not self._wait_resume(run_id, ctl)):
                        final = "stopped"
                        break
            if final == "completed" and db.one("SELECT 1 FROM jobs WHERE run_id=? AND status='failed'", (run_id,)):
                final = "completed_with_failures"
        except Exception as e:   # 작업 엔진 자체 오류
            final = "failed"
            db.add_event("error", "run", f"작업 엔진 오류: {type(e).__name__}: {e}", run_id)
            traceback.print_exc(file=sys.__stderr__)
        finally:
            if final == "stopped":
                db.execute("UPDATE jobs SET status='stopped', updated_at=? WHERE run_id=? AND status IN ('pending','paused')",
                           (db.now(), run_id))
            self._set_run(run_id, final)
            self.current.pop(run_id, None)
            lock = self.locks.pop(run_id, None)
            if lock:
                lock.release()
            db.add_event("info", "run", f"작업 #{run_id} 종료: {final}", run_id)

    def _wait_resume(self, run_id: int, ctl: Control) -> bool:
        """일시정지 상태로 재개·중지를 기다린다. 재개면 True, 중지면 False."""
        self._set_run(run_id, "paused")
        db.add_event("info", "control", "일시정지됨", run_id)
        while not ctl.resume.wait(0.5):
            pass
        ctl.resume.clear()
        if ctl.stop.is_set():
            return False
        self._set_run(run_id, "running")
        return True

    def _rest(self, run_id: int, ctl: Control, seconds: int) -> str:
        """계정 사이 대기. 대기 중 일시정지·중지 요청엔 바로 반응한다. ok / paused / stopped."""
        db.add_event("info", "rest", f"다음 계정까지 {seconds}초 쉬는 중", run_id)
        end = time.monotonic() + seconds
        self.current[run_id] = {"job_id": None, "account_no": "", "state": "REST", "rest_until": time.time() + seconds}
        while time.monotonic() < end:
            if ctl.stop.is_set():
                return "stopped"
            if ctl.pause.is_set():
                return "paused"
            time.sleep(min(0.2, max(0.0, end - time.monotonic())))
        self.current.pop(run_id, None)
        return "ok"

    def _run_job(self, run: dict, job: dict, ctl: Control) -> str:
        run_id, job_id, no = run["id"], job["id"], job["account_no"]
        sim = run["mode"] == "simulate"
        secrets: list[str] = []
        if not sim:
            try:
                import phone_login as pl
                a = pl.load_account(no)
                secrets = [a["pw"], a["id"]]
            except Exception:
                pass
        cur = {"job_id": job_id, "account_no": no, "state": "IDLE"}
        self.current[run_id] = cur

        def log(line: str, level: str = "info", kind: str = "log"):
            db.add_event(level, kind, redact(line, secrets), run_id, job_id)

        def on_change(old, new, detail):
            cur["state"] = new.value
            self._job(job_id, state=new.value, detail=detail)
            db.add_event("info", "state", f"{no}번: {LABELS[new]}" + (f" — {detail}" if detail else ""), run_id, job_id)

        sm = StateMachine(on_change)
        payment = {"id": None}
        flow = self.flow_factory(run)

        def guard(info: dict):
            sm.to(S.ORDER_VALIDATION, f"{info.get('amount', 0):,}원")
            try:
                om.validate_order(run, no, info)
                log(f"배송지 확인 통과: {info.get('address_name', '')} ← '{run['address_keyword']}'")
            except om.OrderRejected as e:
                from phone_pay import PayStop
                raise PayStop(f"주문 검증 실패: {e}") from None

        def pay_hook(event: str, data: dict):
            if event == "payment_pending":
                # 기록을 먼저 — 확인 안 된 결제가 있으면 여기서 막혀 결제하기를 누르지 않는다
                try:
                    payment["id"] = om.record_pending(job_id, run_id, no, run["link"], data["amount"], data["card"], sim)
                except om.OrderRejected as e:
                    from phone_pay import PayStop
                    raise PayStop(str(e)) from None
                sm.to(S.PAYMENT_PENDING, f"{data['amount']:,}원")
                self._job(job_id, amount=data["amount"])
            elif event == "auth_required":
                sm.to(S.PAYMENT_AUTH_REQUIRED, data.get("message", ""))
                log(data.get("message", "결제 인증 대기"), "warn", "auth")
            elif event == "auth_done":
                sm.to(S.PAYMENT_PENDING, "인증 완료")
            elif event == "order_complete":
                om.mark(payment["id"], "COMPLETE", data["order_no"], amount=data.get("amount"))

        def auth_hook(kind: str, message: str, deadline: float | None = None):
            if kind == "auth_required":
                cur["auth_until"] = deadline   # 웹에서 남은 대기시간 표시
                sm.to(S.AUTHENTICATION_REQUIRED, message)
                log(message, "warn", "auth")
            elif kind == "auth_done":
                cur.pop("auth_until", None)
                sm.to(S.LOGIN_IN_PROGRESS, "인증 완료")
            elif kind == "auth_timeout":
                cur.pop("auth_until", None)
                log(message, "error", "auth")

        router = _install_router()
        router.sinks[threading.get_ident()] = log
        # dry-run(prepare)은 PIN 단계까지 가지 않으므로 card_secrets.json 없이도 돌게 manual 로 묶는다
        pin_mode = "manual" if run["mode"] == "prepare" else run["pin_mode"]
        flow.bind(guard, pay_hook, auth_hook, pin_mode, ctl.checkpoint)
        self._job(job_id, status="running", started_at=db.now(), error="")
        t_job = time.monotonic()
        ocr0 = _ocr_counts()
        if not sim:
            import screen_reader as sr
            sr.reset_stats()
        try:
            for attempt in (1, 2):
                try:
                    return self._steps(run, job, flow, sm, ctl)
                except adb.AdbDisconnected as e:
                    if sm.in_payment_zone or payment["id"]:
                        raise
                    log(f"ADB 연결 끊김: {e} — 재연결 시도", "warn", "adb")
                    if attempt == 2 or (not sim and not adb.reconnect(60)):
                        raise
                    log("재연결됨 — 이 계정을 처음부터 다시", "info", "adb")
                    sm.to(S.FAILED, "연결 끊김")
                    sm.to(S.IDLE, "재연결 후 재시도")
                    self._job(job_id, retries=job["retries"] + 1)
        except Paused:
            sm.to(S.PAUSED)
            self._job(job_id, status="paused")
            return "paused"
        except Stopped:
            sm.to(S.STOPPED, "사용자 중지")
            self._job(job_id, status="stopped", finished_at=db.now())
            return "stopped"
        except Exception as e:
            from phone_pay import PaymentSubmitted
            msg = redact(f"{e}", secrets)
            if isinstance(e, PaymentSubmitted) or (payment["id"] and sm.in_payment_zone):
                if payment["id"]:
                    om.mark(payment["id"], "UNKNOWN", note=msg)
                if sm.state != S.PAYMENT_RESULT_UNKNOWN:
                    try:
                        sm.to(S.PAYMENT_RESULT_UNKNOWN, msg)
                    except InvalidTransition:
                        pass
                self._job(job_id, status="unknown", error=msg, finished_at=db.now())
                log(f"{no}번 결제 결과 불명: {msg}", "error", "payment")
                return "unknown"
            if payment["id"]:
                om.mark(payment["id"], "NOT_PAID", note=f"인증 전 중단: {msg}")
            try:
                sm.to(S.FAILED, msg)
            except InvalidTransition:
                pass
            self._job(job_id, status="failed", error=msg, finished_at=db.now())
            log(f"{no}번 실패: {msg}", "error", "job")
            return "failed"
        finally:
            cur.pop("auth_until", None)
            flow.unbind()
            if not sim:
                import screen_reader as sr
                st, ocr1 = sr.stats(), _ocr_counts()
                log(f"{no}번 화면 읽기: dump {st['dumps']}회({st['dump_sec']}초) · dump 재사용 {st['dump_reuse']}회 · "
                    f"스크린샷 {st['shots']}회 · OCR {ocr1[0] - ocr0[0]}회(캐시 {ocr1[1] - ocr0[1]}회) · "
                    f"계정 처리 {time.monotonic() - t_job:.1f}초 · dump 캐시 {'켜짐' if sr.DUMP_CACHE else '꺼짐'}",
                    "info", "perf")
            router.sinks.pop(threading.get_ident(), None)

    def _steps(self, run: dict, job: dict, flow, sm: StateMachine, ctl: Control) -> str:
        no, job_id = job["account_no"], job["id"]
        ctl.checkpoint()
        try:
            om.precheck(run, no)
        except om.OrderRejected as e:
            self._job(job_id, status="skipped", detail=str(e), finished_at=db.now())
            db.add_event("warn", "job", f"{no}번 건너뜀: {e}", run["id"], job_id)
            return "skipped"
        serial = flow.ensure_device(run["device"])
        sm.to(S.DEVICE_CONNECTED, serial)
        ctl.checkpoint()
        sm.to(S.LOGIN_REQUIRED)
        sm.to(S.LOGIN_IN_PROGRESS)
        flow.login(no)
        sm.to(S.LOGIN_SUCCESS)
        ctl.checkpoint()
        sm.to(S.PRODUCT_LOADING)
        flow.open_product(no, run["link"])
        sm.to(S.PRODUCT_FOUND)
        ctl.checkpoint()
        flow.select_option(run["option_no"], run["quantity"])
        sm.to(S.OPTION_SELECTED, f"옵션 {run['option_no'] or 1} / 수량 {run['quantity']}")
        ctl.checkpoint()
        sm.to(S.ORDER_PREPARATION)
        amount, button = flow.prepare_payment(run["card"])   # 안에서 ORDER_VALIDATION(guard)
        self._job(job_id, amount=amount)
        if run["mode"] == "prepare":
            sm.to(S.STOPPED, f"dry-run: 결제하기 직전에서 멈춤 ({amount:,}원)")
            self._job(job_id, status="prepared", finished_at=db.now())
            return "prepared"
        ctl.checkpoint()   # 마지막 안전 지점 — 이 뒤로는 결제 결과가 나올 때까지 멈추지 않는다
        if not flow.pay(run["card"], button, amount):
            raise RuntimeError("결제 진행 안 됨")
        order = flow.finish(amount)
        sm.to(S.ORDER_COMPLETE, order)
        self._job(job_id, status="completed", order_no=order, finished_at=db.now())
        return "completed"
