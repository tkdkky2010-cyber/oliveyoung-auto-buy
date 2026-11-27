import pytest

from state_machine import Control, InvalidTransition, Paused, S, StateMachine, Stopped


def test_happy_path():
    sm = StateMachine()
    for s in (S.DEVICE_CONNECTED, S.LOGIN_REQUIRED, S.LOGIN_IN_PROGRESS, S.AUTHENTICATION_REQUIRED, S.LOGIN_IN_PROGRESS,
              S.LOGIN_SUCCESS, S.PRODUCT_LOADING, S.PRODUCT_FOUND, S.OPTION_SELECTED, S.ORDER_PREPARATION,
              S.ORDER_VALIDATION, S.PAYMENT_PENDING, S.PAYMENT_AUTH_REQUIRED, S.PAYMENT_PENDING, S.ORDER_COMPLETE):
        sm.to(s)
    assert sm.state == S.ORDER_COMPLETE


def test_no_skipping_steps():
    sm = StateMachine()
    with pytest.raises(InvalidTransition):
        sm.to(S.PAYMENT_PENDING)
    sm.to(S.DEVICE_CONNECTED)
    with pytest.raises(InvalidTransition):
        sm.to(S.PRODUCT_FOUND)


def test_payment_zone_cannot_pause_or_fail_silently():
    sm = StateMachine(state=S.ORDER_VALIDATION)
    sm.to(S.PAYMENT_PENDING)
    for bad in (S.PAUSED, S.STOPPED, S.FAILED, S.IDLE):
        with pytest.raises(InvalidTransition):
            sm.to(bad)
    sm.to(S.PAYMENT_RESULT_UNKNOWN)
    with pytest.raises(InvalidTransition):
        sm.to(S.IDLE)   # 결과 불명에서 재시도 금지


def test_control():
    c = Control()
    c.checkpoint()
    c.pause.set()
    with pytest.raises(Paused):
        c.checkpoint()
    c.stop.set()
    with pytest.raises(Stopped):
        c.checkpoint()
