"""A′-2: rules(symbol) answers tick, session, lot, halt and daily price limits — or says it does not know."""

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pykbroker.brokers.kis import KIS
from pykbroker.exceptions import ConfigurationError
from pykbroker.rules.kr import equity_rules, price_limits, regular_session_open, session_state

OPEN_KST = datetime(2026, 9, 10, 1, 0, tzinfo=UTC)  # 10:00 KST
CLOSED_KST = datetime(2026, 9, 10, 8, 0, tzinfo=UTC)  # 17:00 KST


def _quote(halt_code=None, prev_close="69000"):
    output = {"stck_prpr": "70000", "stck_sdpr": prev_close}
    if halt_code is not None:
        output["iscd_stat_cls_code"] = halt_code
    return {"json_data": {"rt_cd": "0", "output": output}}


def _kis(halt_code=None, prev_close="69000", **kwargs):
    client, api, scope = Mock(), Mock(), Mock()
    client.key = SimpleNamespace(virtual_account=False)
    client.account.return_value = scope
    api.get_price_domestic.return_value = _quote(halt_code, prev_close)
    api.get_quote_domestic.return_value = {"json_data": {"rt_cd": "0", "output1": {"bidp1": "0", "askp1": "0"}}}
    return KIS(account="fixture", client=client, api=api, **kwargs)


# --- pure KRX rules -------------------------------------------------------


@pytest.mark.parametrize(
    "base,up,down",
    [("70000", 91000, 49000), ("69000", 89700, 48300), ("3333", 4330, 2335), ("1000", 1300, 700)],
)
def test_daily_price_limits_round_inward_to_the_tick(base, up, down):
    assert price_limits(base) == (Decimal(up), Decimal(down))


@pytest.mark.parametrize("bad", ["0", "-1", "nan", "inf"])
def test_price_limits_refuse_a_base_that_is_not_a_price(bad):
    with pytest.raises(ConfigurationError):
        price_limits(bad)


def test_session_state_is_tri_state_and_unknown_is_not_closed():
    assert session_state(OPEN_KST, lambda day: True) is True
    assert session_state(CLOSED_KST, lambda day: True) is False
    assert session_state(OPEN_KST, lambda day: False) is False
    assert session_state(OPEN_KST, None) is None
    assert session_state(OPEN_KST.replace(tzinfo=None), lambda day: True) is None


def test_regular_session_open_stays_fail_closed():
    assert regular_session_open(OPEN_KST, lambda day: True) is True
    assert regular_session_open(OPEN_KST, None) is False
    assert regular_session_open(OPEN_KST.replace(tzinfo=None), lambda day: True) is False


def test_equity_rules_answer_all_five_questions():
    rules = equity_rules(
        "005930", price="70000", prev_close="69000", halted=False, now=OPEN_KST, trading_day=lambda day: True
    )
    assert rules.tick == Decimal(100)
    assert rules.lot == 1
    assert rules.min_notional == Decimal(70000)
    assert (rules.limit_up, rules.limit_down) == (Decimal(89700), Decimal(48300))
    assert rules.session_open is True and rules.halted is False
    assert rules.tradable is True


@pytest.mark.parametrize(
    "kwargs",
    [
        {"halted": None},  # halt status unknown
        {"halted": True},  # halted
        {"now": None},  # session unknown
        {"trading_day": None},  # no calendar
    ],
)
def test_unknown_or_halted_is_never_tradable(kwargs):
    base = dict(price="70000", prev_close="69000", halted=False, now=OPEN_KST, trading_day=lambda day: True)
    assert equity_rules("005930", **{**base, **kwargs}).tradable is False


def test_rules_without_a_price_admit_they_do_not_know():
    rules = equity_rules("005930")
    assert rules.tick is None and rules.limit_up is None and rules.min_notional is None
    assert rules.session_open is None and rules.halted is None and rules.tradable is False


# --- broker wiring --------------------------------------------------------


def test_kis_rules_read_tick_limits_and_halt_from_the_vendor():
    rules = _kis(halt_code="00").get_rules("005930", now=OPEN_KST, trading_day=lambda day: True)
    assert rules.tick == Decimal(100)
    assert (rules.limit_up, rules.limit_down) == (Decimal(89700), Decimal(48300))
    assert rules.halted is False and rules.session_open is True and rules.tradable is True


def test_kis_reports_a_halted_stock_as_halted():
    rules = _kis(halt_code="58").get_rules("005930", now=OPEN_KST, trading_day=lambda day: True)
    assert rules.halted is True and rules.tradable is False


def test_a_silent_vendor_leaves_the_halt_unknown_not_false():
    rules = _kis().get_rules("005930", now=OPEN_KST, trading_day=lambda day: True)
    assert rules.halted is None and rules.tradable is False


def test_halt_provider_only_fills_a_silence():
    """Bypass 1: a caller-supplied provider cannot talk a vendor-declared halt away."""
    assert _kis(halt_code="58", halt_provider=lambda symbol: False).get_rules("005930").halted is True
    assert _kis(halt_provider=lambda symbol: True).get_rules("005930").halted is True
    assert _kis(halt_provider=lambda symbol: False).get_rules("005930").halted is False


def test_an_unreadable_halt_provider_answer_is_still_unknown():
    """Bypass 2: a truthy string is not a halt answer."""
    assert _kis(halt_provider=lambda symbol: "N").get_rules("005930").halted is None
    assert _kis(halt_provider=lambda symbol: None).get_rules("005930").halted is None


def test_kis_us_rules_state_the_us_tick_and_never_a_krx_band():
    client, api = Mock(), Mock()
    client.key = SimpleNamespace(virtual_account=False)
    api.get_price_overseas_detail.return_value = {
        "rt_cd": "0", "output": {"last": "100", "e_ordyn": "매매 가능"}
    }
    broker = KIS(account="fixture", market="US", venue="NASDAQ", client=client, api=api)
    rules = broker.get_rules("AAPL")
    assert rules.market == "US" and rules.currency == "USD" and rules.timezone == "America/New_York"
    assert rules.tick is None and rules.limit_up is None and rules.limit_down is None
    assert rules.lot == 1 and rules.halted is False and rules.tradable is False
    api.get_price_overseas_detail.assert_called_once_with(
        kis_account=client.account.return_value, excd="NAS", code="AAPL"
    )


def test_dbfi_answers_the_same_contract_and_admits_its_silence(dbfi):
    broker, _, _ = dbfi
    rules = broker.get_rules("005930", now=OPEN_KST, trading_day=lambda day: True)
    assert rules.tick == Decimal(100) and rules.min_notional == Decimal(70000) and rules.lot == 1
    # The DB Securities price payload carries no prev-close or halt field we have verified.
    assert rules.limit_up is None and rules.halted is None and rules.tradable is False
