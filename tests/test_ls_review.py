"""LS review regressions; synthetic hosts/responses only, no broker calls."""

from unittest.mock import Mock

import pytest

from pykbroker.brokers import ls as module
from pykbroker.brokers.ls import LS
from pykbroker.exceptions import BrokerRejectedError, ConfigurationError, InvalidResponseError, SpecUnverifiedError
from pykbroker.factory import create_broker
from pykbroker.safety.rate_limit import RateLimiter
from tests.test_ls_contract import FakeClient, buy, cancel, page

LIVE_HOST = "https://openapi.ls-sec.co.kr:8080"
MOCK_HOST = "https://ls-mock.invalid:8080"  # UNVERIFIED: deliberately fictitious, never a vendor address.


def build(responses=None, **kwargs):
    client = FakeClient(responses or {})
    client.base_url = LIVE_HOST
    return LS(account="fixture", client=client, codes={"exchange": "fixture"}, **kwargs), client


@pytest.mark.parametrize("operation,tr", [("place_order", "CSPAT00601"), ("cancel_order", "CSPAT00801")])
def test_global_switch_cannot_unlock_a_live_binding(monkeypatch, live_safety, operation, tr):
    monkeypatch.setattr(module, "LS_SPEC_VERIFIED", True)
    broker, client = build({tr: [page(tr)]}, safety=live_safety, rate_limiter=Mock(spec=RateLimiter))
    # Give the old adapter enough fictitious codes to reproduce the live-client bypass.
    from tests.test_ls_contract import CODES

    broker._codes.update(CODES)
    with pytest.raises(SpecUnverifiedError):
        getattr(broker, operation)(buy() if operation == "place_order" else cancel())
    assert client.calls == []


def test_locked_candidate_fields_do_not_escape_as_verified_values():
    broker, _ = build({"t0425": [page("t0425")]}, rate_limiter=Mock(spec=RateLimiter))
    from tests.test_ls_contract import CODES

    broker._codes.update(CODES)
    for order in broker.list_orders():
        assert (order.quantity, order.filled, order.fill_price, order.status) == (None, None, None, "unknown")


def test_factory_without_client_is_configuration_error():
    with pytest.raises(ConfigurationError):
        create_broker("ls", account="fixture")


@pytest.mark.parametrize(
    "method,tr",
    [("get_quote", "t1101"), ("get_balance", "CSPAQ12200"), ("get_positions", "t0424"), ("list_orders", "t0425")],
)
def test_unverified_error_code_rejects_even_a_normal_read_block(method, tr):
    """UNVERIFIED success mapping 00000/0000/0; error + valid block cannot succeed (L27)."""
    broker, _ = build({tr: [page(tr, rsp_cd="ERROR")]}, rate_limiter=Mock(spec=RateLimiter))
    from tests.test_ls_contract import CODES

    broker._codes.update(CODES)
    with pytest.raises(BrokerRejectedError):
        getattr(broker, method)(*(["005930"] if method == "get_quote" else []))


@pytest.mark.parametrize(
    "method,tr",
    [("get_quote", "t1101"), ("get_balance", "CSPAQ12200"), ("get_positions", "t0424"), ("list_orders", "t0425")],
)
def test_read_response_without_rsp_cd_is_invalid(method, tr):
    body = page(tr)
    del body["rsp_cd"]
    broker, _ = build({tr: [body]}, rate_limiter=Mock(spec=RateLimiter))
    from tests.test_ls_contract import CODES

    broker._codes.update(CODES)
    with pytest.raises(InvalidResponseError):
        getattr(broker, method)(*(["005930"] if method == "get_quote" else []))


@pytest.fixture
def verified_mock(monkeypatch):
    monkeypatch.setattr(module, "LS_SPEC_VERIFIED", True)
    monkeypatch.setattr(module, "LS_MOCK_BASE_URL", MOCK_HOST)


def mock_broker(responses=None, *, verified=True, **kwargs):
    from tests.test_ls_contract import CODES

    client = FakeClient(responses or {})
    client.base_url = MOCK_HOST
    broker = LS(
        account="fixture",
        client=client,
        virtual=True,
        spec_verified=verified,
        codes=CODES,
        rate_limiter=Mock(spec=RateLimiter),
        **kwargs,
    )
    return broker, client


@pytest.mark.parametrize("operation,tr", [("place_order", "CSPAT00601"), ("cancel_order", "CSPAT00801")])
def test_unverified_error_code_with_order_number_is_unknown(verified_mock, live_safety, operation, tr):
    """UNVERIFIED success mapping; a normal acknowledgement cannot override an error (L27)."""
    broker, client = mock_broker({tr: [page(tr, rsp_cd="ERROR")]}, safety=live_safety)
    with pytest.raises(InvalidResponseError):
        getattr(broker, operation)(buy() if operation == "place_order" else cancel())
    assert len(client.calls) == 1


@pytest.mark.parametrize("operation", ["place_order", "cancel_order"])
def test_verification_unlock_is_local_to_one_mock_instance(verified_mock, live_safety, operation):
    locked, client = mock_broker(verified=False, safety=live_safety)
    allowed, _ = mock_broker(safety=live_safety)
    assert allowed.capabilities().supports_limit_order
    assert not locked.capabilities().supports_limit_order
    with pytest.raises(SpecUnverifiedError):
        getattr(locked, operation)(buy() if operation == "place_order" else cancel())
    assert client.calls == []


def test_global_veto_still_blocks_a_verified_mock_instance(verified_mock, monkeypatch, live_safety):
    broker, client = mock_broker(safety=live_safety)
    monkeypatch.setattr(module, "LS_SPEC_VERIFIED", False)
    assert not broker.capabilities().supports_limit_order
    with pytest.raises(SpecUnverifiedError):
        broker.place_order(buy())
    assert client.calls == []


@pytest.mark.parametrize("virtual", [None, 0, 1, "True", [], {}])
def test_mode_is_boolean_and_never_inferred(virtual):
    with pytest.raises(ConfigurationError, match="boolean"):
        build(virtual=virtual)


@pytest.mark.parametrize("verified", [None, 0, 1, "True", [], {}])
def test_instance_verification_is_boolean(verified):
    with pytest.raises(ConfigurationError, match="boolean"):
        build(spec_verified=verified)


@pytest.mark.parametrize(
    "virtual,host",
    [
        (True, LIVE_HOST),
        (False, MOCK_HOST),
        (False, None),
        (False, 1),
        (False, "http://openapi.ls-sec.co.kr:8080"),
        (False, LIVE_HOST + ".evil.invalid"),
        (False, LIVE_HOST + "/other"),
        (False, "https://openapi.ls-sec.co.kr:8080@evil.invalid"),
    ],
)
def test_constructed_client_host_must_match_the_requested_mode(verified_mock, virtual, host):
    client = FakeClient({})
    client.base_url = host
    with pytest.raises(ConfigurationError):
        LS(account="fixture", client=client, virtual=virtual, spec_verified=True)
    assert client.calls == []


def test_mock_contract_cannot_alias_the_live_address(monkeypatch):
    monkeypatch.setattr(module, "LS_MOCK_BASE_URL", LIVE_HOST)
    with pytest.raises(ConfigurationError, match="UNVERIFIED"):
        build(virtual=True)


@pytest.mark.parametrize("operation", ["place_order", "cancel_order", "get_quote"])
def test_changed_host_is_refused_before_any_post(verified_mock, live_safety, operation):
    broker, client = mock_broker(safety=live_safety)
    client.base_url = LIVE_HOST
    with pytest.raises(ConfigurationError):
        getattr(broker, operation)(
            "005930" if operation == "get_quote" else buy() if operation == "place_order" else cancel()
        )
    assert client.calls == []
    broker._limiter.wait.assert_not_called()


def test_mutating_mode_does_not_relabel_a_live_binding_as_mock(verified_mock, live_safety):
    broker, client = build(safety=live_safety, spec_verified=True)
    broker.virtual = True
    with pytest.raises(SpecUnverifiedError):
        broker.place_order(buy())
    assert client.calls == []


def test_live_instance_verification_does_not_unlock_fields_or_orders(verified_mock, live_safety):
    broker, client = build(
        {"t0425": [page("t0425")]}, spec_verified=True, rate_limiter=Mock(spec=RateLimiter), safety=live_safety
    )
    from tests.test_ls_contract import CODES

    broker._codes.update(CODES)
    for order in broker.list_orders():
        assert not order.virtual
        assert (order.quantity, order.filled, order.fill_price, order.status) == (None, None, None, "unknown")
    with pytest.raises(SpecUnverifiedError):
        broker.place_order(buy())
    assert len(client.calls) == 1


def test_zero_fill_quantity_with_cheprice_keeps_average_price_unknown(verified_mock):
    """L3: UNVERIFIED cheprice must remain None even with an explicit mock verification."""
    body = page("t0425")
    row = body["t0425OutBlock1"][0]
    row.update(cheqty="0", cheprice="69000.25", ordrem="3")
    broker, _ = mock_broker({"t0425": [body]})
    order = broker.list_orders()[0]
    assert order.virtual and order.filled == 0 and order.fill_price is None


@pytest.mark.parametrize("code", ["00000", "0000", "0"])
def test_unverified_legacy_success_codes_with_valid_read_block(code):
    """UNVERIFIED: these codes are candidate legacy values, not official contract evidence."""
    broker, _ = build({"t1101": [page("t1101", rsp_cd=code)]}, rate_limiter=Mock(spec=RateLimiter))
    assert broker.get_quote("005930").last == 70000


def test_locked_candidate_values_are_unmapped_even_when_malformed():
    body = page("t0425")
    body["t0425OutBlock1"][0].update(qty="bad", cheqty=True, cheprice="NaN")
    broker, _ = build({"t0425": [body]}, rate_limiter=Mock(spec=RateLimiter))
    from tests.test_ls_contract import CODES

    broker._codes.update(CODES)
    order = broker.list_orders()[0]
    assert order.quantity is None and order.filled is None and order.fill_price is None


def test_verified_mock_keeps_average_execution_price_unmapped(verified_mock):
    broker, _ = mock_broker({"t0425": [page("t0425")]})
    order = broker.list_orders()[0]
    assert order.quantity == 3 and order.filled == 1 and order.fill_price is None


def test_missing_status_keeps_the_verified_binding_mode(verified_mock):
    from pykbroker.models import Order

    body = page("t0425", t0425OutBlock1=[])
    broker, _ = mock_broker({"t0425": [body]})
    result = broker.status(Order(id="999", symbol="005930", status="accepted", virtual=False))
    assert result.status == "unknown" and result.virtual is True
    assert result.filled is None and result.fill_price is None
