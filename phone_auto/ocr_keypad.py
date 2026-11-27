"""카드앱 셔플 키패드 PIN 입력 — UIAutomator dump 의 content-desc 로만 숫자 위치를 읽는다.

보안 인증 화면이라 일반 OCR(화면 글자 인식)로 조작하지 않는다. 예전의 'Claude OCR 핸드셰이크'(스크린샷을 에이전트가
판독) 경로는 없앴다 — dump 에 키패드가 안 잡히는 화면이면 탭하지 않고 멈춘다(PIN_MODE=manual 로 사용자가 직접 입력).

값은 card_secrets.json(키: lotte, hyundai)에서 코드가 직접 읽는다. 숫자·좌표는 로그·파일에 남기지 않는다.
"""
import json
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import adb_manager as adb

SECRETS_PATH = Path(__file__).resolve().parent / "card_secrets.json"
DEVICE_DUMP_PATH = "/sdcard/_kp.xml"

ALLOWED_CARDS = ("lotte", "hyundai")   # 삼성 등 다른 카드는 쓰지 않는다 (ORDER_STEPMAP 카드 규칙)
DIGITS = "0123456789"
TAP_DELAY_SEC = 0.5
BOUNDS_RE = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")


def load_secrets():
    # 예외 메시지·체인에 파일 내용이 섞이지 않도록 from None
    try:
        return json.loads(SECRETS_PATH.read_text(encoding="utf-8"))
    except OSError as e:
        raise RuntimeError(f"card_secrets.json 읽기 실패: {e.strerror}") from None
    except ValueError:
        raise RuntimeError("card_secrets.json 형식 오류") from None


def has_pin(card_type, pin_type="pin6") -> bool:
    try:
        return bool(str(load_secrets().get(card_type, {}).get(pin_type, "")))
    except RuntimeError:
        return False


def run_adb(*args):
    # 좌표 = 숫자 위치이므로 명령 전체가 담긴 예외를 체인에 남기지 않는다
    try:
        return adb.run(*args)
    except adb.AdbError as e:
        raise RuntimeError(f"adb {args[0]} 실패: PIN 입력을 중단합니다. ({type(e).__name__})") from None


def add_key(keypad, digit, xy):
    # 같은 숫자가 다른 위치에 또 잡히면(뒤쪽 WebView 등) 어느 쪽인지 알 수 없다
    if keypad.get(digit, xy) != xy:
        raise RuntimeError("키패드 판독 결과가 모호합니다. 탭하지 않고 중단합니다.")
    keypad[digit] = xy


def dump_ui_xml():
    # PC 에 파일을 남기지 않고, 폰의 dump 도 바로 지운다 (키패드 배열이 들어 있다)
    try:
        run_adb("shell", "uiautomator", "dump", DEVICE_DUMP_PATH)
        return run_adb("exec-out", "cat", DEVICE_DUMP_PATH)
    finally:
        run_adb("shell", "rm", "-f", DEVICE_DUMP_PATH)


def parse_keypad(xml_bytes):
    """content-desc 가 'N' 또는 'N 버튼'(현대카드 앱)인 보이는 노드 → {숫자: 중심좌표}."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        raise RuntimeError("UI dump 를 읽을 수 없습니다. 탭하지 않고 중단합니다.") from None
    keypad = {}
    for node in root.iter("node"):
        desc = node.get("content-desc", "").replace(" 버튼", "").strip()
        m = BOUNDS_RE.match(node.get("bounds", ""))
        if len(desc) != 1 or desc not in DIGITS or not m:
            continue
        x1, y1, x2, y2 = map(int, m.groups())
        if x2 > x1 and y2 > y1:   # [0,0][0,0] = 뒤쪽 숨은 노드
            add_key(keypad, desc, ((x1 + x2) // 2, (y1 + y2) // 2))
    return keypad


def read_keypad_from_dump():
    return parse_keypad(dump_ui_xml())


def validate_keymap(keypad, pin, screen=None):
    # 일부 숫자만 탭되면 틀린 PIN 이 입력되므로, 탭하기 전에 전부 확인한다. 메시지에 숫자·좌표를 넣지 않는다
    if set(keypad) != set(DIGITS) or not set(pin) <= set(DIGITS):
        raise ValueError(f"키패드 숫자 {len(keypad)}/10 판독. 탭하지 않고 중단합니다.")
    if len(set(keypad.values())) != len(keypad):
        raise ValueError("키패드 좌표가 중복됩니다. 탭하지 않고 중단합니다.")
    w, h = screen or (adb.BASE_W, adb.BASE_H)
    if not all(0 <= x < w and 0 <= y < h for x, y in keypad.values()):
        raise ValueError("키패드 좌표가 화면 밖입니다. 탭하지 않고 중단합니다.")


def input_pin(card_type, pin_type="pin6"):
    """현재 화면의 키패드를 1회 판독해 연속 탭한다. 실패해도 재시도하지 않는다(틀린 PIN 누적 = 카드 잠김)."""
    if card_type not in ALLOWED_CARDS:
        raise ValueError(f"{card_type} 카드는 사용하지 않습니다. (lotte, hyundai 만 가능)")
    secrets = load_secrets()
    if card_type not in secrets:
        raise ValueError(f"{card_type} 카드 정보가 없습니다.")
    pin = str(secrets[card_type].get(pin_type, ""))
    if not pin:
        raise ValueError(f"{card_type}의 {pin_type} 값이 비어 있습니다.")

    keypad = read_keypad_from_dump()
    if len(keypad) < 10:   # 키패드 로딩 중 — 탭 전이라 다시 읽어도 안전
        keypad = read_keypad_from_dump()
    try:
        screen = adb.screen_size()
    except adb.AdbError:
        screen = None
    validate_keymap(keypad, pin, screen)
    for i, digit in enumerate(pin, 1):
        x, y = keypad[digit]
        print(f"탭: * ({i}/{len(pin)})", flush=True)
        run_adb("shell", "input", "tap", str(x), str(y))
        time.sleep(TAP_DELAY_SEC)
    print(f"{card_type} 카드의 {pin_type} 입력 완료", flush=True)
