"""화면 읽기 공통 — 스크린샷 + UIAutomator dump + OCR 를 묶고, 같은 화면을 두 번 찍거나 읽지 않게 한다.

· 스크린샷은 짧은 시간(SHOT_TTL) 안에 다시 요청하면 직전 것을 쓴다. 탭·스와이프·키 입력(invalidate) 뒤엔 새로 찍는다.
· OCR 결과는 ocr_engine 이 이미지 바이트로 캐시한다 → 화면이 그대로면 다시 추론하지 않는다.
· 보안 인증 화면(카드앱 결제·비밀번호·키패드)은 일반 OCR 자동 조작 대상이 아니다 → ocr() 가 SecureScreen 을 던진다.
· confirm(): UIAutomator 와 OCR 가 같은 글자를 같은 자리에서 봤는지 교차 확인. 불명확하면 None(탭하지 않는다).
· changed(): 축소 이미지 차이로 화면 전환 감지 → 고정 sleep 대신 쓴다.
"""
from __future__ import annotations

import time

import numpy as np

import adb_manager as adb
import ocr_engine

SHOT_TTL = 0.35          # 초. 이 안의 재요청은 같은 스크린샷
OCR_MIN_CONF = 0.5       # 이보다 낮은 OCR 글자는 버린다
CONFIRM_CONF = 0.85      # dump 로 확인 못 할 때, OCR 단독으로 탭해도 되는 신뢰도
# 카드사 앱·결제 인증 화면. 여기서는 OCR 로 아무것도 누르지 않는다(키패드는 ocr_keypad 의 dump 전용 경로만).
SECURE_PACKAGES = ("com.hyundaicard.appcard", "com.lcacApp")
SECURE_ACTIVITIES = ("SimpleNumberAuthActivity", "PinAuth", "Keypad")


class SecureScreen(RuntimeError):
    pass


_shot = {"png": None, "t": 0.0, "img": None}
_focus = {"line": "", "t": 0.0}
_stats = {"shots": 0, "shot_reuse": 0}


def invalidate() -> None:
    """화면을 바꾸는 입력 뒤에 부른다."""
    _shot["png"], _shot["img"], _shot["t"] = None, None, 0.0
    _focus["t"] = 0.0


def focus_line() -> str:
    """dumpsys window 는 매번 부르면 느리다 → 스크린샷과 같은 TTL 로 재사용."""
    now = time.monotonic()
    if now - _focus["t"] >= SHOT_TTL:
        _focus.update(line=adb.focus(), t=time.monotonic())
    return _focus["line"]


def screenshot_png(fresh: bool = False) -> bytes:
    now = time.monotonic()
    if not fresh and _shot["png"] is not None and now - _shot["t"] < SHOT_TTL:
        _stats["shot_reuse"] += 1
        return _shot["png"]
    png = adb.screencap_png()
    _shot.update(png=png, t=time.monotonic(), img=None)
    _stats["shots"] += 1
    return png


def screenshot(fresh: bool = False):
    from PIL import Image
    import io
    png = screenshot_png(fresh)
    if _shot["img"] is None or _shot["png"] is not png:
        _shot["img"] = Image.open(io.BytesIO(png))
        _shot["img"].load()
    return _shot["img"]


def is_secure(line: str | None = None) -> bool:
    line = line if line is not None else focus_line()
    return any(p in line for p in SECURE_PACKAGES) or any(a in line for a in SECURE_ACTIVITIES)


def ocr(img=None, region=None, min_conf: float = OCR_MIN_CONF, check_secure: bool = True) -> list[dict]:
    """{text,cx,cy,src,confidence,box,bbox} 목록. img 를 안 주면 지금 화면."""
    if check_secure and is_secure():
        raise SecureScreen("보안 인증 화면 — 일반 OCR 자동 조작 대상이 아님")
    if img is None:
        img = screenshot_png()
    return ocr_engine.get_engine().ocr(img, region=region, min_conf=min_conf)


def _norm(s: str) -> str:
    return "".join(s.split())


def find(items: list[dict], word: str, min_y: int = 0, exact: bool = True, min_conf: float = 0.0) -> dict | None:
    w = _norm(word)
    for it in items:
        t = _norm(it["text"])
        if (t == w if exact else w in t) and it["cy"] >= min_y and it.get("confidence", 1.0) >= min_conf:
            return it
    return None


def confirm(word: str, dump_items: list[dict], ocr_items: list[dict], min_y: int = 0,
            tol: int = 60) -> dict | None:
    """같은 글자가 dump 와 OCR 양쪽에서 tol 픽셀 안에 보이면 그 위치(OCR 좌표). 한쪽만 보이면:
    OCR 단독은 신뢰도 CONFIRM_CONF 이상일 때만, dump 단독은 None(뒤쪽 숨은 WebView 글자일 수 있다)."""
    o = find(ocr_items, word, min_y)
    d = [it for it in dump_items if _norm(it["text"]) == _norm(word) and it["cy"] >= min_y]
    if o and any(abs(it["cx"] - o["cx"]) <= tol and abs(it["cy"] - o["cy"]) <= tol for it in d):
        return o
    if o and o.get("confidence", 0) >= CONFIRM_CONF and not d:
        return o
    return None


def thumb(png: bytes, size: int = 64) -> np.ndarray:
    import cv2
    img = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_GRAYSCALE)
    return cv2.resize(img, (size, size * 2), interpolation=cv2.INTER_AREA).astype(np.int16)


def diff(a: bytes, b: bytes) -> float:
    """두 스크린샷 차이(0~255 평균)."""
    return float(np.abs(thumb(a) - thumb(b)).mean())


def wait_change(before: bytes, timeout: float = 5, threshold: float = 2.0, poll: float = 0.15) -> bool:
    """화면이 바뀔 때까지(전환 감지). 바뀌면 True, 시간 안에 그대로면 False."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if diff(before, screenshot_png(fresh=True)) > threshold:
            return True
        time.sleep(poll)
    return False


def wait_stable(timeout: float = 5, threshold: float = 1.0, poll: float = 0.2) -> bytes:
    """연속 두 장이 거의 같을 때까지(애니메이션 끝). 마지막 스크린샷을 돌려준다."""
    prev = screenshot_png(fresh=True)
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        time.sleep(poll)
        cur = screenshot_png(fresh=True)
        if diff(prev, cur) <= threshold:
            return cur
        prev = cur
    return prev


def stats() -> dict:
    return dict(_stats)
