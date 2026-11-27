"""화면 읽기 공통 — 스크린샷 + UIAutomator dump + OCR 를 묶고, 같은 화면을 두 번 찍거나 읽지 않게 한다.

· 스크린샷은 짧은 시간(SHOT_TTL) 안에 다시 요청하면 직전 것을 쓴다. 탭·스와이프·키 입력(invalidate) 뒤엔 새로 찍는다.
· OCR 결과는 ocr_engine 이 이미지 바이트로 캐시한다 → 화면이 그대로면 다시 추론하지 않는다.
· 보안 인증 화면(카드앱 결제·비밀번호·키패드)은 일반 OCR 자동 조작 대상이 아니다 → ocr() 가 SecureScreen 을 던진다.
· confirm(): UIAutomator 와 OCR 가 같은 글자를 같은 자리에서 봤는지 교차 확인. 불명확하면 None(탭하지 않는다).
· changed(): 축소 이미지 차이로 화면 전환 감지 → 고정 sleep 대신 쓴다.
· cached_dump(): UIAutomator dump(1회 ≈ 3초) 재사용. 조건이 모두 맞을 때만 이전 dump 를 돌려준다:
    ① 그 dump 뒤로 탭·스와이프·키 입력이 없었다(invalidate 안 됨)
    ② 지금 스크린샷이 dump 직전 스크린샷과 같다(상태바 제외, 엄격 비교 — 숫자 한 글자만 바뀌어도 다름)
    ③ 보안 화면(카드앱)·검은 화면(FLAG_SECURE)이 아니다
  하나라도 아니면 새로 dump 한다. DUMP_CACHE=0 이면 끈다(전후 비교 측정용).
  비교용 '직전 스크린샷'은 이미 찍어 둔 것(OCR 등으로 0.35초 안에 찍은 것)만 쓴다 — 캐시 때문에 스크린샷을 더 찍지 않는다.
  (처음 설계는 dump 마다 스크린샷을 새로 찍었는데, 모의 측정에서 재사용은 0회이고 시간만 늘어서 바꿨다.)
"""
from __future__ import annotations

import os
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


DUMP_CACHE = os.environ.get("DUMP_CACHE", "1") != "0"

_shot = {"png": None, "t": 0.0, "img": None}
_focus = {"line": "", "t": 0.0}
_dump = {"tree": None, "sig": None, "epoch": -1, "end": 0.0}
COMPARE_MAX_AGE = 1.0   # dump 이후 찍힌 스크린샷을 '지금 화면'으로 볼 수 있는 최대 나이(초)
_epoch = [0]   # 입력할 때마다 +1
_stats = {"shots": 0, "shot_reuse": 0, "dumps": 0, "dump_reuse": 0, "dump_sec": 0.0}


def invalidate() -> None:
    """화면을 바꾸는 입력(탭·스와이프·키·앱 전환) 뒤에 부른다. 스크린샷·dump 캐시를 모두 버린다."""
    _shot["png"], _shot["img"], _shot["t"] = None, None, 0.0
    _focus["t"] = 0.0
    _epoch[0] += 1
    _dump["tree"], _dump["sig"] = None, None


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


# ---------- dump 캐시 ----------

STATUS_BAR = 100   # 1080x2400 기준 상단 상태바(시계·알림) 높이 — 비교에서 뺀다


def signature(png: bytes) -> np.ndarray:
    """화면 비교용 1/4 축소 회색 이미지(상태바 제외)."""
    import cv2
    img = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_GRAYSCALE)
    h, w = img.shape
    small = cv2.resize(img, (max(1, w // 4), max(1, h // 4)), interpolation=cv2.INTER_AREA)
    return small[max(1, round(STATUS_BAR * h / 2400) // 4):].astype(np.int16)


def same_screen(a: np.ndarray, b: np.ndarray) -> bool:
    """엄격 비교: 밝기 차이 24 넘는 픽셀이 4개 이하일 때만 같은 화면. 커서 깜빡임·숫자 변경도 '다름'."""
    return a.shape == b.shape and int(np.count_nonzero(np.abs(a - b) > 24)) <= 4


def blank(sig: np.ndarray) -> bool:
    """검은(또는 단색) 화면 — FLAG_SECURE 카드앱은 스크린샷이 검다. 이런 화면은 비교로 판단하지 않는다."""
    return float(sig.std()) < 2.0


def count_dump(sec: float) -> None:
    """실제 adb uiautomator dump 1회(재시도·빈 WebView 재촬영 포함 모두 센다)."""
    _stats["dumps"] += 1
    _stats["dump_sec"] += sec


def cached_dump(fetch, fresh: bool = False, cacheable=lambda tree: True):
    """fetch() = 실제 uiautomator dump(트리). 위 조건이면 이전 트리를 돌려준다.
    cacheable(tree) 가 거짓인 트리(빈 WebView 등 불완전한 dump)는 저장하지 않는다."""
    use = DUMP_CACHE and not fresh
    if use and _dump["tree"] is not None and _dump["epoch"] == _epoch[0] and not is_secure():
        # 지금 화면: dump 이후 찍힌 스크린샷이 있으면 그걸, 없으면 새로 한 장(0.3~0.6초 — dump 3초보다 싸다)
        recent = _shot["png"] is not None and _shot["t"] > _dump["end"] and time.monotonic() - _shot["t"] < COMPARE_MAX_AGE
        now_sig = signature(_shot["png"] if recent else screenshot_png(fresh=True))
        if not blank(now_sig) and same_screen(now_sig, _dump["sig"]):
            _stats["dump_reuse"] += 1
            return _dump["tree"]
    sig = None
    if use and _shot["png"] is not None and time.monotonic() - _shot["t"] < SHOT_TTL:
        # dump '직전' 화면 = 방금 찍어 둔 스크린샷(추가 촬영 없음). dump 중에 화면이 바뀌었다면
        # 다음 비교에서 다르게 나와 다시 dump 한다(안전한 쪽)
        before = signature(_shot["png"])
        sig = None if blank(before) else before
    epoch = _epoch[0]
    tree = fetch()
    if use and tree is not None and sig is not None and epoch == _epoch[0] and cacheable(tree) and not is_secure():
        _dump.update(tree=tree, sig=sig, epoch=epoch, end=time.monotonic())
    else:
        _dump["tree"], _dump["sig"] = None, None
    return tree


def reset_stats() -> None:
    for k in _stats:
        _stats[k] = 0.0 if k == "dump_sec" else 0


def stats() -> dict:
    out = dict(_stats)
    out["dump_sec"] = round(out["dump_sec"], 1)
    return out
