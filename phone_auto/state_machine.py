"""계정별 구매 작업의 화면 상태 기계.

상태는 정해진 순서로만 넘어간다(TRANSITIONS). 허용되지 않은 전환은 InvalidTransition — 예상과 다른 흐름에서
다음 단계로 넘어가지 않게 한다. 전환할 때마다 jobs.state 에 저장되므로 꺼졌다 켜져도 어디서 멈췄는지 남는다.

일시정지·중지는 '안전 지점(checkpoint)'에서만 걸린다. 결제하기를 누른 뒤(PAYMENT_PENDING 이후)는
결과가 확정될 때까지 멈추지 않는다 — 중간에 끊으면 결제 결과를 모르게 된다.
"""
from __future__ import annotations

import enum
import threading


class State(str, enum.Enum):
    IDLE = "IDLE"
    DEVICE_CONNECTED = "DEVICE_CONNECTED"
    LOGIN_REQUIRED = "LOGIN_REQUIRED"
    LOGIN_IN_PROGRESS = "LOGIN_IN_PROGRESS"
    AUTHENTICATION_REQUIRED = "AUTHENTICATION_REQUIRED"
    LOGIN_SUCCESS = "LOGIN_SUCCESS"
    PRODUCT_LOADING = "PRODUCT_LOADING"
    PRODUCT_FOUND = "PRODUCT_FOUND"
    OPTION_SELECTED = "OPTION_SELECTED"
    ORDER_PREPARATION = "ORDER_PREPARATION"
    ORDER_VALIDATION = "ORDER_VALIDATION"
    PAYMENT_PENDING = "PAYMENT_PENDING"
    PAYMENT_AUTH_REQUIRED = "PAYMENT_AUTH_REQUIRED"
    PAYMENT_RESULT_UNKNOWN = "PAYMENT_RESULT_UNKNOWN"
    ORDER_COMPLETE = "ORDER_COMPLETE"
    PAUSED = "PAUSED"
    FAILED = "FAILED"
    STOPPED = "STOPPED"


S = State
LABELS = {
    S.IDLE: "대기", S.DEVICE_CONNECTED: "기기 연결 확인", S.LOGIN_REQUIRED: "로그인 필요",
    S.LOGIN_IN_PROGRESS: "로그인 중", S.AUTHENTICATION_REQUIRED: "사람 인증 대기", S.LOGIN_SUCCESS: "로그인 완료",
    S.PRODUCT_LOADING: "상품 여는 중", S.PRODUCT_FOUND: "상품 확인", S.OPTION_SELECTED: "옵션·수량 선택",
    S.ORDER_PREPARATION: "주문서 준비", S.ORDER_VALIDATION: "주문 검증", S.PAYMENT_PENDING: "결제 진행",
    S.PAYMENT_AUTH_REQUIRED: "결제 인증 대기", S.PAYMENT_RESULT_UNKNOWN: "결제 결과 불명(주문내역 확인 필요)",
    S.ORDER_COMPLETE: "주문 완료", S.PAUSED: "일시정지", S.FAILED: "실패", S.STOPPED: "중지",
}

TERMINAL = {S.ORDER_COMPLETE, S.FAILED, S.STOPPED, S.PAYMENT_RESULT_UNKNOWN}
# 결제하기를 누른 뒤의 상태 — 여기서는 일시정지·중지·재시도하지 않는다
PAYMENT_ZONE = {S.PAYMENT_PENDING, S.PAYMENT_AUTH_REQUIRED}

_FLOW = [S.IDLE, S.DEVICE_CONNECTED, S.LOGIN_REQUIRED, S.LOGIN_IN_PROGRESS, S.LOGIN_SUCCESS, S.PRODUCT_LOADING,
         S.PRODUCT_FOUND, S.OPTION_SELECTED, S.ORDER_PREPARATION, S.ORDER_VALIDATION, S.PAYMENT_PENDING]
TRANSITIONS: dict[State, set[State]] = {s: set() for s in State}
for a, b in zip(_FLOW, _FLOW[1:]):
    TRANSITIONS[a].add(b)
TRANSITIONS[S.LOGIN_IN_PROGRESS].add(S.AUTHENTICATION_REQUIRED)
TRANSITIONS[S.AUTHENTICATION_REQUIRED].add(S.LOGIN_IN_PROGRESS)
TRANSITIONS[S.PAYMENT_PENDING] |= {S.PAYMENT_AUTH_REQUIRED, S.ORDER_COMPLETE, S.PAYMENT_RESULT_UNKNOWN}
TRANSITIONS[S.PAYMENT_AUTH_REQUIRED] |= {S.PAYMENT_PENDING, S.ORDER_COMPLETE, S.PAYMENT_RESULT_UNKNOWN}
for s in _FLOW + [S.AUTHENTICATION_REQUIRED]:
    if s not in PAYMENT_ZONE:
        TRANSITIONS[s] |= {S.FAILED, S.STOPPED, S.PAUSED}
# 일시정지에서 재개하면 처음(기기 확인)부터 다시 — 앱 화면이 그새 바뀌었을 수 있다
TRANSITIONS[S.PAUSED] |= {S.IDLE, S.STOPPED}
# 결제 전 단계의 실패·중지에서만 재시도(처음부터) 허용
TRANSITIONS[S.FAILED].add(S.IDLE)
TRANSITIONS[S.STOPPED].add(S.IDLE)


class InvalidTransition(RuntimeError):
    pass


class Paused(Exception):
    pass


class Stopped(Exception):
    pass


class Control:
    """작업 한 개(run)의 일시정지·중지 신호. 작업 스레드는 checkpoint() 에서만 반응한다."""

    def __init__(self):
        self.pause = threading.Event()
        self.stop = threading.Event()
        self.resume = threading.Event()

    def checkpoint(self) -> None:
        if self.stop.is_set():
            raise Stopped()
        if self.pause.is_set():
            raise Paused()


class StateMachine:
    def __init__(self, on_change=None, state: State = S.IDLE):
        self.state = State(state)
        self.on_change = on_change   # callable(old, new, detail)
        self.history: list[tuple[State, str]] = [(self.state, "")]

    def can(self, new: State) -> bool:
        return State(new) in TRANSITIONS[self.state]

    def to(self, new: State, detail: str = "") -> None:
        new = State(new)
        if new == self.state:
            return
        if not self.can(new):
            raise InvalidTransition(f"{self.state.value} → {new.value} 전환 불가")
        old, self.state = self.state, new
        self.history.append((new, detail))
        if self.on_change:
            self.on_change(old, new, detail)

    @property
    def in_payment_zone(self) -> bool:
        return self.state in PAYMENT_ZONE
