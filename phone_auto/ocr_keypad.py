import json
import re
import time
import subprocess
import os
import xml.etree.ElementTree as ET

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SECRETS_PATH = os.path.join(BASE_DIR, "card_secrets.json")
HANDSHAKE_DIR = os.path.join(BASE_DIR, "_tmp", "claude_ocr")
REQUEST_PNG = os.path.join(HANDSHAKE_DIR, "request.png")
REQUEST_JSON = os.path.join(HANDSHAKE_DIR, "request.json")
RESPONSE_JSON = os.path.join(HANDSHAKE_DIR, "response.json")
DEVICE_DUMP_PATH = "/sdcard/_kp.xml"

ALLOWED_CARDS = ("lotte", "hyundai")   # 삼성 등 다른 카드는 쓰지 않는다 (ORDER_STEPMAP 카드 규칙)
DIGITS = "0123456789"
SCREEN_W, SCREEN_H = 1080, 2400        # ORDER_STEPMAP 실측 해상도
TAP_DELAY_SEC = 0.5
HANDSHAKE_TIMEOUT_SEC = 240
HANDSHAKE_POLL_SEC = 1.0
BOUNDS_RE = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")

def load_secrets():
    # 예외 메시지·체인에 파일 내용이 섞이지 않도록 from None
    try:
        with open(SECRETS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except OSError as e:
        raise RuntimeError(f"card_secrets.json 읽기 실패: {e.strerror}") from None
    except ValueError:
        raise RuntimeError("card_secrets.json 형식 오류") from None

def run_adb(*args, capture=False):
    # 좌표 = 숫자 위치이므로 CalledProcessError(명령 전체 포함)를 체인에 남기지 않는다
    try:
        return subprocess.run(["adb", *args], check=True, capture_output=capture).stdout
    except FileNotFoundError:
        raise RuntimeError("adb 를 찾을 수 없습니다. PIN 입력을 중단합니다.") from None
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"adb {args[0]} 실패(종료코드 {e.returncode}). PIN 입력을 중단합니다.") from None

def add_key(keypad, digit, xy):
    # 같은 숫자가 다른 위치에 또 잡히면(뒤쪽 WebView 등) 어느 쪽인지 알 수 없다
    if keypad.get(digit, xy) != xy:
        raise RuntimeError("키패드 판독 결과가 모호합니다. 탭하지 않고 중단합니다.")
    keypad[digit] = xy

def dump_ui_xml():
    # PC 에 파일을 남기지 않고, 폰의 dump 도 바로 지운다 (키패드 배열이 들어 있다)
    try:
        run_adb("shell", "uiautomator", "dump", DEVICE_DUMP_PATH, capture=True)
        return run_adb("exec-out", "cat", DEVICE_DUMP_PATH, capture=True)
    finally:
        run_adb("shell", "rm", "-f", DEVICE_DUMP_PATH, capture=True)

def read_keypad_from_dump():
    """카드앱 셔플 키패드 — content-desc 로 숫자 위치를 읽는다 (현대카드 앱은 desc='N 버튼')."""
    try:
        root = ET.fromstring(dump_ui_xml())
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

def clear_handshake_files():
    for path in (REQUEST_PNG, REQUEST_JSON, RESPONSE_JSON):
        if os.path.exists(path):
            os.remove(path)

def parse_handshake_response(request_id):
    """response.json 이 아직 없거나 쓰는 중이면 None."""
    try:
        with open(RESPONSE_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)
        if str(data.get("id")) != request_id:
            return None
        keypad = {}
        for item in data["digits"]:
            digit = str(item["d"]).strip()
            if len(digit) == 1 and digit in DIGITS:
                add_key(keypad, digit, (int(item["x"]), int(item["y"])))
        return keypad
    except (OSError, ValueError, KeyError, TypeError):
        return None

def read_keypad_from_handshake():
    """화면 보안이 없고 dump 에 안 잡히는 키패드 — 에이전트가 스크린샷을 판독해 response.json 을 쓴다.
    {"id": <요청 id>, "digits": [{"d": "0", "x": 367, "y": 864}, ...]}"""
    os.makedirs(HANDSHAKE_DIR, exist_ok=True)
    clear_handshake_files()
    try:
        with open(REQUEST_PNG, "wb") as f:
            f.write(run_adb("exec-out", "screencap", "-p", capture=True))
        request_id = str(int(time.time() * 1000))
        with open(REQUEST_JSON, "w", encoding="utf-8") as f:
            json.dump({"id": request_id, "image": REQUEST_PNG}, f)
        print(f"[claude-ocr] 판독요청 id={request_id} img={REQUEST_PNG} → {RESPONSE_JSON} 대기", flush=True)
        deadline = time.time() + HANDSHAKE_TIMEOUT_SEC
        while time.time() < deadline:
            keypad = parse_handshake_response(request_id)
            if keypad is not None:
                return keypad
            time.sleep(HANDSHAKE_POLL_SEC)
        raise RuntimeError("판독 응답이 없습니다. 탭하지 않고 중단합니다.")
    finally:
        clear_handshake_files()   # 응답에 키패드 배열이 들어 있다

def validate_keymap(keypad, pin):
    # 일부 숫자만 탭되면 틀린 PIN 이 입력되므로, 탭하기 전에 전부 확인한다. 메시지에 숫자·좌표를 넣지 않는다
    if set(keypad) != set(DIGITS) or not set(pin) <= set(DIGITS):
        raise ValueError(f"키패드 숫자 {len(keypad)}/10 판독. 탭하지 않고 중단합니다.")
    if len(set(keypad.values())) != len(keypad):
        raise ValueError("키패드 좌표가 중복됩니다. 탭하지 않고 중단합니다.")
    if not all(0 <= x < SCREEN_W and 0 <= y < SCREEN_H for x, y in keypad.values()):
        raise ValueError("키패드 좌표가 화면 밖입니다. 탭하지 않고 중단합니다.")

KEYPAD_READERS = {"dump": read_keypad_from_dump, "handshake": read_keypad_from_handshake}

def input_pin(card_type, pin_type="pin6", source="dump"):
    """현재 화면의 키패드를 1회 판독해 연속 탭한다. 실패해도 재시도하지 않는다(틀린 PIN 누적 = 카드 잠김)."""
    if card_type not in ALLOWED_CARDS:
        raise ValueError(f"{card_type} 카드는 사용하지 않습니다. (lotte, hyundai 만 가능)")
    if source not in KEYPAD_READERS:
        raise ValueError(f"알 수 없는 판독 방식: {source}")
    secrets = load_secrets()
    if card_type not in secrets:
        raise ValueError(f"{card_type} 카드 정보가 없습니다.")
    pin = str(secrets[card_type].get(pin_type, ""))
    if not pin:
        raise ValueError(f"{card_type}의 {pin_type} 값이 비어 있습니다.")

    keypad = KEYPAD_READERS[source]()
    if source == "dump" and len(keypad) < 10:   # 키패드 로딩 중 — 탭 전이라 다시 읽어도 안전
        keypad = KEYPAD_READERS[source]()
    validate_keymap(keypad, pin)
    for i, digit in enumerate(pin, 1):
        x, y = keypad[digit]
        print(f"탭: * ({i}/{len(pin)})")
        run_adb("shell", "input", "tap", str(x), str(y))
        time.sleep(TAP_DELAY_SEC)
    print(f"{card_type} 카드의 {pin_type} 입력 완료")

if __name__ == "__main__":
    print("키패드 입력 테스트를 시작합니다.")
