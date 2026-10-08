"""Pinned official KB samples; transport is fake and conftest forbids sockets."""

import copy
import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from pykbroker.brokers import kb as module
from pykbroker.brokers.kb import KB, KB_API_BASE_URL
from pykbroker.exceptions import (
    ConfigurationError,
    InvalidResponseError,
    SafetyError,
    SpecUnverifiedError,
    TransportError,
)
from pykbroker.models import CancelRequest, OrderRequest

FIXTURES = Path(__file__).parent / "fixtures" / "kb" / "official"


def sample(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


class FakeClient:
    base_url = KB_API_BASE_URL

    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []

    def post(self, path, *, headers, body):
        self.calls.append((path, headers, copy.deepcopy(body)))
        return copy.deepcopy(self.responses[path])


@pytest.fixture(autouse=True)
def no_wait(monkeypatch):
    class Limiter:
        def wait(self, kind):
            assert kind == "request"

    monkeypatch.setattr(module, "credential_rate_limiter", lambda *args: Limiter())


def broker(client, market="KR", **kwargs):
    return KB(account="fixture-account", bound_account="fixture-account", app_key="k" * 36,
              token="fixture-token", client=client, market=market,
              venue="NASDAQ" if market == "US" else None, **kwargs)


def order(market="KR", **changes):
    fields = dict(symbol="005930" if market == "KR" else "TSLA", market=market,
                  venue=None if market == "KR" else "NASDAQ", side="buy", quantity=1,
                  price=Decimal("70000") if market == "KR" else Decimal("1"), client_key="fixture-order")
    return OrderRequest(**(fields | changes))


def cancel(market="KR", **changes):
    fields = dict(symbol="005930" if market == "KR" else "TSLA", market=market,
                  venue=None if market == "KR" else "NASDAQ", order_id="9000000001", quantity=1,
                  client_key="fixture-cancel")
    return CancelRequest(**(fields | changes))


def test_kr_official_unsettled_holding_is_not_lost():
    client = FakeClient({"/api/v1/ssqm2952": sample("SSQM2952")["output"]})
    b = broker(client)
    positions = b.get_positions()
    assert len(positions) == 1
    position = positions[0]
    assert (position.symbol, position.quantity, position.sellable) == ("005930", 1, 1)
    assert position.market_value == Decimal("426500")
    assert b.available_qty("A005930") == 1
    assert [c[0] for c in client.calls] == ["/api/v1/ssqm2952"] * 2


def test_kr_settlement_quantity_is_not_added_to_held_quantity():
    response = sample("SSQM2952")["output"]
    response["dataBody"]["Record1"][0].update(hld_q="3", ec_q="4", ordr_psbl_q="4", nstmt_b_q="1")
    assert broker(FakeClient({"/api/v1/ssqm2952": response})).get_positions()[0].quantity == 4


@pytest.mark.parametrize("field,value", [("grid_cnt1", "2"), ("tl_data_cnt", "2")])
def test_kr_truncated_official_holding_fails_closed(field, value):
    response = sample("SSQM2952")["output"]
    response["dataBody"][field] = value
    with pytest.raises((InvalidResponseError, SpecUnverifiedError)):
        broker(FakeClient({"/api/v1/ssqm2952": response})).get_positions()


def test_us_funding_official_fixed_array_padding_is_not_counted():
    funding = sample("SKQM3350")["output"]
    # SPQM2226 official export is corrupt; this explicit synthetic empty page
    # isolates funding parsing without treating the corrupt example as empty.
    holdings = sample("SPQM2226")["output"]
    holdings["dataBody"].update(grid_cnt2="0", tl_scrts_exch_val_amt="0", Record1=[])
    client = FakeClient({"/api/v1/skqm3350": funding, "/api/v1/spqm2226": holdings})
    balance = broker(client, "US").get_balance()
    assert balance.currency == "USD" and balance.cash_available == 0 and balance.position_value == 0
    assert len(KB._funding_rows(funding["dataBody"])) == 6


@pytest.mark.parametrize("mutation", ["missing_currency_row", "positive_padding", "missing_padding_field",
                                       "count_mismatch", "duplicate_usd"])
def test_us_funding_corruption_fails_closed(mutation):
    response = sample("SKQM3350")["output"]
    data = response["dataBody"]
    if mutation == "missing_currency_row":
        del data["Record1"][0]
    elif mutation == "positive_padding":
        data["Record1"][-1]["fcrncy_tfnd"] = "1"
    elif mutation == "missing_padding_field":
        del data["Record1"][-1]["fcrncy_tfnd"]
    elif mutation == "count_mismatch":
        data["grid_cnt"] = "7"
    else:
        data["Record1"][0]["crncy_cd"] = "USD"
    with pytest.raises(InvalidResponseError):
        broker(FakeClient({"/api/v1/skqm3350": response}), "US").get_balance()


def test_us_official_displaced_holding_is_not_silently_an_empty_account():
    response = sample("SPQM2226")["output"]
    assert response["dataBody"]["grid_cnt2"] == "0001"
    assert response["dataBody"]["Record2"] == []
    assert Decimal(response["dataBody"]["tl_scrts_exch_val_amt"]) > 0
    with pytest.raises(SpecUnverifiedError, match="incomplete"):
        broker(FakeClient({"/api/v1/spqm2226": response}), "US").get_positions()


def test_us_pages_require_complete_rows_and_follow_next_key():
    # XLSX Record2 fields, attached to a mutated official envelope.
    response = sample("SPQM2226")["output"]
    response["dataBody"].update(grid_cnt2="1", Record1=[], Record2=[{
        "crncy_clsf_nm": "USD", "is_cd": "TSLA", "is_nm": "fixture-stock",
        "frgn_hld_q_p6": "1.000000", "frgn_ordr_psbl_q_p6": "1.000000",
        "now_prc_p4": "1.2500", "byng_avr_prc_p4": "1.0000",
    }], nxt_key="fixture-next")
    last = copy.deepcopy(response)
    last["dataBody"].update(grid_cnt2="0", Record2=[], nxt_key=" ")

    class Pages(FakeClient):
        def post(self, path, *, headers, body):
            super().post(path, headers=headers, body=body)
            return copy.deepcopy(response if len(self.calls) == 1 else last)

    client = Pages({"/api/v1/spqm2226": response})
    result = broker(client, "US").get_positions()
    assert len(result) == 1 and result[0].market_value == Decimal("1.25")
    assert [c[2]["dataBody"]["nxt_key"] for c in client.calls] == ["", "fixture-next"]
    # No invented cn_f enum: the official table provides none.
    assert all("cn_f" not in c[2]["dataBody"] for c in client.calls)
    response["dataBody"]["grid_cnt2"] = "2"
    with pytest.raises(InvalidResponseError):
        broker(Pages({"/api/v1/spqm2226": response}), "US").get_positions()


@pytest.mark.parametrize("price", ["360000", "360000.0", "3.6E+5"])
def test_kr_price_matches_official_integer_input(price):
    official = sample("SSAM1802")
    client = FakeClient({"/api/v1/ssam1802": official["output"]})
    b = broker(client, writes_enabled=True)
    request = order(symbol="A005930", quantity=5, price=Decimal(price))
    b._validate_order(request)
    result = b._place_order(request)
    # The official order example selects NXT (N); this adapter remains KRX (K),
    # both documented by sheet26:16. Only the price format is under test here.
    assert client.calls[0][2]["dataBody"] == official["input"]["dataBody"] | {"sor_ordr_ccd": "K"}
    assert result.status == "accepted"


@pytest.mark.parametrize("price,expected", [("1", "1"), ("1.000000", "1"), ("150.250000", "150.25"),
                                            ("0.0001", "0.0001"), ("395.3100", "395.31")])
def test_us_price_uses_at_most_official_p4(price, expected):
    official = sample("SKAM2101")
    client = FakeClient({"/api/v1/skam2101": official["output"]})
    b = broker(client, "US", writes_enabled=True, is_integrated=True)
    request = order("US", price=Decimal(price))
    b._validate_order(request)
    b._place_order(request)
    assert client.calls[0][2]["dataBody"] == official["input"]["dataBody"] | {"frgn_ordr_prc_p4": expected}


@pytest.mark.parametrize("market,price", [("KR", "1.1"), ("US", "1.00001"), ("US", "1E-1000000"),
                                        ("KR", "1E1000000"), ("US", "12345678901.1234")])
def test_invalid_price_is_refused_before_transport(market, price):
    client = FakeClient()
    b = broker(client, market, writes_enabled=True, is_integrated=True)
    with pytest.raises(ConfigurationError):
        b.place_order(order(market, price=Decimal(price)))
    assert client.calls == []


@pytest.mark.parametrize("market,price", [("KR", "0"), ("US", "-1"), ("US", "NaN")])
def test_nonpositive_and_nonfinite_price_is_refused_even_for_constructed_models(market, price):
    client = FakeClient()
    b = broker(client, market, writes_enabled=True, is_integrated=True)
    request = order(market).model_copy(update={"price": Decimal(price)})
    with pytest.raises(ValidationError):
        b.place_order(request)
    assert client.calls == []


def test_kr_cancel_matches_successful_official_example_despite_xlsx_description():
    official = sample("SSAM1806")
    client = FakeClient({"/api/v1/ssam1806": official["output"]})
    b = broker(client, writes_enabled=True)
    request = cancel(symbol="A005930", order_id=official["input"]["dataBody"]["orgn_ordr_no"])
    b._validate_order(request)
    result = b._cancel_order(request)
    assert client.calls[0][2]["dataBody"] == official["input"]["dataBody"]
    assert result.id == request.order_id and result.status == "unknown"


def test_us_cancel_normalizes_ledger_p6_to_p4_without_rounding():
    ledger = sample("SPQM2204")["output"]
    first = ledger["dataBody"]["Record1"][0]
    acknowledgement = sample("SKAM2101")["output"]  # SKAM2102 official response is a failure.
    client = FakeClient({"/api/v1/spqm2204": ledger, "/api/v1/skam2102": acknowledgement})
    b = broker(client, "US", writes_enabled=True, order_date=date(2026, 6, 22))
    request = cancel("US", order_id=first["ordr_no"])
    result = b._cancel_order(request)
    assert client.calls[-1][2]["dataBody"] == {
        "crct_cncl_clsf": "2", "is_cd": "TSLA", "orgn_ordr_no": first["ordr_no"],
        "frgn_ordr_prc_p4": "1",
    }
    assert result.status == "unknown"


@pytest.mark.parametrize("change", ["partial", "excess_precision", "missing_original_price"])
def test_us_cancel_refuses_unverified_quantity_or_original_price(change):
    ledger = sample("SPQM2204")["output"]
    first = ledger["dataBody"]["Record1"][0]
    if change == "partial":
        first.update(frgn_ordr_q_p6="2", nccls_q_p6="2")
    elif change == "excess_precision":
        first["frgn_ordr_prc_p6"] = "1.00001"
    else:
        del first["frgn_ordr_prc_p6"]
    client = FakeClient({"/api/v1/spqm2204": ledger})
    b = broker(client, "US", writes_enabled=True)
    with pytest.raises((SpecUnverifiedError, InvalidResponseError, ConfigurationError)):
        b._cancel_order(cancel("US", order_id=first["ordr_no"]))
    assert [c[0] for c in client.calls] == ["/api/v1/spqm2204"]


def test_us_ledger_date_preserves_new_york_until_timezone_is_documented(monkeypatch):
    class Clock:
        @staticmethod
        def now(zone):
            assert str(zone) == "America/New_York"
            return datetime(2026, 6, 22, 12, 0)

    monkeypatch.setattr(module, "datetime", Clock)
    client = FakeClient({"/api/v1/spqm2204": sample("SPQM2204")["output"]})
    b = broker(client, "US")
    b.list_orders()
    assert client.calls[0][2]["dataBody"]["strt_ordr_dt"] == sample("SPQM2204")["input"]["dataBody"]["strt_ordr_dt"]
    assert broker(client, "US", order_date=date(2026, 6, 23))._ledger_date() == "20260623"


@pytest.mark.parametrize("tr", ["ssam1801", "ssam1802", "ssam1806", "skam2101", "skam2102"])
def test_every_official_order_field_limit_is_checked_at_final_transport_boundary(tr):
    official = sample(tr.upper())
    for name, maximum in module._ORDER_LENGTHS[tr].items():
        client = FakeClient({"/api/v1/" + tr: official["output"]})
        b = broker(client)
        body = dict(official["input"]["dataBody"])
        if tr == "ssam1801":
            body["sor_ordr_ccd"] = "K"  # Adapter always sends this optional field explicitly.
        body[name] = "X" * maximum
        # Some official outputs are business failures; receipt by the fake
        # transport proves the length boundary passed, not broker acceptance.
        if official["output"]["dataHeader"]["processFlag"] == "B":
            with pytest.raises(InvalidResponseError, match="business result"):
                b._response(tr, body)
        else:
            b._response(tr, body)
        assert client.calls[0][2]["dataBody"][name] == "X" * maximum
        body[name] += "X"
        with pytest.raises(ConfigurationError):
            b._response(tr, body)
        assert len(client.calls) == 1


@pytest.mark.parametrize("market,changes", [("KR", {"quantity": 10**10}), ("KR", {"price": Decimal("1E9")}),
                                          ("US", {"quantity": 10**18}), ("US", {"symbol": "X" * 13})])
def test_order_limits_refused_before_safety_or_transport(market, changes):
    client = FakeClient()
    with pytest.raises(ConfigurationError):
        broker(client, market, writes_enabled=True, is_integrated=True).place_order(order(market, **changes))
    assert client.calls == []


@pytest.mark.parametrize("market,changes", [("KR", {"quantity": 10**10}), ("KR", {"order_id": "1" * 11}),
                                          ("US", {"symbol": "X" * 13}), ("US", {"order_id": "1" * 11})])
def test_cancel_limits_refused_before_ledger_or_transport(market, changes):
    client = FakeClient()
    with pytest.raises(ConfigurationError):
        broker(client, market, writes_enabled=True).cancel_order(cancel(market, **changes))
    assert client.calls == []


def test_issue_token_matches_official_envelope_and_returns_only_issued_fields():
    official = sample("token")
    client = FakeClient({"/oauth2/token": official["output"]})
    result = KB.issue_token(app_key="k" * 36, secret_key="s" * 32, client_factory=lambda: client)
    assert result == official["output"]["dataBody"]
    path, headers, body = client.calls[0]
    assert path == "/oauth2/token" and body == official["input"]
    assert headers == {"Content-Type": "application/json"}
    b = broker(FakeClient())
    assert not hasattr(b, "_secret_key") and "appSecret" not in result


@pytest.mark.parametrize("changes", [{"app_key": "k" * 37}, {"secret_key": "s" * 33},
                                     {"secret_key": "\nsecret"}, {"secret_key": " "}, {"virtual": True}])
def test_token_invalid_inputs_never_call_transport(changes):
    client = FakeClient()
    with pytest.raises(ConfigurationError):
        KB.issue_token(**({"app_key": "k" * 36, "secret_key": "s" * 32} | changes), client_factory=lambda: client)
    assert client.calls == []


@pytest.mark.parametrize("changes", [{"access_token": ""}, {"expires_in": True}, {"expires_in": 0},
                                     {"expires_in": "86400"}, {"token_type": "unknown"}, {"access_token": "t" * 441}])
def test_token_invalid_official_envelope_is_rejected_without_raw_values(changes):
    response = sample("token")["output"]
    response["dataBody"].update(changes)
    client = FakeClient({"/oauth2/token": response})
    with pytest.raises(InvalidResponseError, match="KB token response is invalid"):
        KB.issue_token(app_key="k" * 36, secret_key="s" * 32, client_factory=lambda: client)


def test_token_error_does_not_echo_secret_or_response(capsys):
    class Failure(FakeClient):
        def post(self, *args, **kwargs):
            raise RuntimeError("s" * 32)

    with pytest.raises(TransportError) as error:
        KB.issue_token(app_key="k" * 36, secret_key="s" * 32, client_factory=Failure)
    assert "s" * 32 not in str(error.value)
    assert error.value.__suppress_context__
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


def test_wrong_token_host_and_failed_business_envelope_are_rejected():
    client = FakeClient({"/oauth2/token": sample("token")["output"]})
    client.base_url = "https://untrusted.invalid"
    with pytest.raises(ConfigurationError):
        KB.issue_token(app_key="k" * 36, secret_key="s" * 32, client_factory=lambda: client)
    assert client.calls == []
    client.base_url = KB_API_BASE_URL
    client.responses["/oauth2/token"]["dataHeader"]["processFlag"] = "B"
    with pytest.raises(InvalidResponseError):
        KB.issue_token(app_key="k" * 36, secret_key="s" * 32, client_factory=lambda: client)


def test_write_defaults_and_safety_still_block_without_issuing_a_token():
    client = FakeClient()
    b = broker(client)
    for method, request in [(b.place_order, order()), (b.cancel_order, cancel())]:
        with pytest.raises(SpecUnverifiedError):
            method(request)
    assert client.calls == []
    b = broker(client, writes_enabled=True)
    with pytest.raises(SafetyError):
        b.place_order(order())
    assert client.calls == []


def test_public_order_and_cancel_use_existing_safety_with_fake_transport(live_safety):
    accepted = sample("SSAM1802")["output"]
    accepted["dataBody"].update(ordr_q="1", ordr_uprc="70000")
    cancelled = sample("SSAM1806")["output"]
    cancelled["dataBody"]["orgn_ordr_no"] = accepted["dataBody"]["ordr_no"]
    client = FakeClient({"/api/v1/ssam1802": accepted, "/api/v1/ssam1806": cancelled})
    b = broker(client, writes_enabled=True, safety=live_safety)
    placed = b.place_order(order(price=Decimal("70000.0")))
    result = b.cancel_order(cancel(order_id=placed.id))
    assert [c[0] for c in client.calls] == ["/api/v1/ssam1802", "/api/v1/ssam1806"]
    assert client.calls[0][2]["dataBody"]["ordr_uprc"] == "70000"
    assert client.calls[1][2]["dataBody"]["crct_clsf"] == "1"
    assert result.status == "unknown"


def test_official_fixtures_match_recorded_sanitized_hashes():
    source = (FIXTURES / "SOURCE.md").read_text()
    for file in FIXTURES.glob("*.json"):
        assert hashlib.sha256(file.read_bytes()).hexdigest() in source
