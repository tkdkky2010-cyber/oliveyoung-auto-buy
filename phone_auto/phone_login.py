"""올리브영 폰 앱 로그인 — ADB + uiautomator dump/OCR 병합.

흐름 (이 모듈 = 폰 결제 순서 0번):
  detect_screen() 으로 현재 화면 판정 → 로그인 화면이면
  ID/PW 입력 → 값 검증 → Cloudflare 인증 상태 확인 → 로그인 → 마이 탭 닉네임으로 성공·계정 일치 판정.

2026-11-27 이후 로그인 폼의 글자 캡차(autoBlockText)는 없어지고 Cloudflare 위젯으로 바뀌었다.
글자 캡차가 다시 보이면 자동 판독하지 않고 CAPTCHA_UNSUPPORTED 로 멈춘다(외부 OCR/LLM API 를 쓰지 않는다).
체크박스(자동 로그인·아이디 저장)는 어떤 경우에도 누르지 않는다 — 지금 상태를 그대로 둔다.
Cloudflare 위젯은 즉시 확인 후 3초마다 다시 보며 최대 60초 기다린다. '성공!' 이 보이면 바로 로그인 버튼을 누르고,
60초 안에 안 되면 그 계정 로그인을 실패로 멈춘다(체크 칸을 대신 누르지 않는다).

재시도 규칙:
  · 시도는 최대 3회(MAX_ATTEMPTS). 로그인 제출 실패가 2회 연속(MAX_CONSEC_FAIL)이면 그 계정은 중단.

화면 요소 찾기 (좌표 하드코딩 최소화):
  dump 노드(resource-id / text / content-desc / hint / class) → 없으면 OCR 텍스트
  → 그래도 없으면 layout 파일의 실측 좌표 xy. 선택자·좌표는 oy_login_layout.json 으로 덮어쓴다.
  고정 좌표는 1080x2400 실측값이며 adb_manager.pt() 로 기기 해상도에 맞춰 환산한다.

아이디·비번은 accounts.json 에서 직접 읽고 화면·로그에 출력하지 않는다.
기기는 adb_manager 가 고른 serial(또는 ANDROID_SERIAL)을 쓴다.

사용:
  python phone_auto/phone_login.py login 1     # 1번 계정 로그인
  python phone_auto/phone_login.py logout      # 마이페이지 맨 아래 로그아웃
  python phone_auto/phone_login.py detect      # 현재 화면 판정만
  python phone_auto/phone_login.py dump        # 실측용 dump xml + 스크린샷 저장
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import shlex
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from PIL import Image

import adb_manager as adb
import screen_reader as sr

AUTH_HOOK = None          # callable(kind, message, **info) — 인증 대기 시작·성공·시간 초과 알림 (작업 엔진이 설정)
CANCEL_CHECK = None       # callable() — 일시정지·중지 요청이면 예외를 던진다 (작업 엔진이 설정)
CF_MAX_WAIT = 60          # Cloudflare 인증 최대 대기(초)
CF_INTERVAL = 3           # 인증 상태 재확인 간격(초)
CF_LAST = {"tree": None}  # cloudflare_passed 가 통과를 확인한 dump


def _notify(kind: str, message: str, **info) -> None:
    print(f"[phone_login] {message}", flush=True)
    if AUTH_HOOK:
        AUTH_HOOK(kind, message, **info)   # 상태 전환 실패 등은 숨기지 않는다


class AdbCallError(RuntimeError):
    """예전 subprocess.CalledProcessError 자리. 메시지에 명령 인자(비번)를 넣지 않는다."""


def shell(*args, capture_output=False):
    try:
        out = adb.shell(*args)
    except adb.AdbError as e:
        raise AdbCallError(str(e)) from None
    finally:
        if args and not str(args[0]).startswith(("dumpsys", "wm", "getprop")):
            sr.invalidate()   # 입력·앱 전환 뒤엔 스크린샷을 새로 찍는다
    return out if capture_output else None


def adb_screencap() -> Image.Image:
    return sr.screenshot()


def get_ui_hierarchy() -> ET.Element:
    try:
        return ET.fromstring(adb.dump_xml())
    except adb.AdbError as e:
        raise AdbCallError(str(e)) from None


ROOT = Path(__file__).resolve().parent.parent
ACCOUNTS = ROOT / "accounts.json"
LAYOUT_FILE = Path(__file__).resolve().parent / "oy_login_layout.json"
LOG_DIR = ROOT / "logs"
MAX_ATTEMPTS = 3
MY_TAB = (990, 2208)   # 하단 '마이' 탭 (2026-11-05 실측)
FORM_OPEN = False      # logout() 이 이미 로그인 폼을 보고 있으면 True
LAST_NICK = ""         # 마지막으로 확인한 마이페이지 닉네임(마스킹). 결제 전 계정 확인용
MAX_CONSEC_FAIL = 2
RESULT_TIMEOUT = 12   # 로그인 버튼 후 결과 화면 대기(초)

# 요소별 선택자. 키: rid(resource-id 끝부분 정확일치) / text(정확일치) / desc·hint(부분일치) / cls
#                   / index(같은 cls 중 n번째) / xy(실측 좌표) / box(캡차 크롭 실측 박스)
# 2026-11-04 실측 (앱 ver 3.59.0, SM 1080x2400): 로그인 화면 = MainActivity 위 WebView.
#   dump 에 resource-id 가 HTML id 그대로 나온다. 같은 WebView 사본이 2개 + 뒤쪽 홈 WebView 노드도 같이 잡힌다.
#   ⚠️ dump 는 password 필드 값을 **평문**으로 준다 → dump_texts/save_dump 에서 가린다.
#   ⚠️ 캡차는 android.widget.Image 라 사용자 휴리스틱(ImageView)으로는 안 잡힌다 → desc 로 찾는다.
DEFAULT_LAYOUT = {
    "id_field":        {"rid": "loginId"},                                    # [39,494][1040,640]
    "pw_field":        {"rid": "password"},                                   # [39,631][1040,775]
    # 자동 로그인 및 아이디 저장 체크박스는 조작하지 않는다.
    "captcha_field":   {"rid": "autoBlockText"},                              # [39,1222][1040,1346] — 2026-11-27 이후 없음
    "captcha_image":   {"desc": "자동 입력방지 문자 입력 이미지", "cls": "Image"},  # [42,1003][759,1197]
    "captcha_refresh": {"rid": "reLoad"},                                     # [756,1000][1040,1101]
    "login_button":    {"text": "로그인", "cls": "Button"},                    # [39,1531][1040,1678] (rid 없음)
    "error_ok":        {"rid": "button1"},                                    # 네이티브 AlertDialog '확인' [794,1161][974,1313]
}

# (화면명, 판정함수(t=화면 텍스트, ids=보이는 resource-id 집합)) — 위에서부터 먼저 맞는 것.
# 홈 WebView 노드가 로그인 화면 뒤에 같이 잡히므로 텍스트('장바구니' 등)로 로그인 여부를 보면 오판한다 → rid 로 본다.
SCREEN_MARKERS = [
    ("CAPTCHA_WRONG",  lambda t, ids: "방지문자" in t and "일치하지" in t),        # ⚠️ 문구 미실측
    ("LOGIN_REJECTED", lambda t, ids: "일치하지" in t or "잘못 입력" in t),         # ⚠️ 문구 미실측
    # 로그인 오류는 네이티브 AlertDialog(message + button1, 실측). 문구가 위 둘에 안 걸리면 여기로.
    #   실측 문구: '자동입력 방지 문자를 입력해주세요.' (캡차 칸 비었을 때)
    ("ALERT",          lambda t, ids: "message" in ids and "button1" in ids),
    # 2026-11-27: 글자 캡차(autoBlockText) 제거됨 → loginId+password 로 판정 (Cloudflare 위젯 여부와 무관)
    ("LOGIN_FORM",     lambda t, ids: "loginId" in ids and "password" in ids),
    # 홈(하단 탭바 보임). ★로그인 여부는 여기서 판정하지 않는다 — 로그아웃 상태 홈도 똑같다(2026-11-04 실측 오판).
    #   로그인 여부는 is_logged_in() = 마이 탭 → 로그인 폼이 뜨면 로그아웃 상태(실측).
    ("HOME",           lambda t, ids: "loginId" not in ids and "tabBar" in ids),
]


def load_layout() -> dict:
    layout = {k: dict(v) for k, v in DEFAULT_LAYOUT.items()}
    if LAYOUT_FILE.exists():
        for k, v in json.loads(LAYOUT_FILE.read_text(encoding="utf-8")).items():
            layout[k] = v
    return layout


def load_accounts() -> dict:
    if not ACCOUNTS.exists():
        raise ValueError("accounts.json 이 없음 (accounts.example.json 참고)")
    try:
        return json.loads(ACCOUNTS.read_text(encoding="utf-8"))
    except ValueError:
        raise ValueError("accounts.json 형식 오류") from None


def load_account(no: str) -> dict:
    acct = load_accounts().get(str(no))
    if not acct or not acct.get("id") or not acct.get("pw"):
        raise ValueError(f"accounts.json 에 {no}번 id/pw 가 비어 있음")
    return acct


# ---------- 화면 읽기: dump + OCR 병합 ----------

def _bounds(node: ET.Element) -> tuple[int, int, int, int] | None:
    m = re.match(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', node.attrib.get('bounds', ''))
    return tuple(map(int, m.groups())) if m else None


def dump_tree(retries: int = 3) -> ET.Element | None:
    """uiautomator dump — 화면 전환 중엔 'could not get idle state' 로 실패하므로 몇 번 재시도."""
    for _ in range(retries):
        try:
            return fill_empty_webview(get_ui_hierarchy())
        except (AdbCallError, ET.ParseError) as e:
            print(f"   [dump] 실패, 재시도: {e}", flush=True)
            time.sleep(0.7)
    return None


def _empty_webview(tree: ET.Element) -> bool:
    return any(n.attrib.get("class") == "android.webkit.WebView" and _visible(n) and len(n) == 0
               for n in tree.iter("node"))


def fill_empty_webview(tree: ET.Element, extra: int = 2) -> ET.Element:
    """2026-11-27 실측: dump 가 보이는 WebView 를 **자식 0개**(뼈대 14KB)로 줄 때가 잦다 — 바로 다시 찍으면 290개.
    이 상태면 마이페이지 닉네임·로그아웃 같은 WebView 글자를 못 봐 FAIL 로 빠진다 → 최대 extra 번 다시 찍는다."""
    for _ in range(extra):
        if not _empty_webview(tree):
            break
        tree = get_ui_hierarchy()
    return tree


def _visible(node: ET.Element) -> bool:
    b = _bounds(node)
    return bool(b) and b[2] > b[0] and b[3] > b[1]   # 숨은 노드는 [0,0][0,0]


def dump_ids(tree: ET.Element | None) -> set[str]:
    """보이는 노드의 resource-id 끝부분 집합 (네이티브 'pkg:id/x' → 'x', WebView 는 HTML id 그대로)."""
    nodes = tree.iter('node') if tree is not None else []
    return {n.attrib['resource-id'].split('/')[-1] for n in nodes if n.attrib.get('resource-id') and _visible(n)}


def dump_texts(tree: ET.Element | None) -> list[dict]:
    """dump 의 text/content-desc 를 OCR 항목과 같은 형식({text,cx,cy,src})으로. 비번 노드는 뺀다(평문 노출)."""
    out = []
    for node in (tree.iter('node') if tree is not None else []):
        if node.attrib.get('password') == 'true':
            continue
        b = _bounds(node)
        label = node.attrib.get('text') or node.attrib.get('content-desc')
        if b and label:
            out.append({"text": label, "cx": (b[0] + b[2]) // 2, "cy": (b[1] + b[3]) // 2, "src": "dump"})
    return out


def ocr_texts(img: Image.Image | None = None, region=None) -> list[dict]:
    """PP-OCRv6 + korean PP-OCRv5 (ocr_engine). {text,cx,cy,src} + confidence/box/bbox. 좌표는 원본 해상도.
    img 를 안 주면 지금 화면. 보안 인증 화면(카드앱)에서는 빈 목록 — 일반 OCR 로 조작하지 않는다."""
    try:
        return sr.ocr(img, region=region)
    except sr.SecureScreen:
        print("[phone_login] 보안 인증 화면 — OCR 생략", flush=True)
        return []


def read_screen() -> tuple[Image.Image, ET.Element | None, list[dict]]:
    """스크린샷 + dump 트리 + (OCR ∪ dump) 텍스트. WebView 는 dump 가, 네이티브 흰글씨 버튼은 OCR 이 빈다."""
    img = adb_screencap()
    tree = dump_tree()
    return img, tree, ocr_texts(img) + dump_texts(tree)


def detect_screen() -> str:
    tree = dump_tree()   # 판정은 dump 로 충분하다 (스크린샷·OCR 생략)
    t, ids = " ".join(it["text"] for it in dump_texts(tree)), dump_ids(tree)
    for name, pred in SCREEN_MARKERS:
        if pred(t, ids):
            return name
    return "UNKNOWN"


# ---------- 요소 찾기 ----------

def _node_matches(node: ET.Element, sel: dict) -> bool:
    a = node.attrib
    if not _visible(node):
        return False
    # rid·text 는 정확일치 — 부분일치면 'captcha'↔'captchaSpan', '로그인'↔'카카오로 로그인' 이 먼저 잡힌다(실측)
    if sel.get("rid") and a.get("resource-id", "").split("/")[-1] != sel["rid"]:
        return False
    if sel.get("text") and a.get("text", "").strip() != sel["text"]:
        return False
    if sel.get("desc") and sel["desc"] not in a.get("content-desc", ""):
        return False
    # 빈 EditText 는 dump 에서 hint 가 text 로 나오는 기기도 있어 hint 는 text 도 본다
    if sel.get("hint") and sel["hint"] not in a.get("hint", "") + a.get("text", ""):
        return False
    return not sel.get("cls") or a.get("class", "").endswith("." + sel["cls"])


def find_node_bounds(tree: ET.Element | None, sel: dict) -> tuple[int, int, int, int] | None:
    """선택자에 맞는 dump 노드 bounds. 키 속성이 하나도 안 맞으면 cls+index 로 한 번 더."""
    if tree is None:
        return None
    nodes = list(tree.iter('node'))
    hits = [n for n in nodes if _node_matches(n, sel)]
    if not hits and sel.get("cls") and "index" in sel:
        same_cls = [n for n in nodes if sel["cls"] in n.attrib.get("class", "")]
        hits = same_cls[sel["index"]:sel["index"] + 1]
    return _bounds(hits[0]) if hits else None


def locate(name: str, layout: dict, tree: ET.Element | None, texts: list[dict]) -> tuple[int, int] | None:
    """요소 중심 좌표: dump 노드 → OCR 텍스트 → 실측 xy 순."""
    sel = layout[name]
    b = find_node_bounds(tree, sel)
    if b:
        return (b[0] + b[2]) // 2, (b[1] + b[3]) // 2
    for word in (sel.get("text"), sel.get("hint"), sel.get("desc")):
        hit = next((it for it in texts if word and it["text"].strip() == word), None)
        if hit:
            return hit["cx"], hit["cy"]
    return tuple(sel["xy"]) if sel.get("xy") else None


# ---------- 입력 ----------

def tap(xy: tuple[int, int]) -> None:
    shell("input", "tap", str(int(xy[0])), str(int(xy[1])))


def type_into(xy: tuple[int, int], value: str, clear: int = 30) -> None:
    """필드 탭 → 기존 글자 지우기 → 입력. 값은 로그에 남기지 않는다.
    adb shell 은 원격 sh 가 해석하므로 특수문자 비번($,&,! 등)은 shlex.quote 로 감싼다."""
    tap(xy); time.sleep(0.2)
    if clear:
        shell("input", "keyevent", "KEYCODE_MOVE_END", *["KEYCODE_DEL"] * clear)
    shell("input", "text", shlex.quote(value.replace(" ", "%s")))
    time.sleep(0.3)


# ---------- 로그인 ----------

def solve_captcha(layout: dict) -> tuple[str | None, ET.Element | None]:
    """캡차 칸이 없으면 ("", tree). 글자 캡차가 다시 생겼으면 (None, tree) — 자동 판독하지 않는다
    (외부 OCR·LLM API 미사용 방침). 로그인은 CAPTCHA_UNSUPPORTED 로 멈춘다."""
    tree = dump_tree()
    if find_node_bounds(tree, layout["captcha_field"]) is None and "loginId" in dump_ids(tree):
        return "", tree
    if "loginId" in dump_ids(tree):
        print("[phone_login] 글자 캡차 칸이 보임 — 자동 판독 안 함", flush=True)
    return None, tree


def fill_fast(acct: dict, code: str, layout: dict, tree: ET.Element | None) -> ET.Element | None:
    """캡차를 읽은 dump 로 세 칸을 바로 채운다. 아이디·비번은 adb 세션 하나로 묶어 입력한다.
    키보드는 캡차 칸만 덮으므로(실측) 그 앞에서만 내린다. 틀리면 None → fill_field 경로로 다시 채운다."""
    if tree is None:
        return None
    spots = {}
    for name in ("id_field", "pw_field", "captcha_field")[:3 if code else 2]:
        node = next((n for n in tree.iter('node') if _node_matches(n, layout[name])), None)
        if node is None or not _visible(node):
            print(f"[phone_login] 빠른 입력 불가: {name}", flush=True)
            return None
        x1, y1, x2, y2 = _bounds(node)
        # 저장된 아이디가 미리 들어 있으면 뒤에 이어 붙는다(실측) → 있는 글자 수만큼 먼저 지운다
        existing = len(node.attrib.get("text", ""))
        clear = f"; input keyevent KEYCODE_MOVE_END {' '.join(['KEYCODE_DEL'] * existing)}" if existing else ""
        spots[name] = f"input tap {(x1 + x2) // 2} {(y1 + y2) // 2}; sleep 0.3{clear}"
    def typed(name: str, value: str) -> str:
        # 원격 sh 가 해석하므로 quote 1번. 실패해도 명령(비번 포함)이 예외 메시지로 나가지 않게 한다
        return f"{spots[name]}; input text {shlex.quote(value.replace(' ', '%s'))}; sleep 0.2"
    t0 = time.time()
    try:
        shell(f"{typed('id_field', acct['id'])}; {typed('pw_field', acct['pw'])}")
        t1 = time.time()
        hide_keyboard()
        t2 = time.time()
        if code:
            shell(typed("captcha_field", code))
    except AdbCallError:
        raise RuntimeError("로그인 입력 중 adb 실패") from None
    t3 = time.time()
    result = inputs_ok(acct, code, layout)
    print(f"[phone_login] 아이디·비번 {t1 - t0:.1f}s / 키보드 {t2 - t1:.1f}s / 캡차 {t3 - t2:.1f}s / 값 확인 {time.time() - t3:.1f}s", flush=True)
    return result


def fill_field(name: str, value: str, layout: dict) -> bool:
    """키보드를 내리고 → 화면을 다시 읽어 칸 위치를 새로 찾고 → 입력.
    ★2026-11-04 실측 사고: 비번 입력 후 올라온 키보드가 캡차 칸(y=1284)을 덮어 탭이 안 먹고,
      캡차 6자리가 **비번 칸에** 들어갔다(비번 칸 점 6개 + '자동입력 방지 문자를 입력해주세요.')."""
    hide_keyboard()
    tree = dump_tree()
    xy = locate(name, layout, tree, dump_texts(tree))
    if xy is None:
        return False
    type_into(xy, value)
    return True


def inputs_ok(acct: dict, code: str, layout: dict) -> ET.Element | None:
    """dump 로 세 칸 값이 맞게 들어갔는지 확인하고, 맞으면 그 트리를 돌려준다(로그인 버튼 위치에 재사용).
    dump 는 비번도 평문으로 주므로 메모리에서만 비교, 출력 안 함."""
    hide_keyboard()
    tree = dump_tree()
    want = {"id_field": acct["id"], "pw_field": acct["pw"]}
    if code:
        want["captcha_field"] = code
    for name, value in want.items():
        node = next((n for n in (tree.iter('node') if tree is not None else []) if _node_matches(n, layout[name])), None)
        if node is None or node.attrib.get("text", "") != value:
            print(f"[phone_login] 입력값 불일치: {name} (값은 출력 안 함)", flush=True)
            return None
    return tree


def fill_form(acct: dict, code: str, layout: dict, tree: ET.Element | None = None) -> str:
    """ID/PW/캡차 입력 → 값 검증 → 인증 상태 확인 → 로그인 탭. 실패 사유(성공 시 빈 문자열) 반환."""
    tree = fill_fast(acct, code, layout, tree)
    if tree is None:
        for name, value in (("id_field", acct["id"]), ("pw_field", acct["pw"]), ("captcha_field", code)):
            if name == "captcha_field" and not code:
                continue
            if not fill_field(name, value, layout):
                return name
        tree = inputs_ok(acct, code, layout)
        if tree is None:
            return "INPUT_MISMATCH"
    if not cloudflare_passed():
        return "CLOUDFLARE_TIMEOUT"
    tree = CF_LAST["tree"]   # 성공을 확인한 그 화면으로 바로 로그인 버튼을 찾는다(dump 1회 ≈ 3초 절약)
    if tree is None or not {"loginId", "password"}.issubset(dump_ids(tree)):
        return "LOGIN_FORM_UNVERIFIED"
    button = find_node_bounds(tree, {"text": "로그인", "cls": "Button"})
    if button is None:
        return "login_button"
    x1, y1, x2, y2 = button
    tap(((x1 + x2) // 2, (y1 + y2) // 2))
    return ""


def _cf_state(tree: ET.Element | None) -> str:
    """로그인 폼의 Cloudflare 상태 한 번 판정.
    SUCCESS = 위젯 안에 '성공!' / WAITING = 위젯은 있는데 성공 아님 / NO_WIDGET = 정상 폼, 위젯 없음
    / UNREADABLE = dump 실패·빈 WebView·로그인 폼 아님 (화면 인식 실패 — 성공으로 보지 않는다)."""
    if tree is None or _empty_webview(tree):
        return "UNREADABLE"
    ids = dump_ids(tree)
    if not {"loginId", "password"}.issubset(ids):
        return "UNREADABLE"
    if "cloudflare-captcha" not in ids:
        return "NO_WIDGET"
    if any(t["text"].strip() == "성공!" for t in dump_texts(tree)):
        return "SUCCESS"
    return "WAITING"


def _cf_sleep(until: float) -> None:
    """until(monotonic)까지 짧게 끊어 자면서 일시정지·중지 요청에 바로 반응한다."""
    while True:
        if CANCEL_CHECK:
            CANCEL_CHECK()   # 일시정지·중지면 예외로 빠져나간다
        left = until - time.monotonic()
        if left <= 0:
            return
        time.sleep(min(0.2, left))


def cloudflare_passed(max_wait: float | None = None, interval: float | None = None) -> bool:
    """Cloudflare 위젯(`cloudflare-captcha`) 인증 상태 확인. **체크 칸은 절대 누르지 않는다.**

    · 처음 즉시 확인 → 이후 interval(3초)마다 재확인, 최대 max_wait(60초).
    · '성공!' 이 보이면 남은 시간과 관계없이 바로 True → fill_form 이 로그인 버튼을 누른다.
    · 위젯 없는 정상 폼은 연속 2번(3초 간격) 확인돼야 True — 위젯이 늦게 뜨는 경우를 성공으로 착각하지 않게.
    · dump 실패·빈 화면(UNREADABLE)은 성공으로 보지 않는다.
    · 위젯이 성공 아닌 상태면 AUTH_HOOK("auth_required", …, deadline) 으로 남은 시간을 알린다(사용자가 폰에서 직접 체크).
    · max_wait 를 넘기면 AUTH_HOOK("auth_timeout") 후 False → 이 계정 로그인 실패(CLOUDFLARE_TIMEOUT).
    · 기다리는 동안 CANCEL_CHECK 로 일시정지·중지에 즉시 반응한다."""
    max_wait = CF_MAX_WAIT if max_wait is None else max_wait
    interval = CF_INTERVAL if interval is None else interval
    start = time.monotonic()
    deadline = start + max_wait
    notified = False
    no_widget_seen = 0
    CF_LAST["tree"] = None
    while True:
        tick = time.monotonic()
        tree = dump_tree(retries=1)
        state = _cf_state(tree)
        if state in ("SUCCESS", "NO_WIDGET"):
            CF_LAST["tree"] = tree
        if state == "SUCCESS":
            if notified:
                _notify("auth_done", "Cloudflare 인증 성공 확인 — 로그인 진행")
            else:
                print("[phone_login] Cloudflare 인증 성공 확인", flush=True)
            return True
        if state == "NO_WIDGET":
            no_widget_seen += 1
            if no_widget_seen >= 2:
                return True
        else:
            no_widget_seen = 0
        if state == "WAITING" and not notified:
            notified = True
            left = max(0.0, deadline - time.monotonic())
            _notify("auth_required", f"★ Cloudflare 인증 대기 — 폰에서 직접 체크해 주세요 (최대 {left:.0f}초)",
                    deadline=time.time() + left)
        if time.monotonic() >= deadline:
            _notify("auth_timeout", f"Cloudflare 인증을 {max_wait:.0f}초 안에 확인하지 못함 — 이 계정 로그인 중단")
            return False
        _cf_sleep(min(tick + interval, deadline))


def hide_keyboard() -> None:
    """키보드가 로그인 버튼을 덮지 않게 내린다. 키보드가 없을 때 BACK 을 누르면 로그인 WebView 가 닫히므로
    mInputShown=true 일 때만 누른다."""
    if "mInputShown=true" in shell("dumpsys", "input_method", capture_output=True):
        shell("input", "keyevent", "KEYCODE_BACK")
        time.sleep(0.5)


def close_popups(rounds: int = 10, clean_needed: int = 2) -> int:
    """앱 홈의 프로모션 팝업을 전부 닫는다(앱을 연 직후·로그인 직후 호출). 닫은 개수 반환.
    2026-11-05 실측: 하단 시트 '닫기' 뒤에 브랜드 오버레이 '닫기' 가 겹쳐 있고, 팝업은 늦게도 뜬다 → 연속 2번 없을 때까지 본다.
    dump 는 뒤쪽 숨은 WebView 글자도 주므로 실제 화면인 OCR 만 보고, 겹치면 맨 위 시트인 아래쪽부터 누른다."""
    closed = clean = 0
    for _ in range(rounds):
        hits = [t for t in ocr_texts(adb_screencap()) if t["text"].strip() == "닫기"]
        if not hits:
            clean += 1
            if clean >= clean_needed:
                break
            time.sleep(0.3)
            continue
        clean = 0
        top = max(hits, key=lambda t: t["cy"])
        tap((top["cx"], top["cy"])); closed += 1
        time.sleep(0.3)
    print(f"[phone_login] 팝업 {closed}개 닫음", flush=True)
    return closed


def open_app() -> None:
    """올리브영 앱을 앞으로 띄우고 팝업을 닫는다."""
    shell("monkey", "-p", "com.oliveyoung", "-c", "android.intent.category.LAUNCHER", "1", capture_output=True)
    time.sleep(2)
    close_popups()


def tap_my_tab() -> bool:
    """하단 '마이' 탭. 탭바는 dump 에 글자가 없어(tabBar 노드 하나뿐, 실측) OCR 로 찾는다."""
    texts = ocr_texts(adb_screencap())
    hit = next((t for t in texts if t["text"].strip() == "마이" and t["cy"] > adb.sy(2000)), None)
    if not hit:
        print("[phone_login] 하단 '마이' 탭 못 찾음", flush=True)
        return False
    tap((hit["cx"], hit["cy"]))
    return True


def is_logged_in() -> bool | None:
    """홈에서 마이 탭 → 로그인 폼이 뜨면 로그아웃 상태(실측). 판정 불가면 None."""
    if not tap_my_tab():
        return None
    global LAST_NICK
    end = time.time() + 8
    while time.time() < end:   # 마이페이지 닉네임 또는 로그인 폼이 뜨는 즉시 판정
        tree = dump_tree(retries=1)
        ids = dump_ids(tree)
        if "lblNickName" in ids:
            LAST_NICK = next(n.attrib.get("text", "") for n in tree.iter('node') if n.attrib.get("resource-id", "").endswith("lblNickName"))
            return True
        if "loginId" in ids:
            return False
    return None


def wait_result(timeout: float = RESULT_TIMEOUT) -> str:
    """로그인 탭 후 결과 화면 판정. LOGIN_FORM 이 계속이면 타임아웃 후 그대로 반환."""
    end, s = time.time() + timeout, "UNKNOWN"
    while time.time() < end:
        s = detect_screen()
        if s in ("HOME", "CAPTCHA_WRONG", "LOGIN_REJECTED", "ALERT"):
            break
    if s in ("CAPTCHA_WRONG", "LOGIN_REJECTED", "ALERT"):
        msg = [it["text"] for it in dump_texts(dump_tree()) if len(it["text"]) > 4]
        print(f"[phone_login] 오류 팝업: {msg[:3]}", flush=True)
    if s == "HOME":   # 폼이 닫혔다 ≠ 로그인 성공 → 마이 탭으로 확인
        close_popups(clean_needed=1)   # 로그인 직후 팝업이 탭을 가린다
        return "LOGGED_IN" if is_logged_in() else "NOT_LOGGED_IN"
    return s


def dismiss_error(layout: dict) -> None:
    _, tree, texts = read_screen()
    xy = locate("error_ok", layout, tree, texts)
    if xy:
        tap(xy); time.sleep(0.8)


SAVE_FAIL_SHOTS = os.environ.get("SAVE_FAIL_SHOTS", "1") != "0"
MAX_FAIL_SHOTS = 50


def shot_fail(no: str, tag: str) -> None:
    """실패 화면 저장(원인 분석용). 보안 인증 화면(카드앱)은 저장하지 않고, 최근 MAX_FAIL_SHOTS 장만 남긴다."""
    if not SAVE_FAIL_SHOTS:
        return
    try:
        if sr.is_secure():
            return
        shots = LOG_DIR / "fail_shots"
        shots.mkdir(parents=True, exist_ok=True)
        adb_screencap().save(shots / f"fail_{no}_{tag}_{dt.datetime.now():%m%d_%H%M%S}.png")
        old = sorted(shots.glob("fail_*.png"), key=lambda p: p.stat().st_mtime)
        for p in old[:-MAX_FAIL_SHOTS]:
            p.unlink(missing_ok=True)
    except Exception as e:
        print(f"[phone_login] 실패 화면 저장 못 함: {type(e).__name__}", flush=True)


def attempt(no: str, acct: dict, layout: dict) -> str:
    """1회 시도. 반환: LOGGED_IN / CAPTCHA_UNSUPPORTED(제출 안 함) / MISSING:<요소> / 실패 화면명."""
    code, tree = solve_captcha(layout)
    if code is None:
        return "CAPTCHA_UNSUPPORTED"
    t0 = time.time()
    missing = fill_form(acct, code, layout, tree)
    print(f"[phone_login] 입력·제출 {time.time() - t0:.1f}s", flush=True)
    if missing == "CLOUDFLARE_TIMEOUT":
        shot_fail(no, "cloudflare")
        return missing
    if missing:
        return f"MISSING:{missing}"
    t0 = time.time()
    result = wait_result()
    print(f"[phone_login] 결과 확인 {time.time() - t0:.1f}s", flush=True)
    if result != "LOGGED_IN":
        shot_fail(no, result)
        dismiss_error(layout)
    return result


def login_form_after_logout() -> str:
    """로그아웃 직후 마이 탭 → 로그인 폼. 폼에선 같은 자리가 '회원가입'이라(실측 오탭) 폼이 열렸으면 절대 다시 누르지 않고,
    다시 누를 땐 OCR 로 '마이' 글자가 보일 때만 누른다."""
    if FORM_OPEN:
        return "LOGIN_FORM"
    tap(adb.pt(MY_TAB))
    for i in range(5):
        if "loginId" in dump_ids(dump_tree(retries=2)):
            return "LOGIN_FORM"
        if i >= 1:
            tap_my_tab()
    return "UNKNOWN"


def open_login_form() -> str:
    """로그인 폼을 띄운다. 반환: LOGIN_FORM / ALREADY_LOGGED_IN / 그 밖의 화면명."""
    # 앱이 이미 앞에 있으면(로그아웃 직후 등) 다시 열지 않고 바로 마이 탭으로 판정한다
    if "com.oliveyoung" in adb.focus():
        logged = is_logged_in()
        if logged is not None:
            return "ALREADY_LOGGED_IN" if logged else "LOGIN_FORM"
    open_app()
    s = detect_screen()
    if s == "HOME":
        logged = is_logged_in()   # 로그아웃 상태면 이 탭으로 로그인 폼이 뜬다(실측)
        if logged is None:
            return "UNKNOWN"
        s = "ALREADY_LOGGED_IN" if logged else "LOGIN_FORM"
    return s


def login(no: str, after_logout: bool = False) -> dict:
    """no번 계정 앱 로그인. 홈이면 마이 탭으로 로그인 폼을 연다. 다른 계정 로그인 상태면 멈춘다(먼저 logout)."""
    global LAST_NICK
    acct, layout = load_account(no), load_layout()
    LAST_NICK = ""   # 이전 계정 닉네임으로 일치 판정하지 않게
    start = login_form_after_logout() if after_logout else open_login_form()
    if start != "LOGIN_FORM":
        return {"ok": False, "status": f"NOT_LOGIN_SCREEN:{start}"}
    consec = 0
    for i in range(1, MAX_ATTEMPTS + 1):
        r = attempt(no, acct, layout)
        print(f"[phone_login] {no}번 시도 {i}/{MAX_ATTEMPTS} → {r}", flush=True)
        if r == "LOGGED_IN":
            ok, why = account_matches(acct)
            if not ok:
                return {"ok": False, "status": f"ACCOUNT_MISMATCH:{why}", "attempts": i}
            return {"ok": True, "status": "LOGGED_IN", "attempts": i}
        if r.startswith("MISSING:") or r in ("CAPTCHA_UNSUPPORTED", "CLOUDFLARE_TIMEOUT"):
            return {"ok": False, "status": r, "attempts": i}   # 재시도해도 같다
        consec += 1
        if consec >= MAX_CONSEC_FAIL:
            return {"ok": False, "status": f"STOP_CONSEC_FAIL:{r}", "attempts": i}
    return {"ok": False, "status": "MAX_ATTEMPTS", "attempts": MAX_ATTEMPTS}


def account_matches(acct: dict) -> tuple[bool, str]:
    """마이페이지 닉네임(앞부분 + 마스킹 '*')이 계정 아이디와 맞는지. 닉네임을 못 읽었으면 불일치로 본다."""
    nick = LAST_NICK.strip()
    if not nick:
        return False, "닉네임 확인 불가"
    head = nick.split("*")[0]
    if head and not acct["id"].startswith(head):
        return False, "닉네임이 계정 아이디와 다름"
    return True, ""


def wait_my_tab(timeout: float = 12) -> bool:
    """앱 스플래시가 끝나 하단 '마이' 글자가 보일 때까지 쉬지 않고 확인한다. 팝업이 가리면 닫는다."""
    end = time.time() + timeout
    while time.time() < end:
        texts = ocr_texts(adb_screencap())
        if any(t["text"].strip() == "마이" and t["cy"] > adb.sy(2000) for t in texts):
            return True
        close = [t for t in texts if t["text"].strip() == "닫기"]
        if close:
            top = max(close, key=lambda t: t["cy"])
            tap((top["cx"], top["cy"]))
    return False


def open_my_page(tries: int = 3) -> str:
    """마이페이지가 실제로 뜬 것을 확인한다. 반환: MY / LOGGED_OUT / FAIL. 안 떴으면 스크롤하지 않고 다시 누른다."""
    focus = adb.focus()
    wait = 2.5                # 앱이 이미 떠 있으면 탭이 바로 보인다
    if "com.oliveyoung" not in focus:
        shell("monkey", "-p", "com.oliveyoung", "-c", "android.intent.category.LAUNCHER", "1", capture_output=True)
        time.sleep(2)
        wait = 12             # 스플래시 대기
    for _ in range(tries + 2):
        ids = dump_ids(dump_tree(retries=2))
        if "lblNickName" in ids:
            return "MY"
        if "loginId" in ids:
            return "LOGGED_OUT"
        if wait_my_tab(wait):
            tap_my_tab()
        else:
            shell("input", "keyevent", "KEYCODE_BACK")   # 장바구니·주문서엔 하단 탭이 없다 (실측)
        wait = 2.5
    return "FAIL"


def logout() -> bool:
    """마이페이지 맨 아래 '로그아웃' → 알림 '확인' (2026-11-05 실측). 하단 탭바(y≥2001)에 가리지 않을 때만 누른다."""
    global FORM_OPEN
    state = open_my_page()
    if state != "MY":
        FORM_OPEN = state == "LOGGED_OUT"
        return FORM_OPEN
    for _ in range(6):
        for _ in range(3):
            shell("input", "swipe", *map(str, (*adb.pt((540, 1900)), *adb.pt((540, 300)))), "120")
        tree = dump_tree()
        node = next((n for n in (tree.iter('node') if tree is not None else [])
                     if n.attrib.get("text") == "로그아웃" and _visible(n) and _bounds(n)[3] < adb.sy(2001)), None)
        if node is not None:
            x1, y1, x2, y2 = _bounds(node)
            tap(((x1 + x2) // 2, (y1 + y2) // 2))
            for _ in range(5):
                ok = find_node_bounds(dump_tree(), {"rid": "button1"})
                if ok:
                    tap(((ok[0] + ok[2]) // 2, (ok[1] + ok[3]) // 2))
                    return True
            return False
    return False


def save_dump() -> None:
    """실측용: 현재 화면 스크린샷 + dump xml 을 logs/ 에 저장. 비번 노드 text 는 가린다(dump 는 평문을 준다)."""
    LOG_DIR.mkdir(exist_ok=True)
    stamp = f"{dt.datetime.now():%m%d_%H%M%S}"
    adb_screencap().save(LOG_DIR / f"dump_{stamp}.png")
    tree = dump_tree()
    if tree is not None:
        for n in tree.iter('node'):
            if n.attrib.get('password') == 'true':
                n.set('text', '***')
        ET.ElementTree(tree).write(LOG_DIR / f"dump_{stamp}.xml", encoding="utf-8")
    print(f"[phone_login] logs/dump_{stamp}.png / .xml 저장")


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "login" and len(sys.argv) == 3:
        r = login(sys.argv[2])
        print(f"[phone_login] 결과: {r}")
        return 0 if r["ok"] else 1
    if cmd == "logout":
        ok = logout()
        print(f"[phone_login] 로그아웃: {ok}")
        return 0 if ok else 1
    if cmd == "detect":
        print(detect_screen()); return 0
    if cmd == "open":
        open_app(); print(detect_screen()); return 0
    if cmd == "dump":
        save_dump(); return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
