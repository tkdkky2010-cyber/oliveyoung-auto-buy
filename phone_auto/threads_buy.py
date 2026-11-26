"""쓰레드 글 → 올영 큐레이팅 상품 링크 → 올리브영 앱 상품 → 바로구매 → 주문서 → 결제.

⚠️ 2026-11-10 실측 없이 짠 초안. 쓰레드 화면·링크 모양·상품 페이지·옵션 시트 전부 미실측이다.
   예상과 다르면 탭하지 않고 PayStop 으로 멈춘다. 주문서부터는 phone_pay 의 실측 경로를 그대로 쓴다.
로그인: 시작할 때 올영 앱을 켜서 지금 계정을 무조건 로그아웃 → accounts.json 의 <계정번호> 로 로그인 (사용자 지시 2026-11-18).
링크로 들어갔는데 홈(또는 로그인 폼)이 뜨면 올영 앱을 끄고 쓰레드부터 다시 연다(사용자 지시 2026-11-18).

사용:  python3 phone_auto/threads_buy.py prepare <계정번호> <lotte|hyundai> <쓰레드링크>   # 결제하기 직전까지
       python3 phone_auto/threads_buy.py pay <계정번호> lotte <쓰레드링크>                # 주문완료까지 (실제 결제)
       python3 phone_auto/threads_buy.py pay-all <1-10|1,3,5> lotte <쓰레드링크>         # 계정마다 로그아웃→로그인→구매 반복
"""
from __future__ import annotations

import re
import sys
import time

import phone_login as pl
import phone_pay as pp
from phone_pay import PayStop, log

OY_LINK = re.compile(r"(oliveyoung\.co\.kr|oy\.run)\S*", re.I)   # 큐레이터 링크 = oy.run 단축링크 (웹 확인 2026-11-18, 폰 화면은 미실측)
OY_PACKAGE = "com.oliveyoung"


def screen_texts() -> list[dict]:
    """OCR(실제 화면) + dump(WebView 글자). 둘 다 같은 형식 {text,cx,cy}."""
    return pl.ocr_texts(pl.adb_screencap()) + pl.dump_texts(pl.dump_tree(retries=1))


def find_text(texts: list[dict], word: str, min_y: int = 0) -> dict | None:
    return next((t for t in texts if t["text"].strip() == word and t["cy"] >= min_y), None)


def open_threads(url: str) -> None:
    """링크를 VIEW 인텐트로 연다. 쓰레드 앱이 있으면 앱이, 없으면 브라우저가 받는다(미실측)."""
    if not re.match(r"https://(www\.)?threads\.(net|com)/", url):
        raise PayStop("쓰레드 링크가 아님")
    pl.shell("am", "start", "-a", "android.intent.action.VIEW", "-d", url, capture_output=True)
    log("쓰레드 열기")


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
    이미 로그아웃 상태면 logout() 이 로그인 폼을 보고 FORM_OPEN 을 켠다. 마이 탭 닉네임으로 계정 확인."""
    if not pl.logout():
        raise PayStop("로그아웃 실패")
    log("로그아웃")
    time.sleep(2)   # phone_pay.relogin_and_pay 와 같은 대기
    result = pl.login(no, after_logout=True)
    if not result["ok"]:
        raise PayStop(f"로그인 실패: {result['status']}")
    log(f"{no}번 로그인")
    pp.check_account(no)


def find_oy_link(scrolls: int = 4) -> dict:
    """글에서 올영 링크를 찾는다. 서로 다른 링크가 2개 이상이면 어떤 걸 살지 추측하지 않고 멈춘다."""
    for _ in range(scrolls + 1):
        hits = {}
        for t in screen_texts():
            m = OY_LINK.search(t["text"])
            if m:
                hits.setdefault(m.group(0).rstrip(".,)"), t)
        if len(hits) == 1:
            link, hit = next(iter(hits.items()))
            log(f"올영 링크: {link}")
            return hit
        if len(hits) > 1:
            raise PayStop(f"올영 링크가 여러 개: {', '.join(hits)} — 어떤 상품인지 지정 필요")
        pp.swipe_down()
    raise PayStop("쓰레드 글에서 올영 링크를 못 찾음")


def enter_oy_app(link: dict, timeout: float = 15) -> None:
    """링크 탭 → 올영 앱 전환 대기. 쓰레드 인앱 브라우저로 열리면(미실측) 멈춘다."""
    pl.tap((link["cx"], link["cy"]))
    end = time.time() + timeout
    while time.time() < end:
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
        if "loginId" in pl.dump_ids(pl.dump_tree(retries=1)):
            return "LOGIN"
        ocr = pl.ocr_texts(pl.adb_screencap())
        if find_text(ocr, "구매하기", min_y=1800):
            return "PRODUCT"
        if find_text(ocr, "마이", min_y=2000):
            return "HOME"
    pl.shot_fail("threads", "landing")
    raise PayStop("링크 진입 후 화면 판정 불가")


def open_product(no: str, url: str, tries: int = 3) -> None:
    """쓰레드 → 링크 탭 → 상품 페이지. 홈이 뜨면 올영 앱을 끄고 쓰레드부터 다시 연다.
    로그인 폼이 뜨면 로그인 후 같은 방식으로 다시 연다 (사용자 지시 2026-11-18)."""
    for i in range(1, tries + 1):
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
        hit = find_text(screen_texts(), word, min_y)
        if hit:
            pl.tap((hit["cx"], hit["cy"]))
            log(f"'{word}' 누름")
            return
    pl.shot_fail("threads", word)
    raise PayStop(f"'{word}' 버튼을 못 찾음")


def buy_now():
    """상품 페이지 하단 '구매하기' → 시트 '바로구매' → 주문서. 옵션 선택이 필요한 상품은 멈춘다(미실측)."""
    tap_word("구매하기", 10, min_y=1800)
    texts = screen_texts()
    if any("옵션" in t["text"] and "선택" in t["text"] for t in texts):
        raise PayStop("옵션 선택이 필요한 상품 — 옵션 시트 미실측")
    tap_word("바로구매", 5, min_y=1500)
    tree = pp.wait_tree(lambda t: bool(pp.text_of(t, "addrSummaryName")), 10, "주문서")
    if pp.text_of(tree, "baseDlvpYnFlag") != "기본배송지":
        raise PayStop("기본배송지가 아님 — 변경 경로 미실측")
    log(f"배송지: {pp.text_of(tree, 'addrSummaryName')}")
    return tree


def prepare(no: str, card: str, url: str) -> tuple:
    """결제하기 버튼 직전까지. 주문서부터는 phone_pay.prepare 와 같은 순서."""
    relogin(no)
    open_product(no, url)
    pp.tap_cjone(buy_now())
    tree = pp.select_card(card, pp.payment_area())
    pp.agree_all(tree)
    button = pp.find(tree, rid="btnPay")
    amount = pp.number(button.get("text", ""))
    log(f"결제 준비 완료: {amount:,}원")
    return amount, pp.center(button)


def pay(no: str, card: str, url: str) -> str | None:
    if card != "lotte":
        raise PayStop("현대카드 앱 구간은 아직 미실측")
    amount, button = prepare(no, card, url)
    pl.tap(button)
    pp.pay_lotte(amount)
    order = pp.wait_order_complete(amount)
    log(f"주문 완료: {order} {amount:,}원")
    return order


def parse_accounts(spec: str) -> list[str]:
    """'1-10' 또는 '1,3,5' → ['1', ...]."""
    if re.fullmatch(r"\d+-\d+", spec):
        a, b = map(int, spec.split("-"))
        return [str(n) for n in range(a, b + 1)]
    if re.fullmatch(r"\d+(,\d+)*", spec):
        return spec.split(",")
    raise ValueError(f"계정번호 형식 오류: {spec} (예: 1-10 또는 1,3,5)")


def pay_all(spec: str, card: str, url: str) -> None:
    """계정마다 로그아웃 → 로그인 → 쓰레드 → 구매를 순서대로. 한 계정이라도 멈추면 전체를 멈춘다(이중결제 방지)."""
    nos = parse_accounts(spec)
    for no in nos:
        pl.load_account(no)   # id/pw 비어 있는 계정이 있으면 결제 시작 전에 종료
    done = []
    for i, no in enumerate(nos, 1):
        log(f"===== {no}번 계정 ({i}/{len(nos)})")
        try:
            order = pay(no, card, url)
        except (PayStop, RuntimeError, ValueError) as e:
            raise PayStop(f"{no}번에서 멈춤: {e} | 완료: {', '.join(done) or '없음'} | 재실행 전 {no}번 주문내역 확인")
        done.append(f"{no}번 {order}")
    log(f"전체 완료: {', '.join(done)}")


def main() -> int:
    if len(sys.argv) != 5 or sys.argv[1] not in ("prepare", "pay", "pay-all") or sys.argv[3] not in pp.CARD_NAMES:
        print(__doc__)
        return 2
    cmd, no, card, url = sys.argv[1:]
    try:
        {"prepare": prepare, "pay": pay, "pay-all": pay_all}[cmd](no, card, url)
    except (PayStop, RuntimeError, ValueError) as e:
        log(f"중단: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
