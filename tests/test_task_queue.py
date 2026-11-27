"""작업 큐 (폰 없이 SimFlow): 순차 실행, 일시정지·재개·중지, 중복 결제 방지, 한도, 재시작 복구, 재연결."""
import time

import pytest

import order_manager as om
import task_queue as tq

LINK = "https://m.oliveyoung.co.kr/m/goods/getGoodsDetail.do?goodsNo=A000000251335"


def mgr(scenario=None, step=0.01, flows=None):
    def factory(run):
        f = tq.SimFlow(scenario, step_sec=step)
        if flows is not None:
            flows.append(f)
        return f
    return tq.TaskManager(flow_factory=factory)


def reg(m, **kw):
    p = {"link": LINK, "accounts": "1-3", "card": "hyundai", "mode": "simulate", "address_keyword": "홍길동",
         "account_delay": 0}
    p.update(kw)
    return m.register(p)


def wait_status(m, run_id, statuses, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        s = m.snapshot(run_id)["run"]["status"]
        if s in statuses:
            return s
        time.sleep(0.02)
    raise AssertionError(f"status {m.snapshot(run_id)['run']['status']}")


def test_parse_accounts():
    assert tq.parse_accounts("1-3,7,2") == ["1", "2", "3", "7"]
    for bad in ("", "a", "3-1", "1--2"):
        with pytest.raises(ValueError):
            tq.parse_accounts(bad)


def test_register_validation(tmpdb):
    m = mgr()
    for bad in ({"card": "samsung"}, {"link": "https://evil.example.com/x"}, {"quantity": 0}, {"mode": "x"},
                {"address_keyword": ""}, {"address_keyword": " 김 "}, {"account_delay": -1}, {"account_delay": 601}):
        with pytest.raises(ValueError):
            reg(m, **bad)


def test_register_does_not_start(tmpdb):
    m = mgr()
    rid = reg(m)
    time.sleep(0.1)
    snap = m.snapshot(rid)
    assert snap["run"]["status"] == "registered" and all(j["status"] == "pending" for j in snap["jobs"])


def test_full_run_completes(tmpdb):
    m = mgr()
    rid = reg(m)
    m.start(rid)
    assert wait_status(m, rid, {"completed"}) == "completed"
    jobs = m.snapshot(rid)["jobs"]
    assert [j["status"] for j in jobs] == ["completed"] * 3
    assert all(j["order_no"].startswith("Y") and j["state"] == "ORDER_COMPLETE" for j in jobs)
    with pytest.raises(RuntimeError):
        m.start(rid)   # 완료된 작업 재실행 불가


def test_double_start_rejected(tmpdb):
    m = mgr(step=0.05)
    rid = reg(m)
    m.start(rid)
    with pytest.raises(RuntimeError):
        m.start(rid)
    m.stop(rid)
    wait_status(m, rid, {"stopped", "completed"})


def test_pause_resume(tmpdb):
    m = mgr(step=0.03)
    rid = reg(m, accounts="1-4")
    m.start(rid)
    time.sleep(0.05)
    m.pause(rid)
    wait_status(m, rid, {"paused"})
    statuses = [j["status"] for j in m.snapshot(rid)["jobs"]]
    assert "paused" in statuses or "pending" in statuses
    m.resume(rid)
    assert wait_status(m, rid, {"completed"}) == "completed"
    assert [j["status"] for j in m.snapshot(rid)["jobs"]] == ["completed"] * 4


def test_stop(tmpdb):
    m = mgr(step=0.03)
    rid = reg(m, accounts="1-5")
    m.start(rid)
    time.sleep(0.05)
    m.stop(rid)
    assert wait_status(m, rid, {"stopped"}) == "stopped"
    jobs = m.snapshot(rid)["jobs"]
    assert jobs[-1]["status"] == "stopped"
    assert all(j["status"] in ("completed", "stopped") for j in jobs)


def test_unknown_blocks_and_prevents_repay(tmpdb):
    # 2번이 결과 불명 → 전체 멈춤, 3번은 실행 안 함
    m = mgr({"2": "unknown"})
    rid = reg(m, mode="simulate")
    m.start(rid)
    assert wait_status(m, rid, {"blocked"}) == "blocked"
    jobs = {j["account_no"]: j for j in m.snapshot(rid)["jobs"]}
    assert jobs["1"]["status"] == "completed" and jobs["2"]["status"] == "unknown" and jobs["3"]["status"] == "pending"
    assert jobs["2"]["state"] == "PAYMENT_RESULT_UNKNOWN"
    p = tmpdb.one("SELECT * FROM payments WHERE account_no='2'")
    assert p["status"] == "UNKNOWN"
    # 실제 결제 기록이라면: 결과 불명이 남은 계정은 새 결제 기록 자체가 거절된다
    tmpdb.execute("UPDATE payments SET simulated=0 WHERE id=?", (p["id"],))
    with pytest.raises(om.OrderRejected):
        om.record_pending(None, rid, "2", LINK, 1000, "hyundai", simulated=False)
    with pytest.raises(om.OrderRejected):
        om.precheck({"mode": "pay", "allow_repeat": 0, "link": LINK}, "2")
    with pytest.raises(om.OrderRejected):
        om.resolve(p["id"], True, "")   # 주문번호 없이 '결제됨' 불가
    om.resolve(p["id"], True, "Y2611272780971")
    assert tmpdb.one("SELECT status FROM jobs WHERE id=?", (p["job_id"],))["status"] == "completed"


def test_failure_stop_on_failure_and_continue(tmpdb):
    m = mgr({"2": "fail_login"})
    rid = reg(m)
    m.start(rid)
    assert wait_status(m, rid, {"failed"}) == "failed"
    assert [j["status"] for j in m.snapshot(rid)["jobs"]] == ["completed", "failed", "pending"]
    m2 = mgr({"2": "fail_login"})
    rid2 = reg(m2, stop_on_failure=False)
    m2.start(rid2)
    assert wait_status(m2, rid2, {"completed_with_failures"})
    jobs = m2.snapshot(rid2)["jobs"]
    assert [j["status"] for j in jobs] == ["completed", "failed", "completed"]
    assert "로그인 실패" in jobs[1]["error"]


def test_amount_limits(tmpdb):
    m = mgr()
    rid = reg(m, max_amount_per_account=10000, stop_on_failure=False)
    m.start(rid)
    wait_status(m, rid, {"completed_with_failures", "failed"})
    jobs = m.snapshot(rid)["jobs"]
    assert all(j["status"] == "failed" and "계정별 최대" in j["error"] for j in jobs)
    assert tmpdb.one("SELECT COUNT(*) n FROM payments WHERE run_id=?", (rid,))["n"] == 0   # 결제 기록 없음 = 결제하기 안 누름

    rid = reg(m, max_total_amount=40000, stop_on_failure=False)   # 16,200 × 2 = 32,400 까지만
    m.start(rid)
    wait_status(m, rid, {"completed_with_failures"})
    assert [j["status"] for j in m.snapshot(rid)["jobs"]] == ["completed", "completed", "failed"]


def test_rest_between_accounts(tmpdb):
    m = mgr()
    rid = reg(m, accounts="1-2", account_delay=1)
    t0 = time.time()
    m.start(rid)
    wait_status(m, rid, {"completed"})
    assert time.time() - t0 >= 1.0
    rests = tmpdb.query("SELECT message FROM events WHERE run_id=? AND kind='rest'", (rid,))
    assert len(rests) == 1 and "1초" in rests[0]["message"]   # 마지막 계정 뒤에는 쉬지 않는다


def test_stop_and_pause_during_rest(tmpdb):
    m = mgr()
    rid = reg(m, accounts="1-3", account_delay=30)
    m.start(rid)
    end = time.time() + 5
    while time.time() < end and (m.current.get(rid) or {}).get("state") != "REST":
        time.sleep(0.02)
    m.pause(rid)
    assert wait_status(m, rid, {"paused"}, timeout=2) == "paused"   # 30초를 다 기다리지 않는다
    m.stop(rid)
    assert wait_status(m, rid, {"stopped"}, timeout=2) == "stopped"
    assert [j["status"] for j in m.snapshot(rid)["jobs"]] == ["completed", "stopped", "stopped"]


def test_no_daily_purchase_limit(tmpdb):
    m = mgr()
    for _ in range(2):   # 하루 구매 한도 없음
        rid = reg(m, accounts="1")
        m.start(rid)
        wait_status(m, rid, {"completed"})
        assert m.snapshot(rid)["jobs"][0]["status"] == "completed"


def test_auth_wait_states(tmpdb):
    m = mgr({"1": "auth"})
    rid = reg(m, accounts="1")
    m.start(rid)
    wait_status(m, rid, {"completed"})
    states = [e["message"] for e in tmpdb.query("SELECT message FROM events WHERE run_id=? AND kind='state'", (rid,))]
    assert any("사람 인증 대기" in s for s in states) and any("결제 인증 대기" in s for s in states)


def test_disconnect_retries_from_start(tmpdb):
    m = mgr({"1": "disconnect"})
    rid = reg(m, accounts="1")
    m.start(rid)
    wait_status(m, rid, {"completed"})
    j = m.snapshot(rid)["jobs"][0]
    assert j["status"] == "completed" and j["retries"] == 1


def test_recover_after_restart(tmpdb):
    m = mgr()
    rid = reg(m, mode="simulate")
    # 결제하기 직전 기록만 남기고 프로그램이 죽은 상황
    job = tmpdb.query("SELECT * FROM jobs WHERE run_id=? ORDER BY seq", (rid,))
    tmpdb.execute("UPDATE runs SET status='running' WHERE id=?", (rid,))
    tmpdb.execute("UPDATE jobs SET status='running', state='PAYMENT_PENDING' WHERE id=?", (job[0]["id"],))
    tmpdb.execute("UPDATE jobs SET status='running', state='LOGIN_IN_PROGRESS' WHERE id=?", (job[1]["id"],))
    pid = om.record_pending(job[0]["id"], rid, "1", LINK, 16200, "hyundai", simulated=False)
    tq.TaskManager().recover()
    snap = m.snapshot(rid)
    assert snap["run"]["status"] == "blocked"   # 자동으로 다시 결제하지 않는다
    assert snap["jobs"][0]["status"] == "unknown" and snap["jobs"][1]["status"] == "pending"
    assert tmpdb.one("SELECT status FROM payments WHERE id=?", (pid,))["status"] == "UNKNOWN"


def test_log_redaction():
    assert tq.redact("pw=Pw!secret$1 id tester01", ["Pw!secret$1", "tester01"]) == "pw=*** id ***"


def test_address_check():
    info = {"address_name": "1(김건엽)", "texts": ["헤이즐넛 바디워시", "서울 강남구 역삼로 123 101동 202호", "16,200원"]}
    assert om.check_address("김건엽", info) == "배송지 이름"
    assert om.check_address("역삼로 123", info) == "주소"
    for bad in ("홍길동", "바디워시", "", "김"):   # 상품명 글자는 주소로 인정하지 않는다
        with pytest.raises(om.OrderRejected):
            om.check_address(bad, info)


def test_wrong_address_stops_before_payment(tmpdb):
    m = mgr()
    rid = reg(m, accounts="1", address_keyword="김건엽")
    m.start(rid)
    wait_status(m, rid, {"failed"})
    j = m.snapshot(rid)["jobs"][0]
    assert j["status"] == "failed" and "배송지 확인 실패" in j["error"]
    assert tmpdb.one("SELECT COUNT(*) n FROM payments")["n"] == 0   # 결제하기 안 누름


def test_cloudflare_wait_pause_and_stop(tmpdb):
    m = mgr({"1": "auth_wait:30"})
    rid = reg(m, accounts="1")
    m.start(rid)
    end = time.time() + 5
    while time.time() < end and (m.current.get(rid) or {}).get("state") != "AUTHENTICATION_REQUIRED":
        time.sleep(0.02)
    cur = m.snapshot(rid)["run"]["current"]
    assert 25 < cur["auth_until"] - time.time() <= 30          # 웹에 남은 시간 표시용
    t0 = time.time()
    m.pause(rid)
    assert wait_status(m, rid, {"paused"}, timeout=2) == "paused" and time.time() - t0 < 1
    assert m.snapshot(rid)["jobs"][0]["state"] == "PAUSED"
    m.stop(rid)
    assert wait_status(m, rid, {"stopped"}, timeout=2) == "stopped"


def test_cloudflare_timeout_fails_account_and_continues(tmpdb):
    m = mgr({"1": "auth_wait:0.3"})
    rid = reg(m, accounts="1-2", stop_on_failure=False)
    m.start(rid)
    wait_status(m, rid, {"completed_with_failures"})
    jobs = m.snapshot(rid)["jobs"]
    assert jobs[0]["status"] == "failed" and "CLOUDFLARE_TIMEOUT" in jobs[0]["error"]
    assert jobs[1]["status"] == "completed"
    assert tmpdb.one("SELECT COUNT(*) n FROM payments WHERE account_no='1'")["n"] == 0
