"""UIAutomator dump 캐시: 같은 화면·입력 없음일 때만 재사용, 화면 변화·입력·보안 화면이면 새로 dump."""
import io
import json
import subprocess
import sys

import pytest
from PIL import Image, ImageDraw

import screen_reader as sr
from conftest import ROOT


def png(text="A", digits="16,200", clock="12:00", color=(30, 30, 30)):
    img = Image.new("RGB", (1080, 2400), "white")
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, 1080, 90), fill=(240, 240, 240))
    d.text((20, 30), clock, fill=(0, 0, 0))                 # 상태바 시계
    d.rectangle((100, 400, 900, 600), fill=color)
    d.text((200, 1200), digits, fill=(0, 0, 0), font_size=60)
    d.text((200, 1500), text, fill=(0, 0, 0), font_size=60)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture()
def dev(monkeypatch):
    state = {"png": png(), "secure": False, "shots": 0}

    def shot():
        state["shots"] += 1
        return state["png"]

    monkeypatch.setattr(sr.adb, "screencap_png", shot)
    monkeypatch.setattr(sr, "is_secure", lambda line=None: state["secure"])
    monkeypatch.setattr(sr, "DUMP_CACHE", True)
    sr.invalidate()
    sr.reset_stats()
    return state


def fetcher():
    calls = {"n": 0}

    def fetch():
        calls["n"] += 1
        return {"tree": calls["n"]}
    return fetch, calls


def test_reuse_same_screen_no_input(dev):
    fetch, calls = fetcher()
    sr.screenshot_png()                      # OCR 등으로 방금 찍어 둔 스크린샷
    a = sr.cached_dump(fetch)
    sr.screenshot_png(fresh=True)            # 다음 OCR
    b = sr.cached_dump(fetch)
    assert a is b and calls["n"] == 1 and sr.stats()["dump_reuse"] == 1


def test_no_extra_screenshot_without_prior_shot(dev):
    fetch, calls = fetcher()
    sr.cached_dump(fetch)
    sr.cached_dump(fetch)
    assert calls["n"] == 2 and dev["shots"] == 0   # 캐시 때문에 스크린샷을 더 찍지 않는다


def test_input_invalidates(dev):
    fetch, calls = fetcher()
    sr.screenshot_png()
    sr.cached_dump(fetch)
    sr.invalidate()                          # 탭·스와이프·키 입력
    sr.screenshot_png()
    sr.cached_dump(fetch)
    assert calls["n"] == 2


@pytest.mark.parametrize("change", [dict(digits="16,201"), dict(text="B"), dict(color=(200, 30, 30))])
def test_small_screen_change_forces_new_dump(dev, change):
    fetch, calls = fetcher()
    sr.screenshot_png()
    sr.cached_dump(fetch)
    dev["png"] = png(**change)               # 금액 숫자 한 글자만 바뀌어도
    sr.screenshot_png(fresh=True)
    sr.cached_dump(fetch)
    assert calls["n"] == 2


def test_status_bar_clock_ignored(dev):
    fetch, calls = fetcher()
    sr.screenshot_png()
    sr.cached_dump(fetch)
    dev["png"] = png(clock="12:01")
    sr.screenshot_png(fresh=True)
    sr.cached_dump(fetch)
    assert calls["n"] == 1


def test_secure_and_black_screens_never_cached(dev):
    fetch, calls = fetcher()
    dev["secure"] = True                     # 카드앱 결제·인증 화면
    sr.screenshot_png()
    sr.cached_dump(fetch)
    sr.screenshot_png(fresh=True)
    sr.cached_dump(fetch)
    assert calls["n"] == 2
    dev["secure"] = False
    black = io.BytesIO()
    Image.new("RGB", (1080, 2400), "black").save(black, "PNG")
    dev["png"] = black.getvalue()            # FLAG_SECURE → 검은 스크린샷
    sr.invalidate()
    sr.screenshot_png()
    sr.cached_dump(fetch)
    sr.screenshot_png(fresh=True)
    sr.cached_dump(fetch)
    assert calls["n"] == 4


def test_incomplete_dump_not_cached_and_off_switch(dev, monkeypatch):
    fetch, calls = fetcher()
    sr.screenshot_png()
    sr.cached_dump(fetch, cacheable=lambda t: False)   # 빈 WebView 등
    sr.screenshot_png(fresh=True)
    sr.cached_dump(fetch)
    assert calls["n"] == 2
    monkeypatch.setattr(sr, "DUMP_CACHE", False)
    sr.screenshot_png(fresh=True)
    sr.cached_dump(fetch)
    sr.screenshot_png(fresh=True)
    sr.cached_dump(fetch)
    assert calls["n"] == 4
    sr.screenshot_png(fresh=True)
    monkeypatch.setattr(sr, "DUMP_CACHE", True)
    assert sr.cached_dump(fetch, fresh=True) and calls["n"] == 5


def test_bench_simulated_flows_fewer_dumps_same_results():
    """가상 폰 비교: 결과는 같고 dump 는 줄어야 한다 (캐시 켜짐/꺼짐 모두)."""
    out = {}
    for c in ("0", "1"):
        p = subprocess.run([sys.executable, str(ROOT / "tests" / "bench_dump.py"), "--src", str(ROOT / "phone_auto"),
                            "--cache", c, "--json"], capture_output=True, text=True, encoding="utf-8", timeout=120)
        out[c] = json.loads(p.stdout.strip().splitlines()[-1])
    expect = {"로그인(입력→Cloudflare 7초→로그인→계정 확인)": ("LOGGED_IN", 5),
              "로그인 실패 알림 → 확인": ("LOGIN_REJECTED", 4),        # 기존 6
              "링크 진입 화면 판정(상품 페이지)": ("PRODUCT", 0),         # 기존 1
              "바로구매→옵션 1번→수량 1→주문서": ("1(홍길동)", 3)}          # 기존 6
    for c in ("0", "1"):
        for name, (result, dumps) in expect.items():
            assert out[c][name]["result"] == result and out[c][name]["dump"] == dumps, (c, name, out[c][name])
