"""쓰레드 글 → 올영 큐레이팅 상품 링크 → 올리브영 앱 상품 → 바로구매 → 주문서 → 결제.

⚠️ 2026-11-10 실측 없이 짠 초안. 쓰레드 화면·링크 모양·상품 페이지·옵션 시트 전부 미실측이다.
   예상과 다르면 탭하지 않고 PayStop 으로 멈춘다. 주문서부터는 phone_pay 의 실측 경로를 그대로 쓴다.
로그인: 시작할 때 올영 앱을 켜서 지금 계정을 무조건 로그아웃 → accounts.json 의 <계정번호> 로 로그인 (사용자 지시 2026-11-18).
링크로 들어갔는데 홈(또는 로그인 폼)이 뜨면 올영 앱을 끄고 쓰레드부터 다시 연다(사용자 지시 2026-11-18).

<링크> = 쓰레드 글 주소, 또는 올영 상품 주소(바로 올영 앱으로 연다). [옵션번호] = 옵션이 여러 개일 때 몇 번째(1부터).
옵션이 1개면 번호 없이 그걸 산다. 여러 개인데 번호가 없으면 옵션 목록을 보여주고 멈춘다 (사용자 지시 2026-11-27).

사용:  python phone_auto/threads_buy.py prepare <계정번호> <lotte|hyundai> <링크> [옵션번호]   # 결제하기 직전까지
       python3 phone_auto/threads_buy.py pay <계정번호> <lotte|hyundai> <쓰레드링크>      # 주문완료까지 (실제 결제)
       python3 phone_auto/threads_buy.py pay-all <1-10|1,3,5> <lotte|hyundai> <쓰레드링크> # 계정마다 로그아웃→로그인→구매 반복
"""
from __future__ import annotations

import re
import shlex
import sys
import time

import adb_manager as adb
import phone_login as pl
import phone_pay as pp
from phone_pay import PayStop, log

OY_LINK = re.compile(r"(oliveyoung\.co\.kr|oy\.run)\S*", re.I)   # 큐레이터 링크 = oy.run 단축링크 (웹 확인 2026-11-18, 폰 화면은 미실측)
OY_PACKAGE = "com.oliveyoung"


def ocr_then_dump(pick):
    """OCR 로 먼저 찾고(pick(texts) → 결과), 못 찾았을 때만 dump 글자까지 본다.
    예전엔 매번 OCR + dump(≈3초) 를 함께 읽었다. OCR 이 먼저였으므로 OCR 에서 찾으면 결과는 같다."""
    ocr = pl.ocr_texts(pl.adb_screencap())
    hit = pick(ocr)
    if hit:
        return hit
    return pick(ocr + pl.dump_texts(pl.dump_tree(retries=1)))


def find_text(texts: list[dict], word: str, min_y: int = 0) -> dict | None:
    """min_y 는 실측(2400 높이) 기준 — 기기 해상도로 환산해 비교한다."""
    y = adb.sy(min_y)
    return next((t for t in texts if t["text"].strip() == word and t["cy"] >= y), None)


def open_threads(url: str) -> None:
    """링크를 VIEW 인텐트로 연다. 쓰레드 앱이 있으면 앱이, 없으면 브라우저가 받는다(미실측)."""
    if not re.match(r"https://(www\.)?threads\.(net|com)/", url):
        raise PayStop("쓰레드 링크가 아님")
    pl.shell("am", "start", "-a", "android.intent.action.VIEW", "-d", shlex.quote(url), capture_output=True)
    log("쓰레드 열기")


def is_oy_url(url: str) -> bool:
    return bool(re.match(r"https://([a-z]+\.)?(oliveyoung\.co\.kr|oy\.run)/", url))


def open_oy_direct(url: str, timeout: float = 15) -> None:
    """쓰레드 글이 아니라 올영 상품 주소를 받았을 때 — VIEW 인텐트를 올영 앱이 바로 받는다 (실측 2026-11-27)."""
    pl.shell("am", "start", "-a", "android.intent.action.VIEW", "-d", shlex.quote(url), capture_output=True)
    log("올영 상품 주소 열기")
    pp.wait_focus(OY_PACKAGE, timeout)


def login_on_form(no: str) -> None:
    """로그인 폼이 이미 떠 있을 때 no번 로그인. 폼에선 마이 탭 자리가 '회원가입'이라 FORM_OPEN 으로 재탭을 막는다."""
    pl.FORM_OPEN = True
    result = pl.login(no, after_logout=True)
    if not result["ok"]:
        raise PayStop(f"로그인 실패: {result['status']}")
    log("로그인")
    pp.check_account(no)


def relogin(no: str) -> None:
    """올영 앱을 켜서 지금 계정을 무조건 로그아웃 → no번 로그인 (사용자 지시 2026-11-18).
    이미 로그아웃 상태면 logout() 이 로그인 폼을 보고 FORM_OPEN 을 켠다. 마이 탭 닉네임으로 계정 확인.
    먼저 앱을 강제 종료해 홈에서 시작한다 — 주문서·상품 페이지엔 하단 탭이 없어 뒤로가기 5번으로 마이 탭에 못 닿았다(2026-11-27 실측)."""
    pl.shell("am", "force-stop", OY_PACKAGE, capture_output=True)
    if not pl.logout():
        raise PayStop("로그아웃 실패")
    log("로그아웃")
    time.sleep(2)   # phone_pay.relogin_and_pay 와 같은 대기
    result = pl.login(no, after_logout=True)
    if not result["ok"]:
        raise PayStop(f"로그인 실패: {result['status']}")
    log(f"{no}번 로그인")
    pp.check_account(no)


def oy_link_hits() -> dict:
    """화면의 올영 링크 {링크글자: 위치}. 경로 없는 도메인만(`oliveyoung.co.kr`)은 글 아래 링크 미리보기 카드라 뺀다
    (크롬 실측 2026-11-27). 크롬 페이지 글자는 dump 에 안 잡히고 OCR 로만 읽힌다(실측)."""
    def pick(texts):
        hits = {}
        for t in texts:
            m = OY_LINK.search(t["text"])
            if m and "/" in m.group(0):
                hits.setdefault(m.group(0).rstrip(".,)"), t)
        return hits
    return ocr_then_dump(pick)


def find_oy_link(scrolls: int = 4, load_wait: float = 10) -> dict:
    """글에서 올영 링크를 찾는다. 서로 다른 링크가 2개 이상이면 어떤 걸 살지 추측하지 않고 멈춘다.
    처음엔 페이지가 뜰 때까지 내리지 않고 기다린다 — 로딩 중에 내리면 링크를 지나친다."""
    end = time.time() + load_wait
    for _ in range(scrolls + 1):
        hits = oy_link_hits()
        while not hits and time.time() < end:
            hits = oy_link_hits()
        if len(hits) == 1:
            link, hit = next(iter(hits.items()))
            log(f"올영 링크: {link}")
            return hit
        if len(hits) > 1:
            raise PayStop(f"올영 링크가 여러 개: {', '.join(hits)} — 어떤 상품인지 지정 필요")
        pp.swipe_down()
    raise PayStop("쓰레드 글에서 올영 링크를 못 찾음")


def enter_oy_app(link: dict, timeout: float = 15) -> None:
    """링크 탭 → 올영 앱 전환 대기. 크롬 실측(2026-11-27): 첫 탭은 쓰레드 'Threads에서 대화하기' 팝업만 닫고,
    두 번째 탭에 올영 앱이 바로 열린다 → 3초 안에 안 넘어가면 같은 자리를 한 번 더 누른다."""
    pl.tap((link["cx"], link["cy"]))
    end, retap_at = time.time() + timeout, time.time() + 3
    while time.time() < end:
        if retap_at and time.time() > retap_at and OY_PACKAGE not in pp.focus():
            pl.tap((link["cx"], link["cy"]))
            log("링크 다시 누름 (첫 탭은 쓰레드 팝업 닫기)")
            retap_at = 0
        if OY_PACKAGE in pp.focus():
            log("올영 앱 진입")
            return
        time.sleep(pp.POLL_SEC)
    pl.shot_fail("threads", "oyapp")
    raise PayStop(f"올영 앱으로 안 넘어감 (현재 화면: {pp.focus().split('/')[-1]})")


def wait_landing(timeout: float = 15) -> str:
    """링크로 들어온 올영 앱 화면 판정. PRODUCT(하단 구매하기) / LOGIN(로그인 폼) / HOME(하단 마이 탭). ⚠️ 미실측.
    dump 는 뒤쪽 숨은 WebView 글자도 잡으므로 버튼은 실제 화면인 OCR 로만 본다."""
    end = time.time() + timeout
    while time.time() < end:
        # OCR 로 먼저 본다 — 상품·홈·없는 상품은 화면 글자로 확실하다. 로그인 폼(loginId)만 dump 로 본다.
        ocr = pl.ocr_texts(pl.adb_screencap())
        if any("상품을 찾을 수 없어요" in t["text"] for t in ocr):   # 실측 2026-11-27: 글의 링크 상품번호가 잘림
            pl.shot_fail("threads", "nogoods")
            raise PayStop("올영 '상품을 찾을 수 없어요' — 글의 링크가 잘못됐거나 판매종료")
        if find_text(ocr, "바로구매", min_y=1800):   # 실측 2026-11-27: 하단 '장바구니' / '바로구매'
            return "PRODUCT"
        if find_text(ocr, "마이", min_y=2000):
            return "HOME"
        if "loginId" in pl.dump_ids(pl.dump_tree(retries=1)):
            return "LOGIN"
    pl.shot_fail("threads", "landing")
    raise PayStop("링크 진입 후 화면 판정 불가")


def open_product(no: str, url: str, tries: int = 3) -> None:
    """쓰레드 → 링크 탭 → 상품 페이지. 홈이 뜨면 올영 앱을 끄고 쓰레드부터 다시 연다.
    로그인 폼이 뜨면 로그인 후 같은 방식으로 다시 연다 (사용자 지시 2026-11-18)."""
    for i in range(1, tries + 1):
        if is_oy_url(url):
            open_oy_direct(url)
        else:
            open_threads(url)
            enter_oy_app(find_oy_link())
        screen = wait_landing()
        log(f"링크 진입 {i}/{tries}: {screen}")
        if screen == "PRODUCT":
            return
        if screen == "LOGIN":
            login_on_form(no)
        pl.shell("am", "force-stop", OY_PACKAGE, capture_output=True)
        log("올영 앱 종료 → 쓰레드부터 다시")
    raise PayStop("링크로 상품 페이지가 안 열림")


def tap_word(word: str, timeout: float, min_y: int = 0) -> None:
    """글자가 보일 때까지 읽고 탭한다. 화면 아래쪽 버튼만 보려면 min_y."""
    end = time.time() + timeout
    while time.time() < end:
        hit = ocr_then_dump(lambda texts: find_text(texts, word, min_y))
        if hit:
            pl.tap((hit["cx"], hit["cy"]))
            log(f"'{word}' 누름")
            return
    pl.shot_fail("threads", word)
    raise PayStop(f"'{word}' 버튼을 못 찾음")


def option_name(text: str) -> str:
    """옵션 행 글자 = '<이름> <이름> 10% 16,200원 …' (이름이 두 번, 실측) → 이름."""
    for i in range(len(text) // 2, 0, -1):
        if text.startswith(text[:i] + " " + text[:i]):
            return text[:i]
    return text


def option_rows(tree) -> list:
    """옵션 목록 행(실측 2026-11-27): Button, x 84~995, 글자에 가격('원'). 화면 밖 행은 높이 0."""
    return [n for n in pp.nodes(tree) if n.get("class", "").endswith("Button") and "원" in n.get("text", "")
            and pl._bounds(n)[0] < adb.sx(100) and pl._bounds(n)[2] > adb.sx(900)]


def shown(tree, cls: str, text: str | None = None, min_y: int = 0):
    """높이가 있는(실제로 보이는) cls 노드. 화면 밖 노드는 높이 0 으로 dump 에 남아 있다 (실측)."""
    for n in pp.nodes(tree):
        x1, y1, x2, y2 = pl._bounds(n)
        if n.get("class", "").endswith(cls) and y2 - y1 > 20 and y1 >= adb.sy(min_y) and (text is None or n.get("text") == text):
            return n
    return None


def selected_ready(tree) -> bool:
    """옵션이 골라진 상태: 수량 EditText + 하단 '바로구매' Button 이 보인다 (실측 [630,2088][1040,2232])."""
    return shown(tree, "EditText") is not None and shown(tree, "Button", "바로구매", min_y=1800) is not None


def scroll_option_into_view(index: int, tree=None):
    """index(1부터)번째 옵션 행이 시트 안(y 1219~2200)에 보일 때까지 시트를 민다. tree 를 주면 첫 판정에 그대로 쓴다."""
    for _ in range(15):
        tree = tree if tree is not None else pl.dump_tree(retries=2)
        rows = option_rows(tree)
        if index > len(rows):
            raise PayStop(f"옵션 {index}번 없음 (옵션 {len(rows)}개)")
        x1, y1, x2, y2 = pl._bounds(rows[index - 1])
        if y2 - y1 >= adb.sy(100) and y1 >= adb.sy(1219) and y2 <= adb.sy(2200):
            return rows[index - 1]
        down = y2 - y1 < adb.sy(100) or y2 > adb.sy(2200)
        pp.swipe((540, 1900 if down else 1400), (540, 1400 if down else 1900), 300)
        tree = None   # 밀었으니 다시 읽는다
    raise PayStop(f"옵션 {index}번을 화면에 띄우지 못함")


def choose_option(tree, option: int | None):
    """옵션 1개면 그걸, 여러 개면 option 번째(1부터)를 고른다. 여러 개인데 번호가 없으면 목록을 보여주고 멈춘다."""
    rows = option_rows(tree)
    names = [option_name(n.get("text", "")) for n in rows]
    if len(rows) > 1 and option is None:
        listing = ", ".join(f"{i}.{n}" for i, n in enumerate(names, 1))
        raise PayStop(f"옵션 {len(rows)}개 — 몇 번째인지 지정 필요: {listing}")
    index = option or 1
    row = scroll_option_into_view(index, tree)   # 방금 읽은 옵션 목록을 그대로 쓴다(dump 1회 절약)
    if "품절" in row.get("text", ""):
        raise PayStop(f"옵션 {index}번 품절: {names[index - 1]}")
    pl.tap(pp.center(row))
    tree = pp.wait_tree(selected_ready, 8, "옵션 선택")
    if shown(tree, "TextView", names[index - 1]) is None:   # 위쪽 컬러 썸네일은 Button 이라 구분된다
        raise PayStop(f"선택된 옵션이 {index}번({names[index - 1]})이 아님")
    log(f"옵션 {index}번 선택: {names[index - 1]}")
    return tree   # 선택 확인에 쓴 화면 — 수량·바로구매 판정에 그대로 쓴다


def quantity_value(tree) -> int | None:
    box = shown(tree, "EditText")
    if box is None:
        return None
    digits = re.sub(r"\D", "", box.get("text", ""))
    return int(digits) if digits else None


def set_quantity(tree, quantity: int):
    """수량 EditText 옆 '수량 증가'(content-desc, 2026-11-27 dump 실측)를 눌러 맞추고 값으로 확인한다.
    ⚠️ 2개 이상 구매는 폰 미실측 — 버튼이나 값이 예상과 다르면 탭을 멈춘다."""
    if quantity == 1 and quantity_value(tree) in (None, 1):
        return tree
    if quantity < 1 or quantity > 10:
        raise PayStop(f"구매 수량 범위 밖: {quantity} (1~10)")
    for _ in range(quantity + 3):
        cur = quantity_value(tree)
        if cur is None:
            raise PayStop("수량 칸을 찾지 못함")
        if cur == quantity:
            log(f"수량 {quantity}")
            return tree
        if cur > quantity:
            raise PayStop(f"수량이 목표보다 큼({cur} > {quantity}) — 멈춤")
        plus = next((n for n in pp.nodes(tree) if n.get("content-desc", "") == "수량 증가"
                     and pl._bounds(n)[3] - pl._bounds(n)[1] > 20), None)
        if plus is None:
            raise PayStop("'수량 증가' 버튼을 찾지 못함")
        pl.tap(pp.center(plus))
        tree = pp.wait_tree(lambda t: quantity_value(t) not in (None, cur), 4, "수량 변경")
    raise PayStop("수량을 맞추지 못함")


def buy_now(option: int | None = None, quantity: int = 1):
    """상품 페이지 하단 '바로구매'(실측: '구매하기' 버튼은 없음) → 옵션 목록이면 고르기 → 수량 → '바로구매' → 주문서."""
    tap_word("바로구매", 10, min_y=1800)
    tree = pp.wait_tree(lambda t: bool(option_rows(t)) or selected_ready(t) or bool(pp.text_of(t, "addrSummaryName")),
                        10, "바로구매 이후 화면")
    if option_rows(tree):
        tree = choose_option(tree, option)   # (예전: 여기서 dump 를 한 번 더 했다)
    if not pp.text_of(tree, "addrSummaryName"):
        tree = set_quantity(tree, quantity)
    elif quantity != 1:
        raise PayStop("수량 선택 화면 없이 주문서로 넘어감 — 수량 2개 이상은 확인 불가")
    if not pp.text_of(tree, "addrSummaryName"):
        button = shown(tree, "Button", "바로구매", min_y=1800)
        if button is None:
            raise PayStop("옵션 선택 후 '바로구매' 버튼 없음")
        pl.tap(pp.center(button))
        log("'바로구매' 누름 (옵션 선택 후)")
    tree = pp.wait_tree(lambda t: bool(pp.text_of(t, "addrSummaryName")), 15, "주문서")
    if pp.text_of(tree, "baseDlvpYnFlag") != "기본배송지":
        raise PayStop("기본배송지가 아님 — 변경 경로 미실측")
    log(f"배송지: {pp.text_of(tree, 'addrSummaryName')}")
    return tree


def prepare(no: str, card: str, url: str, option: int | None = None, quantity: int = 1) -> tuple:
    """결제하기 버튼 직전까지. 주문서부터는 phone_pay.prepare 와 같은 순서."""
    relogin(no)
    open_product(no, url)
    pp.tap_cjone(buy_now(option, quantity))
    return pp.prepare_payment(card)


def pay(no: str, card: str, url: str, option: int | None = None, quantity: int = 1) -> str | None:
    amount, button = prepare(no, card, url, option, quantity)
    if not pp.pay_by_card(card, button, amount):
        return None
    return pp.finish_order(amount)


def parse_accounts(spec: str) -> list[str]:
    """'1-10' 또는 '1,3,5' → ['1', ...]."""
    if re.fullmatch(r"\d+-\d+", spec):
        a, b = map(int, spec.split("-"))
        return [str(n) for n in range(a, b + 1)]
    if re.fullmatch(r"\d+(,\d+)*", spec):
        return spec.split(",")
    raise ValueError(f"계정번호 형식 오류: {spec} (예: 1-10 또는 1,3,5)")


def pay_all(spec: str, card: str, url: str, option: int | None = None) -> None:
    """계정마다 로그아웃 → 로그인 → 쓰레드 → 구매를 순서대로. 한 계정이라도 멈추면 전체를 멈춘다(이중결제 방지)."""
    nos = parse_accounts(spec)
    for no in nos:
        pl.load_account(no)   # id/pw 비어 있는 계정이 있으면 결제 시작 전에 종료
    done = []
    for i, no in enumerate(nos, 1):
        log(f"===== {no}번 계정 ({i}/{len(nos)})")
        try:
            order = pay(no, card, url, option)
        except (PayStop, RuntimeError, ValueError) as e:
            raise PayStop(f"{no}번에서 멈춤: {e} | 완료: {', '.join(done) or '없음'} | 재실행 전 {no}번 주문내역 확인")
        done.append(f"{no}번 {order}")
    log(f"전체 완료: {', '.join(done)}")


def main() -> int:
    args = sys.argv[1:]
    if len(args) not in (4, 5) or args[0] not in ("prepare", "pay", "pay-all") or args[2] not in pp.CARD_NAMES \
            or (len(args) == 5 and not args[4].isdigit()):
        print(__doc__)
        return 2
    cmd, no, card, url = args[:4]
    option = int(args[4]) if len(args) == 5 else None
    try:
        {"prepare": prepare, "pay": pay, "pay-all": pay_all}[cmd](no, card, url, option)
    except (PayStop, RuntimeError, ValueError) as e:
        log(f"중단: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
