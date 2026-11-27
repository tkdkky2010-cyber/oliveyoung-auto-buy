"""UIAutomator dump 호출 횟수·처리시간 비교 — 가상 폰(모의 기기) + 가상 시계.

실제 폰이 아니다. 같은 화면 흐름을 기존 코드와 수정 코드에 똑같이 흘려 dump 호출 수를 센다.
시간은 비용 모델로 계산한다: dump 3.0초(ORDER_STEPMAP 9 실측), 스크린샷 0.6초(가정), OCR 0.9초(OCR_BENCHMARK 평균),
그 밖의 adb 명령 0.15초, dumpsys window 0.2초. 실제 기기 측정값이 아니므로 '추정'이다.

사용:  python tests/bench_dump.py --src <phone_auto 폴더> [--cache 0|1] [--json]
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time as _time_mod
from pathlib import Path

COST = {"dump": 3.0, "screencap": 0.6, "ocr": 0.9, "shell": 0.15, "focus": 0.2}


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def time(self):
        return self.t

    def sleep(self, s):
        self.t += max(0.0, s)


CLOCK = Clock()


def install_clock():
    _time_mod.time = CLOCK.time
    _time_mod.monotonic = CLOCK.time
    _time_mod.sleep = CLOCK.sleep
    _time_mod.perf_counter = CLOCK.time


# ---------- 화면 정의 ----------

def node(cls, text="", rid="", bounds=(0, 0, 0, 0), desc="", extra=""):
    x1, y1, x2, y2 = bounds
    return (f'<node class="android.widget.{cls}" text="{text}" resource-id="{rid}" content-desc="{desc}" '
            f'bounds="[{x1},{y1}][{x2},{y2}]" {extra}/>')


def webview(*children):
    return ('<?xml version="1.0"?><hierarchy><node class="android.webkit.WebView" bounds="[0,0][1080,2400]">'
            + "".join(children) + "</node></hierarchy>")


ACCT = {"id": "tester01", "pw": "Pw!secret$1"}

SCREENS = {
    "LOGIN_CF_WAIT": dict(
        xml=webview(node("EditText", ACCT["id"], "loginId", (39, 494, 1040, 640)),
                    node("EditText", ACCT["pw"], "password", (39, 631, 1040, 775), extra='password="true"'),
                    '<node class="android.view.View" resource-id="cloudflare-captcha" bounds="[39,900][1040,1100]">'
                    + node("CheckBox", "확인 중", "", (60, 950, 120, 1010)) + "</node>",
                    node("Button", "로그인", "", (39, 1531, 1040, 1678))),
        ocr=[("로그인", 540, 1604)]),
    "LOGIN_CF_OK": dict(
        xml=webview(node("EditText", ACCT["id"], "loginId", (39, 494, 1040, 640)),
                    node("EditText", ACCT["pw"], "password", (39, 631, 1040, 775), extra='password="true"'),
                    '<node class="android.view.View" resource-id="cloudflare-captcha" bounds="[39,900][1040,1100]">'
                    + node("CheckBox", "성공!", "", (60, 950, 120, 1010)) + "</node>",
                    node("Button", "로그인", "", (39, 1531, 1040, 1678))),
        ocr=[("로그인", 540, 1604), ("성공!", 200, 980)]),
    "ALERT": dict(xml=webview(node("TextView", "아이디 또는 비밀번호가 일치하지 않습니다.", "message", (100, 1000, 980, 1100)),
                              node("Button", "확인", "button1", (794, 1161, 974, 1313))),
                  ocr=[("아이디 또는 비밀번호가 일치하지 않습니다.", 540, 1050), ("확인", 884, 1237)]),
    "LOADING": dict(xml=webview(node("ProgressBar", "", "loading", (440, 1100, 640, 1300))), ocr=[]),
    "HOME": dict(xml=webview(node("View", "", "tabBar", (0, 2100, 1080, 2300))),
                 ocr=[("홈", 90, 2208), ("마이", 990, 2208)]),
    "MY": dict(xml=webview(node("TextView", "tes*****", "lblNickName", (60, 300, 600, 380))),
               ocr=[("마이페이지", 540, 150), ("마이", 990, 2208)]),
    "PRODUCT": dict(xml=webview(node("TextView", "헤이즐넛 바디워시", "", (40, 1200, 1000, 1300))),
                    ocr=[("헤이즐넛 바디워시", 520, 1250), ("장바구니", 300, 2160), ("바로구매", 835, 2160)]),
    "OPTIONS": dict(xml=webview(node("Button", "헤이즐넛 헤이즐넛 10% 16,200원", "", (84, 1300, 995, 1450)),
                                node("Button", "바닐라 바닐라 10% 16,200원", "", (84, 1460, 995, 1610))),
                    ocr=[("헤이즐넛", 300, 1375), ("바닐라", 300, 1535)]),
    "SELECTED": dict(xml=webview(node("TextView", "헤이즐넛", "", (84, 1300, 600, 1360)),
                                 node("EditText", "1", "", (500, 1500, 600, 1580)),
                                 node("Button", "", "", (400, 1500, 480, 1580), desc="수량 증가"),
                                 node("Button", "바로구매", "", (630, 2088, 1040, 2232))),
                     ocr=[("헤이즐넛", 300, 1330), ("바로구매", 835, 2160)]),
    "ORDER": dict(xml=webview(node("TextView", "1(홍길동)", "addrSummaryName", (40, 300, 600, 360)),
                              node("TextView", "기본배송지", "baseDlvpYnFlag", (620, 300, 800, 360))),
                  ocr=[("주문서", 540, 150), ("1(홍길동)", 300, 330)]),
}


def png_for(name):
    from PIL import Image, ImageDraw
    if name not in png_for.cache:
        img = Image.new("RGB", (1080, 2400), "white")
        d = ImageDraw.Draw(img)
        h = abs(hash(name)) % 1500
        d.rectangle((40, 200 + h, 1040, 500 + h), fill=(30, 30, 30))      # 화면마다 다른 모양
        for text, x, y in SCREENS[name]["ocr"]:
            d.rectangle((x - 80, y - 25, x + 80, y + 25), fill=(90, 90, 90))
        buf = io.BytesIO()
        img.save(buf, "PNG")
        png_for.cache[name] = buf.getvalue()
    return png_for.cache[name]


png_for.cache = {}


class FakePhone:
    """화면 상태 + 탭에 따른 전환. Cloudflare 는 로그인 폼이 뜨고 CF_SEC 초 뒤 '성공!'."""
    CF_SEC = 7.0
    LOAD_SEC = 6.0

    def __init__(self, screen, reject=False):
        self.screen, self.since, self.reject = screen, CLOCK.t, reject
        self.counts = {"dump": 0, "screencap": 0, "ocr": 0, "shell": 0, "focus": 0}

    def cur(self):
        if self.screen == "LOGIN_CF_WAIT" and CLOCK.t - self.since >= self.CF_SEC:
            self.go("LOGIN_CF_OK")
        if self.screen == "LOADING" and CLOCK.t - self.since >= self.LOAD_SEC:
            self.go("PRODUCT")
        return self.screen

    def go(self, s):
        self.screen, self.since = s, CLOCK.t

    def cost(self, kind):
        self.counts[kind] += 1
        CLOCK.t += COST[kind]

    # adb 대역
    def dump_xml(self, *a, **k):
        self.cost("dump")
        return SCREENS[self.cur()]["xml"]

    def screencap_png(self):
        self.cost("screencap")
        return png_for(self.cur())

    def focus(self):
        self.cost("focus")
        return "mCurrentFocus=Window{1 u0 com.oliveyoung/com.oliveyoung.MainActivity}"

    def shell(self, *args, **k):
        self.cost("shell")
        cmd = " ".join(map(str, args))
        if "dumpsys input_method" in cmd:
            return "mInputShown=false"
        if "dumpsys window" in cmd:
            return self.focus()
        import re
        for x, y in re.findall(r"input tap (\d+) (\d+)", cmd):
            self.tap(int(x), int(y))
        return ""

    def tap(self, x, y):
        s = self.cur()
        if s == "LOGIN_CF_OK" and 1531 <= y <= 1678:
            self.go("ALERT" if self.reject else "HOME")
        elif s == "ALERT" and 1161 <= y <= 1313:
            self.go("LOGIN_CF_OK")
        elif s == "HOME" and y > 2000 and x > 800:
            self.go("MY")
        elif s == "PRODUCT" and y > 2000 and x > 600:
            self.go("OPTIONS")
        elif s == "OPTIONS" and 1300 <= y <= 1450:
            self.go("SELECTED")
        elif s == "SELECTED" and y > 2000 and x > 600:
            self.go("ORDER")

    def ocr_texts(self, img=None, region=None):
        self.cost("ocr")
        return [{"text": t, "cx": x, "cy": y, "src": "ocr", "confidence": 0.99} for t, x, y in SCREENS[self.cur()]["ocr"]]


def run(src: Path, cache: bool) -> dict:
    os.environ["DUMP_CACHE"] = "1" if cache else "0"
    sys.path.insert(0, str(src))
    install_clock()
    import adb_manager as adb
    import phone_login as pl
    import threads_buy as tb
    results = {}

    def scenario(name, start, fn, **kw):
        ph = FakePhone(start, **kw)
        adb.dump_xml, adb.screencap_png, adb.focus = ph.dump_xml, ph.screencap_png, ph.focus
        adb.shell = ph.shell
        pl.ocr_texts = ph.ocr_texts
        try:
            import screen_reader as sr
            sr.invalidate()
            sr._shot["png"] = None
            sr.is_secure = lambda line=None: False
        except ImportError:
            pass
        t0 = CLOCK.t
        out = fn()
        results[name] = {"result": str(out), "sec": round(CLOCK.t - t0, 1), **ph.counts}

    pl.load_account = lambda no: ACCT
    scenario("로그인(입력→Cloudflare 7초→로그인→계정 확인)", "LOGIN_CF_WAIT",
             lambda: pl.attempt("1", ACCT, pl.load_layout()))
    scenario("로그인 실패 알림 → 확인", "LOGIN_CF_WAIT", lambda: pl.attempt("1", ACCT, pl.load_layout()), reject=True)
    scenario("링크 진입 화면 판정(상품 페이지)", "PRODUCT", lambda: tb.wait_landing())
    scenario("링크 진입 화면 판정(로그인 폼)", "LOGIN_CF_OK", lambda: tb.wait_landing())
    scenario("링크 진입 화면 판정(로딩 6초 → 상품)", "LOADING", lambda: tb.wait_landing())
    scenario("바로구매→옵션 1번→수량 1→주문서", "PRODUCT", lambda: tb.pp.text_of(tb.buy_now(1, 1), "addrSummaryName"))
    return results


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--cache", default="1")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    res = run(Path(a.src).resolve(), a.cache == "1")
    if a.json:
        print(json.dumps(res, ensure_ascii=False))
    else:
        for k, v in res.items():
            print(k, v)
    return 0


if __name__ == "__main__":
    sys.exit(main())
