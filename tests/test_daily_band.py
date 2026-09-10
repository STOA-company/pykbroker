"""A′2-3: the daily band comes from the venue's own numbers; ±30% is only the fallback.

The vendor puts 상한가/하한가 on the very price row we already read
(`_pykis/scope/stock/api/response/price.py:47,49` stck_mxpr / stck_llam). The first
round ignored them and recomputed 기준가 × ±30% instead. That assumption is KRX cash
equities only: ETF·ETN, 정리매매 and 신규상장 do not share it, so the recomputed band
could be wider than the one the venue will actually accept.
"""

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pykbroker.brokers.kis import KIS
from pykbroker.exceptions import ConfigurationError, InvalidResponseError
from pykbroker.rules.kr import equity_rules

# A band the ±30% rule would never produce from this base — an ETF-shaped one.
VENUE_UP, VENUE_DOWN = "75000", "60000"
DERIVED_UP, DERIVED_DOWN = Decimal(89700), Decimal(48300)


def _kis(**output):
    payload = {"stck_prpr": "70000", "stck_sdpr": "69000", "iscd_stat_cls_code": "00", **output}
    client, api, scope = Mock(), Mock(), Mock()
    client.key = SimpleNamespace(virtual_account=False)
    client.account.return_value = scope
    api.get_price_domestic.return_value = {"json_data": {"rt_cd": "0", "output": payload}}
    return KIS(account="fixture", client=client, api=api)


def test_the_venue_band_is_used_verbatim_even_when_it_differs_from_thirty_percent():
    rules = _kis(stck_mxpr=VENUE_UP, stck_llam=VENUE_DOWN).get_rules("005930")
    assert (rules.limit_up, rules.limit_down) == (Decimal(75000), Decimal(60000))
    assert (rules.limit_up, rules.limit_down) != (DERIVED_UP, DERIVED_DOWN)
    assert rules.limits_source == "venue"


def test_without_a_venue_band_the_thirty_percent_rule_is_used_and_labelled_as_derived():
    rules = _kis().get_rules("005930")
    assert (rules.limit_up, rules.limit_down) == (DERIVED_UP, DERIVED_DOWN)
    assert rules.limits_source == "derived"


def test_a_band_that_is_not_known_at_all_says_so():
    rules = _kis(stck_sdpr="0").get_rules("005930")
    assert rules.limit_up is None and rules.limit_down is None and rules.limits_source is None


def test_a_prev_close_never_widens_a_band_the_venue_declared():
    """Bypass 1: the derived rule cannot be reintroduced alongside the venue's answer."""
    rules = _kis(stck_mxpr=VENUE_UP, stck_llam=VENUE_DOWN, stck_sdpr="69000").get_rules("005930")
    assert rules.limit_up == Decimal(75000) < DERIVED_UP
    assert rules.limit_down == Decimal(60000) > DERIVED_DOWN


@pytest.mark.parametrize("output", [{"stck_mxpr": VENUE_UP}, {"stck_llam": VENUE_DOWN}])
def test_half_a_band_is_refused_rather_than_quietly_completed(output):
    """Bypass 2: one declared side plus one derived side would be a band nobody stated."""
    with pytest.raises((ConfigurationError, InvalidResponseError)):
        _kis(**output).get_rules("005930")


def test_an_inverted_venue_band_is_refused():
    with pytest.raises((ConfigurationError, InvalidResponseError)):
        _kis(stck_mxpr=VENUE_DOWN, stck_llam=VENUE_UP).get_rules("005930")


@pytest.mark.parametrize("bad", ["-1", "nan", "inf", "abc"])
def test_a_venue_band_that_is_not_a_price_is_refused(bad):
    with pytest.raises((ConfigurationError, InvalidResponseError)):
        _kis(stck_mxpr=bad, stck_llam=VENUE_DOWN).get_rules("005930")


def test_equity_rules_take_the_band_directly_and_label_its_source():
    stated = equity_rules("005930", price="70000", limit_up="75000", limit_down="60000")
    assert (stated.limit_up, stated.limit_down, stated.limits_source) == (
        Decimal(75000),
        Decimal(60000),
        "venue",
    )
    derived = equity_rules("005930", price="70000", prev_close="69000")
    assert (derived.limit_up, derived.limit_down, derived.limits_source) == (DERIVED_UP, DERIVED_DOWN, "derived")
