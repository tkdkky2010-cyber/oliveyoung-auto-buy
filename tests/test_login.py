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
    monkeypatch.setattr(pl, "AUTH_HOOK", lambda kind, msg: notes.append(kind))
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


def test_cloudflare_success_without_tap(monkeypatch):
    taps, _, notes = patch(monkeypatch, [tree("확인 중")] * 3 + [tree("성공!")])
    clock = iter(range(0, 1000, 10))
    monkeypatch.setattr(pl.time, "monotonic", lambda: next(clock))
    assert pl.cloudflare_passed(timeout=15, user_wait=300) is True
    assert taps == [] and notes == ["auth_required", "auth_done"]


def test_cloudflare_timeout_stops(monkeypatch):
    taps, _, notes = patch(monkeypatch, [tree("사람인지 확인하십시오")] * 100)
    clock = iter(range(0, 10000, 10))
    monkeypatch.setattr(pl.time, "monotonic", lambda: next(clock))
    assert pl.cloudflare_passed(timeout=15, user_wait=60) is False
    assert taps == [] and notes == ["auth_required", "auth_timeout"]


def test_fill_form_cloudflare_blocks_login_tap(monkeypatch):
    taps, _, _ = patch(monkeypatch, [tree("사람인지 확인하십시오")] * 200)
    clock = iter(range(0, 100000, 50))
    monkeypatch.setattr(pl.time, "monotonic", lambda: next(clock))
    assert pl.fill_form(ACCT, "", pl.load_layout(), tree(filled=False)) == "CLOUDFLARE"
    assert taps == []


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
