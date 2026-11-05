"""올리브영 PC 장바구니 — 로그인 → 비우기 → 옵션·수량 담기 → 장바구니 재검증.

CFT 9250(launch_cft.sh)에 CDP 로 붙어 **기존 창 하나**만 쓴다.
계정별 새 컨텍스트는 새 창을 띄워 포커스를 뺏으므로(2026-11-04 실측) 쓰지 않고,
계정을 바꿀 때 쿠키만 sessions/<번호>.json 으로 바꿔 끼운다.

로그인 폼 제출 = 하루 계정당 최대 2회. 2회 실패면 그 계정은 그날 중단(봇 탐지일 수 있음).
아이디·비번은 accounts.json 에서 이 스크립트가 직접 읽고, 화면·로그에 출력하지 않는다.

사용:  python3 pc_cart.py login 1
       python3 pc_cart.py cart 2 <상품링크> <옵션명(부분일치)> <수량> <일반|오늘드림>
       python3 pc_cart.py items 1 <수량> <일반|오늘드림> <상품링크> [상품링크 ...]   (옵션 없는 상품 여러 개)
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent
CDP_URL = f"http://127.0.0.1:{os.environ.get('OY_CDP_PORT', '9250')}"
ACCOUNTS = ROOT / "accounts.json"
SESS_DIR = ROOT / "sessions"
LOG_DIR = ROOT / "logs"
FAIL_FILE = LOG_DIR / "login_fail.json"
MAIN_URL = "https://www.oliveyoung.co.kr/store/main/main.do"
LOGIN_URL = "https://www.oliveyoung.co.kr/store/login/loginForm.do"
MAX_FAIL = 2
LOGIN_LINK = 'a[data-attr="공통^헤더^로그인"]'   # 비로그인 상단헤더 (실측)
CART_URL = "https://www.oliveyoung.co.kr/shop/cart?ctype=delivery"   # 2026-11 개편 장바구니(일반·오늘드림·픽업 통합)
CART_CARD = "[data-cart-number]"                                   # 장바구니 상품 카드 (실측)


class AskUser(Exception):
    """사용자에게 물어야 하는 상황(옵션 후보 0개·2개 이상, 품절) — 추측해서 진행하지 않는다."""


def load_account(no: str) -> dict:
    accts = json.loads(ACCOUNTS.read_text(encoding="utf-8"))
    acct = accts.get(no)
    if not acct or not acct.get("id") or not acct.get("pw"):
        sys.exit(f"[login] accounts.json 에 {no}번 id/pw 가 비어 있음")
    return acct


def _fail_log() -> dict:
    return json.loads(FAIL_FILE.read_text(encoding="utf-8")) if FAIL_FILE.exists() else {}


def fails_today(no: str) -> list:
    return _fail_log().get(dt.date.today().isoformat(), {}).get(no, [])


def record_fail(no: str, reason: str) -> int:
    log = _fail_log()
    day = log.setdefault(dt.date.today().isoformat(), {})
    day.setdefault(no, []).append({"at": dt.datetime.now().isoformat(timespec="seconds"), "reason": reason})
    LOG_DIR.mkdir(exist_ok=True)
    FAIL_FILE.write_text(json.dumps(log, ensure_ascii=False, indent=1), encoding="utf-8")
    return len(day[no])


async def oy_page(ctx):
    """올리브영 탭을 재사용하고, 없으면 새 탭을 연다. 다른 탭(3mall 등)은 건드리지 않는다."""
    for pg in ctx.pages:
        if "oliveyoung" in pg.url:
            return pg
    return await ctx.new_page()


def _is_cf(cookie: dict) -> bool:
    return cookie["name"].startswith(("cf_", "__cf", "_cfuvid"))


async def wait_cf(page, timeout_s: int = 30) -> None:
    """Cloudflare '잠시만 기다리십시오' 가 지나갈 때까지 기다린다."""
    for _ in range(timeout_s):
        if "잠시만" not in await page.title():
            return
        await page.wait_for_timeout(1000)
    raise RuntimeError("Cloudflare 대기 화면이 30초 넘게 안 풀림")


async def swap_session(ctx, no: str) -> bool:
    """올리브영 쿠키를 <no>번 저장 세션으로 바꾼다(Cloudflare 쿠키는 유지). 저장 세션 유무 반환."""
    # 3mall CFT 를 같이 쓰므로 올리브영 쿠키만 지운다 — 전체를 지우면 3mall 로그인이 풀린다
    keep = [c for c in await ctx.cookies() if "oliveyoung" in c["domain"] and _is_cf(c)]
    await ctx.clear_cookies(domain=re.compile("oliveyoung"))
    sess = SESS_DIR / f"{no}.json"
    saved = json.loads(sess.read_text(encoding="utf-8")) if sess.exists() else []
    if keep + saved:
        await ctx.add_cookies(keep + saved)
    return bool(saved)


async def save_session(ctx, no: str) -> None:
    cookies = [c for c in await ctx.cookies() if "oliveyoung" in c["domain"] and not _is_cf(c)]
    SESS_DIR.mkdir(exist_ok=True)
    path = SESS_DIR / f"{no}.json"
    path.write_text(json.dumps(cookies, ensure_ascii=False), encoding="utf-8")
    path.chmod(0o600)


async def is_logged_in(page) -> bool:
    """메인 상단헤더가 그려졌는데 '로그인' 링크가 없으면 로그인 상태.
    (로그인 후 헤더 문구는 아직 미실측이라 '로그아웃' 문구에 기대지 않는다.)"""
    await page.goto(MAIN_URL, wait_until="domcontentloaded")
    await wait_cf(page)
    await page.wait_for_timeout(1500)
    header_drawn = await page.locator('a[data-attr^="공통^헤더^"]').count() > 0
    if not header_drawn:
        raise RuntimeError("메인 상단헤더가 안 그려짐 — 로그인 여부 판정 불가")
    return await page.locator(LOGIN_LINK).count() == 0


async def wait_turnstile(page, timeout_s: int = 30) -> None:
    """Cloudflare Turnstile 토큰이 채워질 때까지 기다린다.
    비어 있는 채로 누르면 doLogin 이 알림 없이 '인증을 완료한 후 로그인 버튼을 눌러주세요.' 만 띄우고
    제출하지 않는다(2026-11-04 #1 2회 '로그인 화면에 머묾' 원인, doLogin 소스·error-message 실측).
    시간 안에 안 채워지면 클릭 전에 예외 — 제출을 안 했으니 실패 횟수에 넣지 않는다."""
    token = page.locator('input[name="cf-turnstile-response"]')
    for _ in range(timeout_s):
        if await token.count() and await token.first.input_value():
            return
        await page.wait_for_timeout(1000)
    raise RuntimeError("Cloudflare 인증 토큰이 30초 넘게 안 채워짐 — 제출하지 않음")


async def submit_login(page, acct: dict) -> str:
    """로그인 폼 1회 제출. 실패 사유 문자열(성공 시 빈 문자열)을 반환."""
    dialogs: list[str] = []

    async def on_dialog(d):
        dialogs.append(d.message)
        await d.accept()

    page.on("dialog", on_dialog)
    try:
        await page.goto(LOGIN_URL, wait_until="domcontentloaded")
        await wait_cf(page)
        # ★2026-11-05 #1: 자동완성 값 뒤에 이어 입력돼 아이디가 두 번 들어갔다 → 먼저 비우고, 제출 전 값을 확인한다
        for sel, value in (("#loginId", acct["id"]), ("#password", acct["pw"])):
            box = page.locator(sel)
            await box.fill("")
            await box.press_sequentially(value, delay=70)
            if await box.input_value() != value:
                raise RuntimeError(f"{sel} 입력값이 계정 정보와 다름 — 제출하지 않음")
        await wait_turnstile(page)
        await page.locator("button.btn-login").click()   # onclick=login.doLogin() (실측)
        try:
            await page.wait_for_url(lambda u: "loginForm" not in u, timeout=15000)
        except Exception:
            return "로그인 화면에 머묾" + (f" / 알림: {' | '.join(dialogs)}" if dialogs else "")
        await page.wait_for_timeout(3000)
        # ★2026-11-05 #1: 잠깐 이동했다 로그인 폼으로 되돌아왔는데 메인으로 넘어가 사유 문구를 잃었다 → 여기서 읽는다
        if "loginForm" in page.url:
            shown = await page.evaluate("""() => [...document.querySelectorAll('[class*="error"], [role="alert"], .ly-pop, [class*="layer"]')]
                .filter(e => e.offsetParent).map(e => e.innerText.replace(/\\s+/g, ' ').trim()).filter(Boolean)""")
            return f"로그인 폼으로 되돌아옴 / 화면: {shown or '-'} / 알림: {dialogs or '-'}"
        print(f"[login] 제출 후 이동: {page.url}")
        return ""
    finally:
        page.remove_listener("dialog", on_dialog)


async def login(ctx, page, no: str) -> bool:
    if await swap_session(ctx, no) and await is_logged_in(page):
        print(f"[login] {no}번 저장 세션으로 로그인 상태 확인")
        return True
    if len(fails_today(no)) >= MAX_FAIL:
        print(f"[login] {no}번 오늘 {MAX_FAIL}회 실패 — 중단. 사용자 확인 필요(봇 탐지 가능성)")
        return False
    reason = await submit_login(page, load_account(no))
    if not reason and await is_logged_in(page):
        await save_session(ctx, no)
        print(f"[login] {no}번 로그인 성공 — 세션 저장")
        return True
    reason = reason or "제출 후에도 상단헤더가 비로그인 상태"
    LOG_DIR.mkdir(exist_ok=True)
    await page.screenshot(path=str(LOG_DIR / f"login_fail_{no}_{dt.datetime.now():%H%M%S}.png"))
    n = record_fail(no, reason)
    print(f"[login] {no}번 실패({n}/{MAX_FAIL}) — {reason}. 비번 오류로 단정하지 말 것")
    return False


async def open_cart(page) -> None:
    """장바구니 열기 + '장바구니가 달라졌어요!' 안내 모달(확인) 닫기 — 안 닫으면 모든 클릭이 막힌다(실측)."""
    await page.goto(CART_URL, wait_until="domcontentloaded")
    await wait_cf(page)
    await wait_cart_rendered(page)
    # ★2026-11-05 #1: 팝업이 카드보다 늦게 떠서 바로 확인하면 놓친다 → 장바구니에 들어올 때마다 5초까지 지켜보고 닫는다
    notice = page.locator(".ReactModalPortal button:visible", has_text="확인")
    try:
        await notice.first.wait_for(state="visible", timeout=5000)
    except Exception:
        return
    await notice.first.click()
    await notice.first.wait_for(state="hidden", timeout=5000)
    print("[cart] '장바구니가 달라졌어요' 팝업 확인")


async def cart_header_count(page) -> int | None:
    """상단헤더 '장바구니(N)' 의 N (실측). 빈 장바구니는 헤더가 숫자 없는 '장바구니' 라
    본문 '장바구니에 담긴 상품이 없어요' 로 0 을 판정한다(실측). 아직 안 그려졌으면 None."""
    if await page.get_by_text("장바구니에 담긴 상품이 없어요").count():
        return 0
    texts = await page.locator("a", has_text="장바구니").all_inner_texts()
    for t in texts:
        m = re.search(r"장바구니\s*\((\d+)\)", t)
        if m:
            return int(m.group(1))
    return None


async def wait_cart_rendered(page, timeout_s: int = 20) -> None:
    """카드 수가 헤더 '장바구니(N)' 와 같아질 때까지 기다린다.
    ★2026-11-04 #2: 카드가 그려지기 전에 읽어 빈 목록 → 아무것도 안 지우고 '비우기 성공'으로 넘어갔다(조용한 SKIP)."""
    for _ in range(timeout_s * 2):
        n = await cart_header_count(page)
        if n is not None and await page.locator(CART_CARD).count() == n:
            return
        await page.wait_for_timeout(500)
    raise RuntimeError(f"장바구니 화면 카드 수가 헤더 '장바구니({n})' 와 {timeout_s}초 동안 안 맞음")


async def read_cart(page) -> list[dict]:
    """장바구니 카드 → [{name, option, qty}]. 카드 첫 줄=상품명, '옵션변경' 버튼이 있으면 둘째 줄=옵션명(실측)."""
    return await page.evaluate("""() => [...document.querySelectorAll('[data-cart-number]')].map(c => {
        const lines = (c.innerText || '').split('\\n').map(s => s.trim()).filter(Boolean);
        const qty = c.querySelector('input[aria-label="수량"]');
        const hasOpt = [...c.querySelectorAll('button')].some(b => b.innerText.trim() === '옵션변경');
        return {name: lines[0] || '', option: hasOpt ? (lines[1] || '') : '', qty: qty ? Number(qty.value) : null};
    })""")


async def _accept(dialog) -> None:
    print(f"[cart] 알림: {dialog.message} → 확인")
    await dialog.accept()


async def clear_cart(page) -> list[dict]:
    """장바구니를 비운다(전날 카트는 지워도 된다 — 사용자 규칙). 지운 항목은 출력해 남긴다.
    삭제 확인 = 네이티브 '선택한 상품을 장바구니에서 삭제하시겠어요?'(실측)."""
    await open_cart(page)
    removed = await read_cart(page)
    page.on("dialog", _accept)
    try:
        for _ in range(len(removed) + 2):
            btn = page.locator(f'{CART_CARD} button[aria-label$=" 삭제"]')
            if not await btn.count():
                break
            await btn.first.click()
            await page.wait_for_timeout(1500)
    finally:
        page.remove_listener("dialog", _accept)
    for it in removed:
        print(f"[cart] 삭제: {it['name']} / {it['option'] or '-'} ×{it['qty']}")
    await open_cart(page)
    if await read_cart(page):
        raise RuntimeError("장바구니 비우기 후에도 상품이 남아 있음")
    return removed


async def pick_option(page, option: str) -> str:
    """옵션 부분일치로 1개를 고른다. 후보 0개·2개 이상·품절이면 AskUser. 고른 옵션 정식명을 반환."""
    await page.locator('[class*="OptionSelector_btn-option"]').first.click()
    await page.wait_for_timeout(1000)
    items = page.locator('[class*="OptionSelector_option-item-btn"]')
    titles = [await items.nth(i).locator('[class*="OptionSelector_option-item-tit"]').inner_text()
              for i in range(await items.count())]
    hits = [i for i, t in enumerate(titles) if option in t]
    if len(hits) != 1:
        raise AskUser(f"옵션 '{option}' 후보 {len(hits)}개 — {[titles[i] for i in hits] or titles}")
    btn = items.nth(hits[0])
    # ⚠️ 품절 표시는 미실측(오늘 품절 옵션 없음) — '품절' 글자·disabled 둘 다 본다
    if "품절" in await btn.inner_text() or await btn.is_disabled():
        raise AskUser(f"옵션 '{titles[hits[0]]}' 품절")
    await btn.click()
    await page.wait_for_timeout(800)
    return titles[hits[0]]


async def set_qty(page, qty: int) -> None:
    """옵션 선택 직후 수량 1 → '+' 로 qty 까지 올리고 입력값으로 확인(실측: data-qa-name)."""
    box = page.locator('input[data-qa-name="input-product-number"]').first
    plus = page.locator('button[data-qa-name="button-product-number-plus"]').first
    for _ in range(qty + 5):
        cur = int(await box.input_value() or 0)
        if cur == qty:
            return
        if cur > qty:
            raise RuntimeError(f"수량이 이미 {cur}개 — 지시 {qty}개보다 많음")
        await plus.click()
        await page.wait_for_timeout(300)
    raise RuntimeError(f"수량 {qty}개 맞추기 실패")


async def set_today(page, today: bool) -> None:
    """'오늘드림으로 받아보시겠어요?' 체크(#todayDelivery, 기본 해제 — 실측). ⚠️ 체크하는 경로는 미실측."""
    box = page.locator("#todayDelivery")
    if await box.is_checked() != today:
        await box.set_checked(today, force=True)
        await page.wait_for_timeout(500)
    if await box.is_checked() != today:
        raise RuntimeError(f"오늘드림 체크를 {today} 로 못 맞춤")


async def add_to_cart(page, url: str, option: str, qty: int, today: bool) -> str:
    """상품 페이지에서 옵션·수량·배송방식 → '장바구니'. 담은 뒤 뜨는 알림·모달 문구는 출력만(판정은 verify_cart)."""
    await page.goto(url, wait_until="domcontentloaded")
    await wait_cf(page)
    await page.wait_for_timeout(2500)
    name = await pick_option(page, option)
    await set_qty(page, qty)
    await set_today(page, today)
    page.on("dialog", _accept)
    try:
        await page.locator('button[class*="PurchaseBottom_btn-cart"]:visible').first.click()
        await page.wait_for_timeout(2500)
    finally:
        page.remove_listener("dialog", _accept)
    modal = await page.evaluate("""() => [...document.querySelectorAll('.ReactModalPortal, [role=dialog]')]
        .map(m => (m.innerText || '').replace(/\\s+/g, ' ').trim()).filter(Boolean)""")
    print(f"[cart] 담기 클릭: {name} ×{qty} ({'오늘드림' if today else '일반'}) / 화면 메시지: {modal or '-'}")
    return name


async def verify_cart(page, option: str, qty: int, today: bool) -> dict:
    """장바구니를 다시 읽어 지시와 같은지 검증 — '담았습니다' 문구만 믿지 않는다."""
    await open_cart(page)
    items = await read_cart(page)
    ok = len(items) == 1 and option in items[0]["option"] and items[0]["qty"] == qty
    if ok and not today:   # 일반배송 탭의 구매 버튼 건수로 배송방식 확인(실측 문구 '일반배송 구매하기 (1건)')
        ok = await page.locator("button", has_text="일반배송 구매하기 (1건)").count() > 0
    if not ok:
        raise RuntimeError(f"장바구니 검증 실패 — 지시: {option} ×{qty} / 실제: {items}")
    print(f"[cart] 검증 OK: {items[0]['name']} / {items[0]['option']} ×{items[0]['qty']}"
          + ("" if not today else " (⚠️ 오늘드림 배송방식 검증은 미실측)"))
    return items[0]


async def run_cart(no: str, url: str, option: str, qty: int, today: bool) -> int:
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(CDP_URL)
        ctx = browser.contexts[0]
        page = await oy_page(ctx)
        if not await login(ctx, page, no):
            return 1
        try:
            await clear_cart(page)
            await add_to_cart(page, url, option, qty, today)
            await verify_cart(page, option, qty, today)
        except AskUser as e:
            print(f"[cart] {no}번 멈춤 — 사용자 확인 필요: {e}")
            return 3
        return 0


async def add_plain_item(page, url: str, qty: int, today: bool) -> str:
    """옵션 없는 단일 상품 담기. 수량칸은 접힌 하단 패널 안에 있어 안 보이면 먼저 연다(실측: 옵션뷰레이어 열기)."""
    await page.goto(url, wait_until="domcontentloaded")
    await wait_cf(page)
    await page.wait_for_timeout(2500)
    if await page.locator('[class*="OptionSelector_btn-option"]').count():
        raise AskUser(f"옵션이 있는 상품인데 옵션 지시가 없음: {url}")
    box = page.locator('input[data-qa-name="input-product-number"]').first
    if not await box.is_visible():
        await page.locator('button[class*="PurchaseBottom_btn-handler"]:visible').first.click()
        await page.wait_for_timeout(800)
    await set_qty(page, qty)
    if await page.locator("#todayDelivery").count():
        await set_today(page, today)
    elif today:
        raise AskUser(f"오늘드림이 안 되는 상품: {url}")
    name = re.sub(r"\s*\|\s*올리브영\s*$", "", await page.title())
    page.on("dialog", _accept)
    try:
        await page.locator('button[class*="PurchaseBottom_btn-cart"]:visible').first.click()
        await page.wait_for_timeout(2500)
    finally:
        page.remove_listener("dialog", _accept)
    print(f"[cart] 담기 클릭: {name} ×{qty} ({'오늘드림' if today else '일반'})")
    return name


async def verify_plain_items(page, names: list[str], qty: int, today: bool) -> None:
    """장바구니를 다시 읽어 지시한 상품만 각 qty 개인지 검증. 상품명 앞 [행사문구]는 바뀔 수 있어 빼고 비교한다."""
    await open_cart(page)
    items = await read_cart(page)
    keys = [re.sub(r"^\[[^\]]*\]\s*", "", n) for n in names]
    ok = len(items) == len(keys) and all(
        any(k in it["name"] and it["qty"] == qty for it in items) for k in keys)
    if ok and not today:
        ok = await page.locator("button", has_text=f"일반배송 구매하기 ({len(keys)}건)").count() > 0
    if not ok:
        raise RuntimeError(f"장바구니 검증 실패 — 지시: {keys} ×{qty} / 실제: {items}")
    for it in items:
        print(f"[cart] 검증 OK: {it['name']} ×{it['qty']}")


async def run_items(no: str, urls: list[str], qty: int, today: bool) -> int:
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(CDP_URL)
        ctx = browser.contexts[0]
        page = await oy_page(ctx)
        if not await login(ctx, page, no):
            return 1
        try:
            await clear_cart(page)
            names = [await add_plain_item(page, u, qty, today) for u in urls]
            await verify_plain_items(page, names, qty, today)
        except AskUser as e:
            print(f"[cart] {no}번 멈춤 — 사용자 확인 필요: {e}")
            return 3
        return 0


async def run_login(no: str) -> int:
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(CDP_URL)
        ctx = browser.contexts[0]
        page = await oy_page(ctx)
        return 0 if await login(ctx, page, no) else 1


def main() -> int:
    args = sys.argv[1:]
    if len(args) == 2 and args[0] == "login":
        return asyncio.run(run_login(args[1]))
    # 배송방식은 날마다 다르다 — 기본값 없이 '일반'/'오늘드림' 중 하나를 꼭 받는다(추측 금지)
    if len(args) == 6 and args[0] == "cart" and args[5] in ("일반", "오늘드림") and args[4].isdigit():
        return asyncio.run(run_cart(args[1], args[2], args[3], int(args[4]), args[5] == "오늘드림"))
    if len(args) >= 5 and args[0] == "items" and args[3] in ("일반", "오늘드림") and args[2].isdigit():
        return asyncio.run(run_items(args[1], args[4:], int(args[2]), args[3] == "오늘드림"))
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
