"""주문 검증·최대 결제금액·결제 기록(중복 결제 방지).

결제 기록 상태:
  PENDING  — 결제하기를 누르기 직전에 기록. 이 상태로 남아 있으면(프로그램 종료 등) 결과 불명이다.
  COMPLETE — 주문완료 화면에서 주문번호 확인.
  UNKNOWN  — 결제 인증 이후 완료를 못 봤다. **주문내역을 사람이 확인해 resolve 하기 전에는 그 계정을 다시 결제하지 않는다.**
  NOT_PAID — 결제 인증 전에 멈춤(카드앱 인증 전이라 결제 없음) 또는 사람이 주문내역을 보고 '결제 안 됨'으로 확정.
"""
from __future__ import annotations

import re

import storage as db

OPEN_STATUSES = ("PENDING", "UNKNOWN")
COUNTED_STATUSES = ("PENDING", "UNKNOWN", "COMPLETE")   # 한도 계산엔 결과 불명도 결제된 것으로 센다(보수적)


class OrderRejected(Exception):
    pass


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "").lower()


def normalize_link(link: str) -> str:
    """같은 상품 링크 판정용 — 앞뒤 공백·끝 슬래시·utm 파라미터 차이를 무시."""
    link = (link or "").strip()
    link = re.sub(r"[?&]utm_[^&#]*", "", link)
    return link.rstrip("/?&")


# ---------- 결제 기록 ----------

def open_payment(account_no: str) -> dict | None:
    return db.one(f"SELECT * FROM payments WHERE account_no=? AND status IN {OPEN_STATUSES} AND simulated=0 "
                  "ORDER BY id DESC LIMIT 1", (str(account_no),))


def record_pending(job_id, run_id, account_no, link, amount, card, simulated=False) -> int:
    if not simulated and open_payment(account_no):
        raise OrderRejected(f"{account_no}번 계정에 확인 안 된 결제가 있음 — 주문내역 확인 후 처리")
    return db.execute("INSERT INTO payments(job_id, run_id, account_no, link, amount, card, status, simulated, created_at, day) "
                      "VALUES (?,?,?,?,?,?,'PENDING',?,?,?)",
                      (job_id, run_id, str(account_no), normalize_link(link), int(amount), card, int(simulated),
                       db.now(), db.today()))


def mark(payment_id: int, status: str, order_no: str = "", note: str = "", amount: int | None = None) -> None:
    if status not in ("COMPLETE", "UNKNOWN", "NOT_PAID"):
        raise ValueError(status)
    sets, params = "status=?, order_no=?, note=?, resolved_at=?", [status, order_no, note[:500], db.now()]
    if amount is not None:
        sets += ", amount=?"
        params.append(int(amount))
    db.execute(f"UPDATE payments SET {sets} WHERE id=?", (*params, payment_id))


def resolve(payment_id: int, paid: bool, order_no: str = "", note: str = "") -> dict:
    """사람이 주문내역을 확인한 결과로 결과 불명 결제를 확정한다."""
    p = db.one("SELECT * FROM payments WHERE id=?", (payment_id,))
    if not p or p["status"] not in OPEN_STATUSES:
        raise OrderRejected("확인할 결제가 아님")
    if paid and not re.fullmatch(r"Y\d{10,}", order_no or ""):
        raise OrderRejected("결제됨으로 확정하려면 주문번호(Y…)가 필요")
    mark(payment_id, "COMPLETE" if paid else "NOT_PAID", order_no if paid else "", note or "주문내역 확인(사용자)")
    if p["job_id"]:
        if paid:
            db.execute("UPDATE jobs SET status='completed', order_no=?, error='', updated_at=? WHERE id=?",
                       (order_no, db.now(), p["job_id"]))
        else:
            db.execute("UPDATE jobs SET status='failed', error='결제 안 됨(주문내역 확인)', updated_at=? WHERE id=?",
                       (db.now(), p["job_id"]))
    return db.one("SELECT * FROM payments WHERE id=?", (payment_id,))


def bought_before(account_no: str, link: str) -> bool:
    return db.one("SELECT 1 FROM payments WHERE account_no=? AND link=? AND status='COMPLETE' AND simulated=0",
                  (str(account_no), normalize_link(link))) is not None


# ---------- 금액 집계 ----------

def today_totals(simulated: bool = False) -> dict:
    r = db.one(f"SELECT COUNT(*) n, COALESCE(SUM(amount),0) amt FROM payments WHERE day=? AND simulated=? "
               f"AND status IN {COUNTED_STATUSES}", (db.today(), int(simulated)))
    return {"orders": r["n"], "amount": r["amt"]}


def run_total(run_id: int) -> int:
    return db.one(f"SELECT COALESCE(SUM(amount),0) s FROM payments WHERE run_id=? AND status IN {COUNTED_STATUSES}",
                  (run_id,))["s"]


def precheck(run: dict, account_no: str) -> None:
    """계정 작업을 시작하기 전(로그인 전) 검사. 결과 불명 결제·중복 구매."""
    sim = run["mode"] == "simulate"
    if not sim and open_payment(account_no):
        raise OrderRejected("확인 안 된 결제(결과 불명)가 있음 — 주문내역 확인 후 처리")
    if not sim and not run["allow_repeat"] and bought_before(account_no, run["link"]):
        raise OrderRejected("이미 이 링크 상품을 구매한 계정 — 중복 구매 방지")


ADDRESS_LIKE = re.compile(r"\d.*(로|길|동|읍|면|리|가)\b|(로|길|동|읍|면|리|가)\s*\d")


def check_address(keyword: str, info: dict) -> str:
    """배송지 확인(필수). keyword(수령인 이름 또는 주소 일부)가
    ① 배송지 이름 `addrSummaryName`(예 '1(김건엽)', 실측) 에 있거나
    ② 주소처럼 생긴 주문서 글자(숫자 + 로/길/동…) 에 있으면 통과. 어디에도 없으면 결제하기를 누르지 않는다.
    ⚠️ ② 주소 줄의 정확한 위치(resource-id)는 미실측 — 수령인 이름은 ①로 확실히 확인된다."""
    kw = _norm(keyword)
    if len(kw) < 2:
        raise OrderRejected("배송지 확인값이 없음 (필수)")
    if kw in _norm(info.get("address_name", "")):
        return "배송지 이름"
    for t in info.get("texts", []):
        if ADDRESS_LIKE.search(t) and kw in _norm(t):
            return "주소"
    raise OrderRejected(f"배송지 확인 실패: 주문서 배송지({info.get('address_name') or '?'})에 '{keyword}' 없음")


def validate_order(run: dict, account_no: str, info: dict) -> None:
    """결제하기 직전 주문서 검증. 하나라도 어긋나면 OrderRejected(결제하기를 누르지 않음)."""
    amount = int(info.get("amount") or 0)
    if amount <= 0:
        raise OrderRejected("결제 금액을 읽지 못함")
    if info.get("card_shown") != info.get("card"):
        raise OrderRejected(f"선택 카드 불일치: {info.get('card_shown')}")
    if "삼성" in (info.get("card_shown") or ""):
        raise OrderRejected("삼성카드 사용 금지")
    if info.get("installment") not in ("일시불", None):
        raise OrderRejected("일시불이 아님")
    if not info.get("address_default"):
        raise OrderRejected("기본배송지가 아님")
    check_address(run.get("address_keyword", ""), info)
    if run.get("max_amount_per_account") and amount > run["max_amount_per_account"]:
        raise OrderRejected(f"결제금액 {amount:,}원 > 계정별 최대 {run['max_amount_per_account']:,}원")
    if run.get("max_total_amount") and run_total(run["id"]) + amount > run["max_total_amount"]:
        raise OrderRejected(f"전체 최대 결제금액 {run['max_total_amount']:,}원 초과 예정")
