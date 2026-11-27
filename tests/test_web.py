"""웹 대시보드: 접속만으로 실행 안 됨, CSRF·Host 차단, 새로고침 중복 실행 방지, 비밀번호 비노출, 결과 확인."""
import json
import time

import pytest
from fastapi.testclient import TestClient

H = {"x-oy-dashboard": "1"}
LINK = "https://m.oliveyoung.co.kr/m/goods/getGoodsDetail.do?goodsNo=A000000251335"


@pytest.fixture()
def client(tmpdb, monkeypatch, tmp_path):
    import phone_login as pl
    accts = tmp_path / "accounts.json"
    accts.write_text(json.dumps({"1": {"id": "tester01", "pw": "Pw!secret$1"}, "2": {"id": "tester02", "pw": "x"}}),
                     encoding="utf-8")
    monkeypatch.setattr(pl, "ACCOUNTS", accts)
    import web.app as appmod
    monkeypatch.setattr(appmod, "manager", None)
    with TestClient(appmod.app, base_url="http://127.0.0.1:8000") as c:
        yield c


def body(**kw):
    b = {"link": LINK, "accounts": "1-2", "card": "lotte", "mode": "simulate", "address_keyword": "홍길동",
         "account_delay": 0}
    b.update(kw)
    return b


def test_get_pages_do_not_start_anything(client, tmpdb):
    for path in ("/", "/api/status", "/api/runs", "/api/devices", "/api/orders", "/api/accounts", "/api/health"):
        assert client.get(path).status_code == 200
    client.post("/api/runs", json=body(), headers=H)
    for _ in range(3):   # 새로고침 반복
        client.get("/")
        client.get("/api/status")
    time.sleep(0.2)
    assert tmpdb.one("SELECT status FROM runs")["status"] == "registered"
    assert tmpdb.one("SELECT COUNT(*) n FROM payments")["n"] == 0


def test_csrf_and_host_blocked(client):
    assert client.post("/api/runs", json=body()).status_code == 403                     # 헤더 없음
    assert client.post("/api/runs", json=body(), headers={**H, "origin": "https://evil.example"}).status_code == 403
    assert client.get("/api/status", headers={"host": "evil.example:8000"}).status_code == 403


def test_accounts_never_expose_password(client):
    r = client.get("/api/accounts").json()
    text = json.dumps(r, ensure_ascii=False)
    assert "Pw!secret$1" not in text and "tester01" not in text
    assert r["accounts"][0]["id_masked"].startswith("tes")


def test_run_lifecycle_and_double_start(client, tmpdb):
    rid = client.post("/api/runs", json=body(), headers=H).json()["run"]["id"]
    assert client.post(f"/api/runs/{rid}/start", headers=H).status_code == 200
    second = client.post(f"/api/runs/{rid}/start", headers=H)
    assert second.status_code == 409 or second.json()["run"]["status"] in ("completed",)
    end = time.time() + 10
    while time.time() < end and client.get(f"/api/runs/{rid}").json()["run"]["status"] != "completed":
        time.sleep(0.05)
    assert client.get(f"/api/runs/{rid}").json()["run"]["status"] == "completed"
    assert client.post(f"/api/runs/{rid}/start", headers=H).status_code == 409


def test_register_rejects_samsung_and_missing_accounts(client):
    assert client.post("/api/runs", json=body(card="samsung"), headers=H).status_code == 400
    assert client.post("/api/runs", json=body(address_keyword=""), headers=H).status_code == 400
    assert client.post("/api/runs", json=body(mode="prepare", accounts="1-5"), headers=H).status_code == 400


def test_events_and_no_settings_endpoint(client):
    assert client.post("/api/settings", json={"account_daily_limit": 2}, headers=H).status_code in (404, 405)
    assert "events" in client.get("/api/events").json()
