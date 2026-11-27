"""올리브영 폰 결제 — 장바구니 → 주문서 → CJ ONE → 카드 선택 → 모두 동의 → 결제하기 → 카드앱 → 주문완료.

2026-11-05 1번 계정 롯데카드 라이브 완주 경로를 코드로 옮긴 것(주문 Y2611059661356).
현대카드 앱 구간은 3mall pay_lotte_hyundai(2026-06-12 라이브 검증)를 옮긴 것 — 올영에선 미실측.
고정 대기 없이 화면이 바뀌는 즉시 다음 단계로 넘어간다. 예상과 다르면 탭하지 않고 PayStop 으로 멈춘다.
비번은 ocr_keypad.input_pin 이 card_secrets.json 에서 직접 읽어 탭한다(PIN_MODE="auto").
PIN_MODE="manual" 이면 키패드에서 멈추고 사용자가 폰에서 직접 인증하기를 기다렸다가 자동으로 이어간다.

작업 엔진 연동 (web 대시보드):
  ORDER_GUARD(info) — 결제하기 누르기 직전 주문서 검증(상품·옵션·배송지·금액·한도). 문제가 있으면 PayStop.
  PAY_HOOK(event, data) — "payment_pending"(결제하기 누르기 직전, DB 에 먼저 기록) / "auth_required" / "auth_done"
                          / "order_complete". 결제하기를 누른 뒤의 예외는 결과 불명(PAYMENT_RESULT_UNKNOWN)으로 다룬다.

사용:  python3 phone_auto/phone_pay.py prepare <계정번호> <lotte|hyundai>   # 결제하기 직전까지
       python3 phone_auto/phone_pay.py pay <계정번호> <lotte|hyundai>             # 주문완료까지 (실제 결제)
       python3 phone_auto/phone_pay.py relogin-pay <계정번호> <lotte|hyundai>     # 로그아웃 → 로그인 → 주문완료
       python3 phone_auto/phone_pay.py relogin-to-pin <계정번호> <lotte|hyundai>  # 로그아웃 → 로그인 → PIN 화면에서 멈춤 (입력 안 함)
"""
from __future__ import annotations

import os
import re
import sys
import time

import adb_manager as adb
import ocr_keypad
import phone_login as pl

CARD_NAMES = {"lotte": "롯데카드", "hyundai": "현대카드"}
BASKET_ICON = (1006, 142)   # 마이페이지·홈 우측 상단 장바구니 (실측)
SAFE_TOP, SAFE_BOTTOM = 220, 2200   # 헤더·하단바에 가리지 않는 y 범위 (실측)
HYUNDAI_PKG = "com.hyundaicard.appcard"
HYUNDAI_CARD = "The CJ-M Edition2"   # 올영은 현대카드 앱에서 이 카드로 결제 (사용자 지정, ORDER_STEPMAP 8)
POLL_SEC = 0.15
START = time.time()
PIN_MODE = os.environ.get("PIN_MODE", "auto")     # auto = card_secrets.json 으로 코드가 입력 / manual = 사용자가 직접
MANUAL_AUTH_WAIT = 240                             # manual 인증 대기(초)
ORDER_GUARD = None    # callable(info: dict) -> None (문제 있으면 PayStop)
PAY_HOOK = None       # callable(event: str, data: dict) -> None


class PayStop(Exception):
    pass


class PaymentSubmitted(PayStop):
    """결제하기를 누른 뒤의 중단 — 결제됐는지 모른다. 주문내역 확인 전 절대 다시 결제하지 않는다."""


def hook(event: str, **data) -> None:
    if PAY_HOOK:
        PAY_HOOK(event, data)


def log(msg: str) -> None:
    print(f"[pay {time.time() - START:5.1f}s] {msg}", flush=True)


def swipe_down(times: int = 1) -> None:
    """dump 1회가 약 3초라, 위치를 아는 구간은 dump 없이 먼저 내린다."""
    for _ in range(times):
        swipe((540, 1800), (540, 500), 500)   # 천천히: 관성으로 지나치지 않게


def swipe(a: tuple[int, int], b: tuple[int, int], ms: int) -> None:
    """실측(1080x2400) 좌표로 스와이프 — 기기 해상도로 환산한다."""
    pl.shell("input", "swipe", *map(str, (*adb.pt(a), *adb.pt(b))), str(ms))


def nodes(tree) -> list:
    """[0,0][0,0] 숨은 노드를 뺀 노드. 화면 밖 노드(높이 0)는 값 읽기용으로 남긴다."""
    if tree is None:
        return []
    return [n for n in tree.iter("node") if pl._bounds(n) and pl._bounds(n) != (0, 0, 0, 0)]


def rid_of(node) -> str:
    return node.get("resource-id", "").split("/")[-1]


def find(tree, rid=None, text=None, contains=False):
    for n in nodes(tree):
        value = n.get("text", "")
        if (rid is None or rid_of(n) == rid) and (text is None or (text in value if contains else text == value)):
            return n
    return None


def text_of(tree, rid: str) -> str:
    n = find(tree, rid=rid)
    return n.get("text", "") if n is not None else ""


def number(s: str) -> int:
    return int(re.sub(r"\D", "", s) or 0)


def center(node) -> tuple[int, int]:
    x1, y1, x2, y2 = pl._bounds(node)
    return (x1 + x2) // 2, (y1 + y2) // 2


def tappable(node) -> bool:
    x1, y1, x2, y2 = pl._bounds(node)
    return y2 - y1 >= adb.sy(40) and y1 >= adb.sy(SAFE_TOP) and y2 <= adb.sy(SAFE_BOTTOM)


def wait_tree(check, timeout: float, what: str):
    """check(tree) 가 참이 될 때까지 dump 를 반복한다. 쉬지 않고 바로 다시 읽는다."""
    end = time.time() + timeout
    while time.time() < end:
        tree = pl.dump_tree(retries=1)
        if tree is not None and check(tree):
            return tree
    raise PayStop(f"{what} 대기 시간 초과")


def focus() -> str:
    """현재 포커스 창. dumpsys 출력은 파이썬에서 거른다(OS 전용 명령 없이 Windows 에서도 동작)."""
    return adb.focus()


def wait_focus(name: str, timeout: float) -> None:
    end = time.time() + timeout
    while time.time() < end:
        if name in focus():
            return
        time.sleep(POLL_SEC)
    raise PayStop(f"{name} 화면 미도달")


def scroll_to(rid: str, tries: int = 10):
    """주문서는 WebView 라 화면 밖 요소는 높이 0 으로 잡힌다 → 탭 가능할 때까지 스크롤."""
    for _ in range(tries):
        tree = pl.dump_tree(retries=2)
        n = find(tree, rid=rid)
        if n is not None and tappable(n):
            return n, tree
        y = pl._bounds(n)[1] if n is not None else adb.sy(SAFE_BOTTOM)
        delta = 1000 if y >= adb.sy(SAFE_BOTTOM) else -500
        swipe((540, 1500), (540, 1500 - delta), 200)
    raise PayStop(f"{rid} 를 화면에 띄우지 못함")


def tap_until_cart(icon: tuple[int, int], tries: int = 4):
    """푸시 알림이 화면 위쪽을 덮으면 탭이 씹힌다(실측) → 장바구니가 뜰 때까지 다시 누른다."""
    for _ in range(tries):
        pl.tap(icon)
        tree = pl.dump_tree(retries=2)
        if find(tree, text="일반배송 구매하기", contains=True) is not None:
            return tree
    raise PayStop("장바구니 대기 시간 초과")


def check_account(no: str) -> None:
    nick = pl.LAST_NICK.split("*")[0]
    if nick and not pl.load_account(no)["id"].startswith(nick):
        raise PayStop(f"로그인 계정이 {no}번이 아님")


def open_order_sheet(no: str):
    """우측 상단 장바구니 → 일반배송 구매하기 → 주문서. 로그인 계정과 기본배송지를 확인한다."""
    check_account(no)
    tree = tap_until_cart(adb.pt(BASKET_ICON))
    button = find(tree, text="일반배송 구매하기", contains=True)
    log(button.get("text"))
    for _ in range(2):
        pl.tap(center(button))
        try:
            tree = wait_tree(lambda t: bool(text_of(t, "addrSummaryName")), 7, "주문서")
            break
        except PayStop:
            if any("달라졌어요" in t["text"] for t in pl.ocr_texts(pl.adb_screencap())):
                pl.tap(adb.pt((540, 2150)))   # 첫 진입 안내 시트 '확인' — dump 에 없음 (실측)
    else:
        raise PayStop("주문서 미도달")
    if text_of(tree, "baseDlvpYnFlag") != "기본배송지":   # 배송지 이름의 숫자는 계정 번호가 아니다 (2번 = '1(…)', 실측)
        raise PayStop("기본배송지가 아님 — 변경 경로 미실측")
    log(f"배송지: {text_of(tree, 'addrSummaryName')}")
    return tree


def tap_cjone(tree) -> None:
    """CJ ONE 전액사용만 누르고 결과는 보지 않는다 (사용자 지시 2026-11-05). 뜨는 알림은 payment_area 가 닫는다."""
    btn = find(tree, rid="cjonePnt_btn")
    if btn is None or not tappable(btn):
        btn, _ = scroll_to("cjonePnt_btn")
    pl.tap(center(btn))
    log("CJ ONE 전액사용 누름")


def scroll_up(px: int) -> None:
    swipe((540, 1000), (540, 1000 + px), 500)   # 천천히: 관성 없이 그만큼만


def payment_area():
    """아래로 2번 → 200px 올림 (사용자 지시 2026-11-05). 카드 칸이 안 보일 때만 그 칸이 보이게 더 올린다.
    글자가 있어도 OCR 이 못 읽을 때가 있어(실측) 위치는 dump 로 읽는다."""
    for _ in range(2):
        swipe((540, 1800), (540, 400), 150)
    scroll_up(200)
    for _ in range(4):
        tree = pl.dump_tree(retries=2)
        alert = find(tree, rid="button1")
        if alert is not None:
            pl.tap(center(alert))   # CJ ONE '10P 단위' 알림
            continue
        card, check = find(tree, rid="acqrCd"), find(tree, rid="auto-all-check")
        if card is None or check is None:
            raise PayStop("결제 영역을 찾지 못함")
        if tappable(card) and tappable(check) and tappable(find(tree, rid="btnPay")):
            return tree
        scroll_up(max(pl._bounds(check)[1] and 1320 - round(pl._bounds(check)[1] * adb.BASE_H / adb.sy(adb.BASE_H)), 200))   # 동의 칸을 y≈1320 으로 (실측 배치)
    raise PayStop("결제 영역을 화면에 띄우지 못함")


def pick_card_row(name: str) -> None:
    """네이티브 카드 선택창에서 name 행을 **이름으로** 찾아 누른다. 2026-11-27 실측: 창이 지금 선택된 카드로
    자동 스크롤되고 항목도 바뀌어(`카드를 선택해주세요.`·`KB카드` 추가) 고정 좌표(CARD_ROWS)가 롯데를 눌렀다.
    못 보이면 목록 맨 위로 올렸다가 아래로 내리며 찾는다."""
    swipes = [("600", "1800")] * 2 + [("1800", "600")] * 4
    for i in range(len(swipes) + 1):
        tree = wait_tree(lambda t: find(t, rid="text1") is not None, 5, "카드 선택창")
        row = next((n for n in nodes(tree) if rid_of(n) == "text1" and n.get("text") == name
                    and pl._bounds(n)[3] - pl._bounds(n)[1] > adb.sy(100)), None)
        if row is not None:
            pl.tap(center(row))
            log(f"카드 선택창: {name}")
            return
        if i < len(swipes):
            swipe((540, int(swipes[i][0])), (540, int(swipes[i][1])), 300)
    raise PayStop(f"카드 선택창에서 '{name}' 을 못 찾음")


def select_card(card: str, tree):
    """선택창이 닫히며 화면이 움직이므로, 바꿨으면 다시 읽어 지금 위치를 돌려준다."""
    name = CARD_NAMES[card]
    node = find(tree, rid="acqrCd")
    if node.get("text") != name:
        pl.tap(center(node))
        pick_card_row(name)
        tree = wait_tree(lambda t: text_of(t, "acqrCd") == name, 5, "카드 선택 반영")
    if text_of(tree, "acqrCd") != name or text_of(tree, "instMmCnt") != "일시불":
        raise PayStop("카드 또는 일시불 확인 실패")
    log(f"{name} 일시불")
    return tree


def checkbox_is_dark(box: tuple[int, int, int, int]) -> bool:
    """dump 의 checked 는 안 바뀐다(실측) → 픽셀로 판정. 미체크 254 / 체크 43."""
    x1, y1, x2, y2 = box
    im = pl.adb_screencap().convert("L").crop((x1 + 10, y1 + 10, x2 - 10, y2 - 10))
    return sum(im.tobytes()) / len(im.tobytes()) < 150


def agree_all(tree) -> None:
    box = pl._bounds(find(tree, rid="auto-all-check"))
    if not checkbox_is_dark(box):
        pl.tap((adb.sx(270), (box[1] + box[3]) // 2))   # 체크박스·설명문이 아닌 '모두 동의합니다' 글자만 반응 (실측)
        end = time.time() + 2
        while not checkbox_is_dark(box):
            if time.time() > end:
                raise PayStop("모두 동의 체크 실패")
    log("모두 동의 체크")


def order_info(tree, card: str, amount: int) -> dict:
    """ORDER_GUARD 에 넘길 주문서 정보. 화면 밖(높이 0) WebView 노드 글자도 포함한다."""
    return {"amount": amount, "card": CARD_NAMES[card], "card_shown": text_of(tree, "acqrCd"),
            "installment": text_of(tree, "instMmCnt"), "address_name": text_of(tree, "addrSummaryName"),
            "address_default": text_of(tree, "baseDlvpYnFlag") == "기본배송지",
            "texts": [t for t in (n.get("text", "") for n in nodes(tree)) if t]}


def prepare_payment(card: str) -> tuple:
    """주문서(CJ ONE 누른 뒤) → 카드·일시불 → 모두 동의 → 금액 읽기 → 주문 검증. (금액, 결제하기 좌표)."""
    if card not in CARD_NAMES:
        raise PayStop(f"사용하지 않는 카드: {card} (삼성카드 금지 — 현대·롯데만)")
    if PIN_MODE == "auto" and not ocr_keypad.has_pin(card):
        raise PayStop(f"{CARD_NAMES[card]} PIN 이 card_secrets.json 에 없음 — PIN_MODE=manual 로 직접 인증하거나 추가")
    tree = select_card(card, payment_area())
    agree_all(tree)
    button = find(tree, rid="btnPay")
    amount = number(button.get("text", ""))
    if amount <= 0:
        raise PayStop("결제 금액을 읽지 못함")
    if ORDER_GUARD:
        ORDER_GUARD(order_info(tree, card, amount))   # 상품·옵션·배송지·금액·한도 — 문제 있으면 PayStop
    log(f"결제 준비 완료: {amount:,}원")
    return amount, center(button)


def prepare(no: str, card: str) -> tuple:
    """결제하기 버튼 직전까지. (결제 예정 금액, 결제하기 좌표) 를 돌려준다. 금액은 LOCA 결제요청 화면과 다시 비교한다."""
    tap_cjone(open_order_sheet(no))
    return prepare_payment(card)


def wait_manual_auth(check, what: str):
    """PIN_MODE=manual: 사용자가 폰에서 직접 인증하기를 기다린다. 끝나면 자동으로 이어간다."""
    hook("auth_required", message=f"{what} — 폰에서 직접 결제 비밀번호를 입력해 주세요 ({MANUAL_AUTH_WAIT}초 대기)")
    log(f"★ {what} — 사용자 인증 대기")
    result = check()
    hook("auth_done")
    return result


def enter_loca_app(timeout: float = 30) -> None:
    """결제창 '결제하기' → LOCA 결제요청. 결제창은 dump 에 안 잡혀 OCR(뒤 주문서만 나옴, 실측).
    앱이 안 뜨면 결제창이 남아 있을 때만 다시 누른다 — 간편번호 전이라 이중결제 위험이 없다."""
    end, tapped_at = time.time() + timeout, 0.0
    while time.time() < end:
        now = focus()
        if "PayRequestActivity" in now:
            return
        if "lcacApp" not in now and time.time() - tapped_at > 5:
            texts = pl.ocr_texts(pl.adb_screencap())
            hit = next((t for t in texts if t["text"].strip() == "결제하기"), None)
            if hit and any("로카페이" in t["text"] for t in texts):
                pl.tap((hit["cx"], hit["cy"]))
                tapped_at = time.time()
                log("로카페이 결제하기")
        time.sleep(POLL_SEC)
    pl.shot_fail("pay", "loca")
    raise PayStop(f"LOCA 결제요청 미도달 (현재 화면: {focus().split('/')[-1]})")


def pay_lotte(amount: int, enter_pin: bool = True) -> bool:
    """로카페이 → LOCA 앱 금액·가맹점 확인 → 간편번호 → 확인. 3mall pay_lotte 와 같은 구간."""
    enter_loca_app()
    tree = wait_tree(lambda t: find(t, rid="bt_appcard_pay_request_payment") is not None, 10, "LOCA 결제요청")
    shown = [n.get("text", "") for n in nodes(tree)]
    if f"{amount:,}원" not in shown or "CJ올리브영" not in shown:
        raise PayStop("LOCA 결제요청의 금액·가맹점이 주문서와 다름")
    pl.tap(center(find(tree, rid="bt_appcard_pay_request_payment")))
    log("LOCA 금액·가맹점 확인, 결제하기")
    wait_focus("SimpleNumberAuthActivity", 15)
    if not enter_pin:
        log("PIN 화면 도달 — 입력하지 않고 멈춤")
        return False
    done = lambda t: find(t, rid="bt_appcard_pay_request_complete_confirm") is not None
    STATE["pin_sent"] = True   # 여기부터는 결제됐을 수 있다
    if PIN_MODE == "manual":
        tree = wait_manual_auth(lambda: wait_tree(done, MANUAL_AUTH_WAIT, "결제 요청 완료"), "롯데카드 간편번호")
    else:
        ocr_keypad.input_pin("lotte")
        tree = wait_tree(done, 20, "결제 요청 완료")
    pl.tap(center(find(tree, rid="bt_appcard_pay_request_complete_confirm")))
    return True


def wait_order_complete(amount: int) -> str:
    """주문완료 화면의 주문번호·금액 확인. 그 위에 뜨는 구글플레이 리뷰창은 '나중에'로 닫는다."""
    def done(t) -> bool:
        later = find(t, text="나중에")
        if later is not None and "vending" in focus():
            pl.tap(center(later))
            return False
        return find(t, text="주문을 완료했어요!") is not None or find(t, rid="mbr_delivery_id") is not None

    tree = wait_tree(done, 45, "주문완료")
    order = next((n.get("text") for n in nodes(tree) if re.fullmatch(r"Y\d{10,}", n.get("text", ""))), None)
    if not order:
        raise PayStop("주문완료 주문번호 확인 실패 — 주문내역을 직접 확인할 것")
    texts = [n.get("text", "") for n in nodes(tree)]
    if f"{amount:,}원" not in texts:  # 현대카드 앱 즉시할인(실측 12,190 → 10,971원)이면 금액이 달라진다
        paid = next((t for t in texts if re.fullmatch(r"[\d,]+원", t)), "?")
        log(f"주문완료 금액이 주문서({amount:,}원)와 다름: {paid} — 카드 할인 등, 주문번호로 완료 판정")
    return order


def enter_hyundai_app(timeout: float = 30) -> None:
    """결제하기 → (카드사 연결 화면 '앱카드 결제') → 현대카드 앱. 3mall pay_lotte_hyundai 경로, 올영 미실측.
    '앱카드' 글자는 앱이 안 떴을 때만 5초 간격으로 다시 누른다 — 비번 전이라 이중결제 위험이 없다."""
    end, tapped_at = time.time() + timeout, 0.0
    while time.time() < end:
        if HYUNDAI_PKG in focus():
            log("현대카드 앱 진입")
            return
        if time.time() - tapped_at > 5:
            hit = next((t for t in pl.ocr_texts(pl.adb_screencap()) if "앱카드" in t["text"]), None)
            if hit:
                pl.tap((hit["cx"], hit["cy"]))
                tapped_at = time.time()
                log(f"'{hit['text']}' 누름")
        time.sleep(POLL_SEC)
    pl.shot_fail("pay", "hyundai")
    raise PayStop(f"현대카드 앱 미진입 (현재 화면: {focus().split('/')[-1]})")


def hyundai_cards(tree) -> list[dict]:
    """현대카드 앱 카드 목록. 행 = btnRoot, 이름 = tvCardName, 선택 = 행 안에 ivCheckcardRegi (3mall 실측).
    이 화면은 FLAG_SECURE(스크린샷 검정) 라 dump 만 믿는다. 카드 순서는 매번 바뀐다 → 이름으로 찾는다."""
    out = []
    for row in nodes(tree):
        if rid_of(row) != "btnRoot":
            continue
        ids = {rid_of(d): d for d in row.iter("node")}
        name = ids["tvCardName"].get("text", "") if "tvCardName" in ids else ""
        if name:
            out.append({"name": name, "xy": center(row), "selected": "ivCheckcardRegi" in ids})
    return out


def select_hyundai_card(tries: int = 3):
    """HYUNDAI_CARD 선택 + dump 재확인. 다른 카드로 결제되느니 멈춘다. 목록은 늦게 뜬다(3mall: 최대 30초)."""
    for _ in range(tries):
        tree = wait_tree(lambda t: bool(hyundai_cards(t)), 30, "현대카드 카드 목록")
        cards = hyundai_cards(tree)
        hit = next((c for c in cards if HYUNDAI_CARD in c["name"]), None)
        if hit is None:
            raise PayStop(f"'{HYUNDAI_CARD}' 없음 (목록: {[c['name'] for c in cards]})")
        if hit["selected"]:
            log(f"카드 선택 확인: {hit['name']}")
            return tree
        pl.tap(hit["xy"])
        log(f"카드 탭: {hit['name']}")
    raise PayStop(f"'{HYUNDAI_CARD}' 선택 확인 실패")


def hyundai_keypad_ready(tree) -> bool:
    descs = [n.get("content-desc", "") for n in nodes(tree)]
    return sum(1 for d in descs if d.endswith(" 버튼") and d[:1].isdigit()) >= 10


def pay_hyundai(amount: int, enter_pin: bool = True) -> bool:
    """현대카드 앱: 카드 선택 → 금액 확인 → '결제 비밀번호 인증' → 셔플 키패드(desc='N 버튼') pin6 → 올영 복귀.
    3mall pay_lotte_hyundai(2026-06-12 라이브 검증) 경로. 6자리 입력 후 자동 제출된다."""
    enter_hyundai_app()
    tree = select_hyundai_card()
    if not any(f"{amount:,}" in n.get("text", "") for n in nodes(tree)):
        raise PayStop(f"현대카드 앱 결제금액이 주문서({amount:,}원)와 다름")
    auth = find(tree, text="결제 비밀번호 인증")
    if auth is None:
        raise PayStop("'결제 비밀번호 인증' 버튼 없음")
    pl.tap(center(auth))
    wait_tree(hyundai_keypad_ready, 15, "현대카드 키패드")
    if not enter_pin:
        log("PIN 화면 도달 — 입력하지 않고 멈춤")
        return False
    STATE["pin_sent"] = True   # 여기부터는 결제됐을 수 있다
    if PIN_MODE == "manual":
        wait_manual_auth(lambda: wait_focus("com.oliveyoung", MANUAL_AUTH_WAIT), "현대카드 결제 비밀번호")
    else:
        ocr_keypad.input_pin("hyundai")
        wait_focus("com.oliveyoung", 30)
    log("올영 복귀")
    return True


STATE = {"pin_sent": False}


def pay_by_card(card: str, button: tuple[int, int], amount: int, enter_pin: bool = True) -> bool:
    """결제하기 → 카드앱 결제. 남아 있던 카드앱 세션이 새 결제를 막으므로 먼저 강제 종료한다
    (현대: 3mall 실사고 / 롯데: 2026-11-27 LOCA 가 결제요청 대신 홈으로 뜸).
    PIN(또는 직접 인증) 이후의 중단은 PaymentSubmitted — 결제됐는지 모르므로 다시 결제하지 않는다."""
    STATE["pin_sent"] = False
    hook("payment_pending", amount=amount, card=card)   # 누르기 전에 기록 → 중간에 죽어도 '결과 불명'으로 남는다
    pl.shell("am", "force-stop", HYUNDAI_PKG if card == "hyundai" else "com.lcacApp", capture_output=True)
    pl.tap(button)
    try:
        return pay_lotte(amount, enter_pin) if card == "lotte" else pay_hyundai(amount, enter_pin)
    except Exception as e:
        if STATE["pin_sent"]:
            raise PaymentSubmitted(f"결제 인증 이후 중단 — 주문내역 확인 필요: {e}") from None
        raise


def finish_order(amount: int) -> str:
    """주문완료 확인. PIN 이후라 실패하면 결과 불명."""
    try:
        order = wait_order_complete(amount)
    except Exception as e:
        raise PaymentSubmitted(f"주문완료를 확인하지 못함 — 주문내역 확인 필요: {e}") from None
    hook("order_complete", order_no=order, amount=amount)
    log(f"주문 완료: {order} {amount:,}원")
    return order


def pay(no: str, card: str, enter_pin: bool = True) -> str | None:
    amount, button = prepare(no, card)
    if not pay_by_card(card, button, amount, enter_pin):
        return None
    return finish_order(amount)


def relogin_and_pay(no: str, card: str, enter_pin: bool = True) -> str | None:
    """지금 계정 로그아웃 → 2초 → no번 로그인 → 1초 → 결제. 프로세스 하나로 돌려 모듈 재로딩 시간을 없앤다."""
    if not pl.logout():
        raise PayStop("로그아웃 실패")
    log("로그아웃")
    time.sleep(2)
    result = pl.login(no, after_logout=True)
    if not result["ok"]:
        raise PayStop(f"로그인 실패: {result['status']}")
    log("로그인")
    time.sleep(1)
    return pay(no, card, enter_pin)


def relogin_to_pin(no: str, card: str) -> None:
    relogin_and_pay(no, card, enter_pin=False)


def main() -> int:
    if len(sys.argv) != 4 or sys.argv[1] not in ("prepare", "pay", "relogin-pay", "relogin-to-pin") or sys.argv[3] not in CARD_NAMES:
        print(__doc__)
        return 2
    cmd, no, card = sys.argv[1:]
    try:
        {"prepare": prepare, "pay": pay, "relogin-pay": relogin_and_pay, "relogin-to-pin": relogin_to_pin}[cmd](no, card)
    except (PayStop, RuntimeError, ValueError) as e:
        log(f"중단: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
