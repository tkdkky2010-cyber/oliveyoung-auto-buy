"""ADB 공통 모듈: 출력 파싱, 해상도 환산, 기기 잠금, 재연결."""
import adb_manager as adb

DEVICES = """List of devices attached
R3CN70ABCDE            device usb:1-1 product:o1sks model:SM_G996N device:o1s transport_id:3
192.168.0.10:5555      device product:o1sks model:SM_G996N device:o1s transport_id:5
adb-RFCNC0WRL9Z-iXGAm4._adb-tls-connect._tcp  offline transport_id:6
emulator-5554 unauthorized transport_id:7
"""


def test_parse_devices():
    d = adb.parse_devices(DEVICES)
    assert [x["serial"] for x in d] == ["R3CN70ABCDE", "192.168.0.10:5555",
                                        "adb-RFCNC0WRL9Z-iXGAm4._adb-tls-connect._tcp", "emulator-5554"]
    assert d[0]["transport"] == "usb" and d[1]["transport"] == "wifi" and d[2]["transport"] == "wifi"
    assert d[0]["model"] == "SM_G996N" and d[2]["state"] == "offline"


def test_parse_focus_and_size():
    out = "  mFocusedApp=x\n  mCurrentFocus=Window{a1 u0 com.oliveyoung/com.oliveyoung.MainActivity}\n  other"
    assert adb.parse_focus(out).startswith("mCurrentFocus=") and "com.oliveyoung" in adb.parse_focus(out)
    assert adb.parse_wm_size("Physical size: 1080x2400\nOverride size: 720x1600") == (720, 1600)
    assert adb.parse_wm_size("Physical size: 1440x3200") == (1440, 3200)


def test_scaling():
    adb.set_size_for_tests((720, 1600))
    try:
        assert adb.pt((1080, 2400)) == (720, 1600)
        assert adb.sy(2000) == 1333 and adb.sx(540) == 360
    finally:
        adb.set_size_for_tests(None)
    assert adb.pt((990, 2208)) == (990, 2208)


def test_ensure_device_rules(monkeypatch):
    monkeypatch.setitem(adb._state, "serial", "")
    monkeypatch.setattr(adb, "list_devices", lambda: adb.parse_devices(DEVICES))
    # 연결된(device) 기기가 2대 → 임의로 고르지 않는다
    try:
        adb.ensure_device()
        assert False
    except adb.AdbDisconnected as e:
        assert "여러 대" in str(e)
    monkeypatch.setitem(adb._state, "serial", "adb-RFCNC0WRL9Z-iXGAm4._adb-tls-connect._tcp")
    try:
        adb.ensure_device()   # offline 기기 → 진행 안 함
        assert False
    except adb.AdbDisconnected:
        pass


def test_reconnect_wifi(monkeypatch):
    calls = {"connect": 0, "n": 0}
    monkeypatch.setitem(adb._state, "serial", "192.168.0.10:5555")

    def is_conn(s=None):
        calls["n"] += 1
        return calls["connect"] >= 1

    monkeypatch.setattr(adb, "is_connected", is_conn)
    monkeypatch.setattr(adb, "connect", lambda a, timeout=5: calls.__setitem__("connect", calls["connect"] + 1) or True)
    monkeypatch.setattr(adb.time, "sleep", lambda s: None)
    assert adb.reconnect(timeout=5) is True and calls["connect"] == 1


def test_device_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(adb, "ROOT", tmp_path)
    a, b = adb.DeviceLock("192.168.0.10:5555"), adb.DeviceLock("192.168.0.10:5555")
    assert a.acquire()
    b.path.write_text("999999")   # 죽은 pid 의 잠금은 치운다
    assert b.acquire()
    b.release()
    assert not b.path.exists()


def test_find_adb_env(tmp_path, monkeypatch):
    fake = tmp_path / "adb"
    fake.write_text("")
    monkeypatch.setenv("ADB_PATH", str(fake))
    assert adb.find_adb() == str(fake)
