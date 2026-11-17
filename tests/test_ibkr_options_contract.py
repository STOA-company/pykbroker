"""IBKR option reads against the OpenAPI contract mock (no network). scene1..4 = the order's customer scenes."""

from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
import pytest

from pykbroker.brokers.ibkr_options import (
    ContractNotFound,
    ExpiredContract,
    IbkrNotAuthenticated,
    IbkrOptions,
    IbkrRequestError,
    MarketDataNotSubscribed,
    NoOptionSection,
    NoStrikes,
    UnderlyingNotFound,
)
from pykbroker.exceptions import InvalidResponseError, TransportError
from tests.ibkr_contract_mock import _SPEC, IbkrContractMock, schema_min

BASE = "https://localhost:5000/v1/api"
TODAY = date(2024, 2, 20)  # the spec example's months (MAR24...) are still ahead on this date
INFO = _SPEC["components"]["schemas"]["secDefInfoResponse"]


def info_row(conid: int, strike: float, right: str, maturity: str) -> dict:
    """schema-minimum secdef/info row + the scenario's own conid/strike/right/maturity."""
    return {**schema_min(INFO), "conid": conid, "ticker": "IBM", "strike": strike, "right": right, "maturityDate": maturity}


def make(mock: IbkrContractMock, **kw):
    sleeps: list[float] = []
    client = IbkrOptions(
        BASE, transport=mock.transport(), sleep=sleeps.append, today=lambda: TODAY,
        headers={"Cookie": "x-sess=test"}, **kw,
    )  # fmt: skip
    return client, sleeps


CHAIN_ROWS = [info_row(7001, 100, "C", "20240315"), ]
MD = {7001: {"31": "C1.23", "84": "0.00", "86": "1.35", "7633": "27.5", "7308": "0.52"}}


def test_scene1_chain_to_quote_table():
    mock = IbkrContractMock(info_rows=CHAIN_ROWS, market_data=MD)
    ib, _ = make(mock)
    conid = ib.find_underlying("IBM")
    assert conid == 8314  # spec example: IBM / NYSE row with an OPT section
    assert ib.expiry_months(conid)[:2] == ["MAR24", "APR24"]
    strikes = ib.strikes(conid, "MAR24")
    assert strikes["call"] == [Decimal(s) for s in (70, 75, 80, 85, 90, 95, 100)]  # spec example
    (contract,) = ib.option_contracts(conid, "MAR24", 100, "C")
    assert (contract.conid, contract.right, contract.strike, contract.maturity) == (7001, "C", 100, date(2024, 3, 15))
    (q,) = ib.snapshot([contract.conid], ["last", "bid", "ask", "iv_pct", "delta"])
    assert q.bid == Decimal("0.00") and q.bid is not None  # real zero bid is kept
    assert (q.ask, q.iv_pct, q.delta) == (Decimal("1.35"), Decimal("27.5"), Decimal("0.52"))
    assert (q.last, q.last_is_prev_close, q.halted) == (Decimal("1.23"), True, False)  # "C" prefix per OpenAPI
    assert q.has_data and q.availability == "RpB" and q.unverified == ()
    q_params = mock.calls[-1].url.params
    assert q_params["fields"] == "31,84,86,7633,7308,6509" and q_params["conids"] == "7001"


def test_scene1_strikes_request_follows_spec_params():
    mock = IbkrContractMock()
    ib, _ = make(mock)
    ib.find_underlying("IBM")
    ib.strikes(8314, "MAR24")
    p = mock.calls[-1].url.params
    assert (p["conid"], p["sectype"], p["month"], p["exchange"]) == ("8314", "OPT", "MAR24", "SMART")


def test_scene1_option_info_schema_only():
    # spec has no option example for secdef/info: body is the schema minimum with the request echoed.
    ib, _ = make(IbkrContractMock())
    (c,) = ib.option_contracts(8314, "MAR24", 105, "P")
    assert (c.right, c.strike, c.maturity, c.maturity_raw) == ("P", Decimal(105), None, "")


def test_scene2_history_bars_from_spec_example():
    mock = IbkrContractMock(info_rows=CHAIN_ROWS)
    ib, _ = make(mock)
    (c,) = ib.option_contracts(8314, "MAR24", 100, "C")
    bars = ib.history(c.conid, "3m", "1h")
    assert len(bars) == 4  # spec "Last" example has 4 bars
    first = bars[0]
    assert (first.open, first.high, first.low, first.close) == tuple(map(Decimal, ("212.43", "213.94", "210.58", "212.33")))
    assert first.time == datetime.fromtimestamp(1747229400, UTC) and first.volume == Decimal("266616.18")
    p = mock.calls[-1].url.params
    assert (p["conid"], p["period"], p["bar"], p["source"], p["outsideRth"]) == ("7001", "3m", "1h", "Last", "false")


def test_scene2_history_bid_ask_source_uses_its_own_example():
    ib, _ = make(IbkrContractMock())
    bars = ib.history(7001, "1w", "1d", source="Bid_Ask")
    assert bars[0].close == Decimal("212.11") and bars[0].volume is None  # IB "Always returns 0" = no value


def test_scene2_history_midpoint_source():
    ib, _ = make(IbkrContractMock())
    bars = ib.history(7001, "1w", "1d", source="Midpoint")
    assert bars[0].close == Decimal("212.42") and bars[0].volume is None


def test_scene3_snapshot_asks_again_until_data_arrives():
    mock = IbkrContractMock(market_data=MD, empty_first=2)
    ib, sleeps = make(mock)
    (q,) = ib.snapshot([7001])
    assert mock.snapshot_calls == 3 and sleeps == [0.5, 0.5]
    assert q.has_data and q.bid == Decimal("0.00")


def test_scene3_still_empty_returns_none_not_zero():
    mock = IbkrContractMock(market_data=MD, empty_first=99)
    ib, sleeps = make(mock, snapshot_retries=2)
    (q,) = ib.snapshot([7001])
    assert mock.snapshot_calls == 3 and len(sleeps) == 2
    assert not q.has_data and q.bid is None and q.ask is None and q.delta is None and q.last is None


def test_scene3_only_empty_conids_are_asked_again():
    mock = IbkrContractMock(market_data={7001: MD[7001]})
    ib, _ = make(mock, snapshot_retries=1)
    q1, q2 = ib.snapshot([7001, 7002])  # 7002 has no data scenario: stays empty, 7001 is not re-asked
    assert q1.has_data and not q2.has_data
    assert mock.calls[-1].url.params["conids"] == "7002"


def test_scene3_unparsed_text_kept_raw_and_flagged():
    # "1.2K" suffix has no OpenAPI basis -> not turned into a number
    ib, _ = make(IbkrContractMock(market_data={7001: {"88": "1.2K", "84": "0.10"}}))
    (q,) = ib.snapshot([7001], ["bid", "bid_size"])
    assert q.bid_size is None and q.unverified == ("88",) and q.raw["88"] == "1.2K" and q.bid == Decimal("0.10")


def test_scene3_snapshot_limits_from_spec():
    ib, _ = make(IbkrContractMock())
    with pytest.raises(ValueError):
        ib.snapshot(range(1, 102))
    with pytest.raises(ValueError):
        ib.snapshot([1], [])


def test_scene4_past_month_refused_before_any_call():
    mock = IbkrContractMock()
    ib, _ = make(mock)
    with pytest.raises(ExpiredContract):
        ib.strikes(8314, "JAN24")
    with pytest.raises(ExpiredContract):
        ib.option_contracts(8314, "JAN24", 100, "C")
    assert mock.calls == []


def test_scene4_expired_maturity_and_no_history_for_it():
    mock = IbkrContractMock(info_rows=[info_row(7005, 100, "C", "20240216")])
    ib, _ = make(mock)
    with pytest.raises(ExpiredContract):
        ib.option_contracts(8314, "FEB24", 100, "C")  # FEB24 is still the current month, contract already expired
    n = len(mock.calls)
    with pytest.raises(ExpiredContract):
        ib.history(7005, "1m", "1d")
    assert len(mock.calls) == n  # no history request was sent


def test_scene4_missing_strike_contract_and_underlying():
    ib, _ = make(IbkrContractMock(info_rows=[]))
    with pytest.raises(ContractNotFound):
        ib.option_contracts(8314, "MAR24", 9999, "C")
    with pytest.raises(UnderlyingNotFound):
        ib.find_underlying("ZZZZ")
    with pytest.raises(NoOptionSection):
        make(IbkrContractMock(strip_opt_sections=True))[0].find_underlying("IBM")
    with pytest.raises(NoStrikes):
        make(IbkrContractMock(strikes_empty=True))[0].strikes(8314, "MAR24")


def test_scene4_no_market_data_subscription():
    ib, _ = make(IbkrContractMock(availability="NpB"), snapshot_retries=1)
    with pytest.raises(MarketDataNotSubscribed) as err:
        ib.snapshot([7001])
    assert err.value.conids == [7001]


def test_scene4_not_logged_in_bad_request_and_transport_failures():
    with pytest.raises(IbkrNotAuthenticated):
        make(IbkrContractMock(fail={"/strikes": 401}))[0].strikes(8314, "MAR24")
    ib, _ = make(IbkrContractMock())
    with pytest.raises(IbkrRequestError) as err:
        ib.history(7001, "three months", "1h")  # mock answers the documented 400 body
    assert err.value.status == 400 and "Bad Request" in str(err.value)
    with pytest.raises(TransportError):
        make(IbkrContractMock(fail={"/history": 503}))[0].history(7001, "1d", "1h")
    with pytest.raises(TransportError):
        make(IbkrContractMock(fail={"/history": httpx.ConnectError("down")}))[0].history(7001, "1d", "1h")


def test_history_without_data_array_is_invalid_response():
    ib, _ = make(IbkrContractMock(info_rows=[]))
    ib._get = lambda path, params: {}  # shape break at the transport edge, parsing is real
    with pytest.raises(InvalidResponseError):
        ib.history(7001, "1d", "1h")


def test_unsubscribe_all_spec_example():
    ib, _ = make(IbkrContractMock())
    assert ib.unsubscribe_all() is True


def test_mock_itself_rejects_requests_outside_the_spec():
    mock = IbkrContractMock()
    with httpx.Client(transport=mock.transport(), base_url=BASE) as c:
        assert c.get("/iserver/secdef/strikes", params={"conid": "1", "sectype": "OPT"}).status_code == 400  # no month
        assert c.get("/iserver/secdef/strikes", params={"conid": "1", "sectype": "STK", "month": "MAR24"}).status_code == 400
        assert c.get("/iserver/account/orders").status_code == 400  # not in subset: order paths are unreachable
