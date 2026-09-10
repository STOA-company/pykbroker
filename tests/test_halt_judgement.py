"""A′2-2: a halt is only ever affirmed, never inferred from a code we cannot read.

First round read `iscd_stat_cls_code` alone and treated every value but 58 as
«trading». 관리(51)·위험(52)·경고(53) are not halts, but they are not a clean bill of
health either, and the vendor's own temporary-halt flag was ignored entirely. Both of
those were fail-open: an unreadable state came out as `halted=False`, and `halted=False`
is what an executor sends orders against.
"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pykbroker.brokers.kis import KIS

# _pykis/scope/stock/api/response/price.py:4,16 — both fields, on the same payload.
NORMAL, HALTED = "00", "58"
DESIGNATED = ["51", "52", "53", "54", "55", "57", "59", "  ", "99"]


def _kis(*, status=None, temp=None, **kwargs):
    output = {"stck_prpr": "70000", "stck_sdpr": "69000"}
    if status is not None:
        output["iscd_stat_cls_code"] = status
    if temp is not None:
        output["temp_stop_yn"] = temp
    client, api, scope = Mock(), Mock(), Mock()
    client.key = SimpleNamespace(virtual_account=False)
    client.account.return_value = scope
    api.get_price_domestic.return_value = {"json_data": {"rt_cd": "0", "output": output}}
    return KIS(account="fixture", client=client, api=api, **kwargs)


def _halted(**kwargs):
    return _kis(**kwargs).get_rules("005930").halted


# --- the vendor's own temporary-halt flag counts ---------------------------


@pytest.mark.parametrize("temp", [True, "Y", "y"])
def test_the_temporary_halt_flag_alone_halts(temp):
    assert _halted(temp=temp) is True
    assert _halted(status=NORMAL, temp=temp) is True


@pytest.mark.parametrize("temp", [False, "N"])
def test_a_cleared_temporary_flag_plus_a_normal_code_is_trading(temp):
    assert _halted(status=NORMAL, temp=temp) is False


def test_the_halt_code_still_halts_on_its_own():
    assert _halted(status=HALTED) is True
    assert _halted(status=HALTED, temp=False) is True


# --- codes we have not verified stay unknown -------------------------------


@pytest.mark.parametrize("status", DESIGNATED)
def test_a_code_that_is_not_58_or_00_is_unknown_not_trading(status):
    """Bypass 1: 관리·위험·경고 종목이 «정지 아님»으로 통과하던 자리."""
    assert _halted(status=status) is None
    assert _halted(status=status, temp=False) is None
    assert _kis(status=status).get_rules("005930").tradable is False


def test_an_unreadable_temporary_flag_is_unknown_not_trading():
    assert _halted(status=NORMAL, temp="maybe") is None
    assert _halted(temp="maybe") is None


def test_a_silent_payload_stays_unknown():
    assert _halted() is None


# --- a halt notice is never overwritten ------------------------------------


@pytest.mark.parametrize("status", [NORMAL, *DESIGNATED, None])
def test_a_halt_notice_survives_whatever_the_price_row_says(status):
    """Bypass 2: 통보가 온 정지를 거래소 행이 «정상»이라고 덮어쓰지 못한다."""
    assert _halted(status=status, halt_provider=lambda symbol: True) is True


@pytest.mark.parametrize("status", [HALTED, None])
def test_a_provider_cannot_talk_a_halt_away_and_cannot_invent_certainty(status):
    assert _halted(status=HALTED, halt_provider=lambda symbol: False) is True
    assert _halted(status=status, halt_provider=lambda symbol: "N") is (True if status == HALTED else None)


def test_a_provider_fills_a_designated_code_silence_with_its_own_no():
    assert _halted(status=NORMAL, temp=False, halt_provider=lambda symbol: False) is False
    assert _halted(status="51", halt_provider=lambda symbol: False) is False
