"""로그인: 체크박스(자동 로그인·아이디 저장) 오클릭 방지, Cloudflare 대기·자동 재개, 계정 일치, 무한 재시도 금지."""
import xml.etree.ElementTree as ET

import phone_login as pl

FORM = """<hierarchy><node class="android.webkit.WebView" bounds="[0,0][1080,2400]">
<node resource-id="loginId" class="android.widget.EditText" text="{id}" bounds="[39,494][1040,640]"/>
<node resource-id="password" class="android.widget.EditText" password="true" text="{pw}" bounds="[39,631][1040,775]"/>
<node resource-id="autoLogin" class="android.widget.CheckBox" text="자동 로그인" checked="false" bounds="[39,800][300,860]"/>
<node resource-id="saveId" class="android.widget.CheckBox" text="아이디 저장" checked="true" bounds="[320,800][600,860]"/>
{cf}
<node class="android.widget.Button" text="로그인" bounds="[39,1531][1040,1678]"/>
</node></hierarchy>"""
CF = '<node resource-id="cloudflare-captcha" class="android.view.View" bounds="[39,900][1040,1100]"><node class="android.widget.CheckBox" text="{t}" bounds="[60,950][120,1010]"/></node>'
ACCT = {"id": "tester01", "pw": "Pw!secret$1"}


def tree(cf_text=None, filled=True):
    cf = CF.format(t=cf_text) if cf_text is not None else ""
    return ET.fromstring(FORM.format(id=ACCT["id"] if filled else "", pw=ACCT["pw"] if filled else "", cf=cf))


def patch(monkeypatch, trees):
    taps, shells, notes = [], [], []
    seq = iter(trees)
    last = {"t": trees[-1]}

    def dump(retries=3):
        try:
            last["t"] = next(seq)
        except StopIteration:
            pass
        return last["t"]

    monkeypatch.setattr(pl, "dump_tree", dump)
    monkeypatch.setattr(pl, "tap", lambda xy: taps.append(tuple(xy)))
    monkeypatch.setattr(pl, "shell", lambda *a, **k: shells.append(a) or "")
    monkeypatch.setattr(pl, "hide_keyboard", lambda: None)
    monkeypatch.setattr(pl.time, "sleep", lambda s: None)
    monkeypatch.setattr(pl, "AUTH_HOOK", lambda kind, msg, **kw: notes.append(kind))
    return taps, shells, notes


def checkbox_hit(taps):
    return [t for t in taps if 800 <= t[1] <= 860 or (60 <= t[0] <= 120 and 950 <= t[1] <= 1010)]


def test_fill_form_never_taps_checkboxes(monkeypatch):
    taps, shells, _ = patch(monkeypatch, [tree()] * 10)
    assert pl.fill_form(ACCT, "", pl.load_layout(), tree(filled=False)) == ""
    assert not checkbox_hit(taps)
    assert taps == [((39 + 1040) // 2, (1531 + 1678) // 2)]   # 로그인 버튼만
    import re
    joined = " ".join(" ".join(map(str, s)) for s in shells)
    shell_taps = [(int(x), int(y)) for x, y in re.findall(r"input tap (\d+) (\d+)", joined)]
    assert shell_taps == [(539, 567), (539, 703)]   # 아이디·비번 칸만
    assert not checkbox_hit(shell_taps)


class Clock:
    """가짜 시계 — sleep 하면 시간이 그만큼 흐른다."""
    def __init__(self, monkeypatch):
        self.t = 1000.0
        self.dumps = []
        monkeypatch.setattr(pl.time, "monotonic", lambda: self.t)
        monkeypatch.setattr(pl.time, "sleep", self.sleep)

    def sleep(self, s):
        self.t += s


def cf_patch(monkeypatch, states):
    """states: 매 dump 마다 돌려줄 트리(마지막 것을 계속 반복). dump 1회는 0.5초 걸린다고 친다."""
    taps, _, notes = patch(monkeypatch, [tree()])
    clock = Clock(monkeypatch)
    seq = list(states)

    def dump(retries=3):
        clock.dumps.append(round(clock.t - 1000.0, 2))
        clock.t += 0.5
        return seq.pop(0) if len(seq) > 1 else seq[0]

    monkeypatch.setattr(pl, "dump_tree", dump)
    return taps, notes, clock


def test_cf_constants_60s_3s():
    assert pl.CF_MAX_WAIT == 60 and pl.CF_INTERVAL == 3
    assert not hasattr(pl, "CF_USER_WAIT") and not hasattr(pl, "CF_AUTO_WAIT")


def test_cf_success_immediately(monkeypatch):
    taps, notes, clock = cf_patch(monkeypatch, [tree("성공!")])
    assert pl.cloudflare_passed() is True
    assert clock.dumps == [0.0] and taps == [] and notes == []   # 처음 즉시 확인, 기다림 없음


def test_cf_polls_every_3s_and_proceeds_on_success(monkeypatch):
    taps, notes, clock = cf_patch(monkeypatch, [tree("확인 중")] * 4 + [tree("성공!")])
    assert pl.cloudflare_passed() is True
    assert clock.dumps == [0.0, 3.0, 6.0, 9.0, 12.0]   # 3초 간격, 성공 보자마자 끝
    assert taps == [] and notes == ["auth_required", "auth_done"]


def test_cf_timeout_60s(monkeypatch):
    taps, notes, clock = cf_patch(monkeypatch, [tree("사람인지 확인하십시오")])
    assert pl.cloudflare_passed() is False
    assert clock.dumps[0] == 0.0 and clock.dumps[-1] == 60.0 and len(clock.dumps) == 21
    assert all(b - a == 3.0 for a, b in zip(clock.dumps, clock.dumps[1:]))
    assert taps == [] and notes == ["auth_required", "auth_timeout"]


def test_cf_unreadable_is_not_success(monkeypatch):
    empty = ET.fromstring('<hierarchy><node class="android.webkit.WebView" bounds="[0,0][1080,2400]"/></hierarchy>')
    taps, notes, clock = cf_patch(monkeypatch, [None, empty, ET.fromstring("<hierarchy/>")])
    assert pl.cloudflare_passed() is False and clock.dumps[-1] == 60.0
    assert notes == ["auth_timeout"]


def test_cf_no_widget_needs_two_reads(monkeypatch):
    taps, notes, clock = cf_patch(monkeypatch, [tree()])
    assert pl.cloudflare_passed() is True and clock.dumps == [0.0, 3.0]
    # 첫 판독엔 위젯이 없다가 늦게 뜨면 성공으로 보지 않는다
    taps, notes, clock = cf_patch(monkeypatch, [tree(), tree("확인 중")])
    assert pl.cloudflare_passed() is False and notes == ["auth_required", "auth_timeout"]


def test_cf_pause_stop_react_immediately(monkeypatch):
    import pytest
    from state_machine import Paused
    taps, notes, clock = cf_patch(monkeypatch, [tree("확인 중")])

    def cancel():
        if clock.t - 1000.0 >= 4.0:
            raise Paused()

    monkeypatch.setattr(pl, "CANCEL_CHECK", cancel)
    with pytest.raises(Paused):
        pl.cloudflare_passed()
    assert clock.t - 1000.0 < 4.5   # 다음 3초 주기를 기다리지 않고 0.2초 안에 반응


def test_fill_form_success_taps_only_login(monkeypatch):
    taps, notes, clock = cf_patch(monkeypatch, [tree("확인 중"), tree("성공!")])
    monkeypatch.setattr(pl, "inputs_ok", lambda a, c, l: tree("확인 중"))
    assert pl.fill_form(ACCT, "", pl.load_layout(), tree(filled=False)) == ""
    assert taps == [((39 + 1040) // 2, (1531 + 1678) // 2)] and not checkbox_hit(taps)


def test_fill_form_cloudflare_timeout_no_login_tap(monkeypatch):
    taps, notes, clock = cf_patch(monkeypatch, [tree("사람인지 확인하십시오")])
    monkeypatch.setattr(pl, "inputs_ok", lambda a, c, l: tree("사람인지 확인하십시오"))
    assert pl.fill_form(ACCT, "", pl.load_layout(), tree(filled=False)) == "CLOUDFLARE_TIMEOUT"
    assert taps == []


def test_login_cloudflare_timeout_no_retry(monkeypatch):
    calls = []
    monkeypatch.setattr(pl, "load_account", lambda no: ACCT)
    monkeypatch.setattr(pl, "open_login_form", lambda: "LOGIN_FORM")
    monkeypatch.setattr(pl, "attempt", lambda no, a, l: calls.append(1) or "CLOUDFLARE_TIMEOUT")
    r = pl.login("1")
    assert r == {"ok": False, "status": "CLOUDFLARE_TIMEOUT", "attempts": 1} and len(calls) == 1


def test_account_matches(monkeypatch):
    monkeypatch.setattr(pl, "LAST_NICK", "tes*****")
    assert pl.account_matches(ACCT)[0]
    monkeypatch.setattr(pl, "LAST_NICK", "abc*****")
    assert not pl.account_matches(ACCT)[0]
    monkeypatch.setattr(pl, "LAST_NICK", "")
    assert not pl.account_matches(ACCT)[0]


def test_login_no_infinite_retry(monkeypatch):
    calls = []
    monkeypatch.setattr(pl, "load_account", lambda no: ACCT)
    monkeypatch.setattr(pl, "open_login_form", lambda: "LOGIN_FORM")
    monkeypatch.setattr(pl, "attempt", lambda no, a, l: calls.append(1) or "LOGIN_REJECTED")
    r = pl.login("1")
    assert not r["ok"] and len(calls) == pl.MAX_CONSEC_FAIL


def test_login_account_mismatch(monkeypatch):
    monkeypatch.setattr(pl, "load_account", lambda no: ACCT)
    monkeypatch.setattr(pl, "open_login_form", lambda: "LOGIN_FORM")

    def att(no, a, l):
        pl.LAST_NICK = "zzz****"
        return "LOGGED_IN"

    monkeypatch.setattr(pl, "attempt", att)
    r = pl.login("1")
    assert not r["ok"] and r["status"].startswith("ACCOUNT_MISMATCH")


def test_captcha_is_not_solved(monkeypatch):
    t = ET.fromstring(FORM.format(id="", pw="", cf='<node resource-id="autoBlockText" class="android.widget.EditText" bounds="[39,1222][1040,1346]"/>'))
    patch(monkeypatch, [t])
    code, _ = pl.solve_captcha(pl.load_layout())
    assert code is None


def test_dump_texts_hides_password():
    texts = [t["text"] for t in pl.dump_texts(tree())]
    assert ACCT["pw"] not in texts
