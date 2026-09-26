"""KIS order writes reach the wire once; the vendor's hidden retries stay on reads.

Pykis retries inside the transport: `_execute_request` is wrapped by tenacity (3 attempts on
everything but ReadTimeout and KisTransientError), and `request` loops again on
KisTransientError (`_pykis/client/client.py:208-214, 245-280`). Nothing above the transport
sees those repeats, so an order POST can go out up to three times. The double below keeps that
exact shape so a test that passes here would also pass against the vendor.
"""

import functools
from decimal import Decimal
from types import SimpleNamespace

import pytest

from pykbroker.brokers.kis import KIS
from pykbroker.exceptions import ConfigurationError, InvalidResponseError, TransportError
from pykbroker.models import OrderRequest

from .conftest import load_fixture

ATTEMPTS = 3


class KisTransientError(ValueError):
    """APBK1350 — the vendor's own «retry me» marker."""


def _retry(function):
    """tenacity `@retry(stop_after_attempt(3), retry_if_not_exception_type(transient, ReadTimeout))`."""

    @functools.wraps(function)
    def wrapper(*args, **kwargs):
        for attempt in range(ATTEMPTS):
            try:
                return function(*args, **kwargs)
            except (KisTransientError, TimeoutError):
                raise
            except Exception:
                if attempt == ATTEMPTS - 1:
                    raise

    return wrapper


class VendorTransport:
    """`KisClient`: a scripted wire under the vendor's retry layers."""

    def __init__(self, script):
        self.script, self.sent = list(script), []

    @_retry
    def _execute_request(self, method, path, headers=None, body=None, params=None, response=None):
        self.sent.append((method, path))
        outcome = self.script.pop(0) if self.script else {"rt_cd": "0", "output": {"ODNO": "0001"}}
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def request(self, method, path, headers=None, body=None, params=None, response=None):
        for attempt in range(ATTEMPTS):
            try:
                return self._execute_request(method, path, headers, body, params, response)
            except KisTransientError:
                if attempt == ATTEMPTS - 1:
                    raise

    def posts(self):
        return [entry for entry in self.sent if entry[0] == "post"]


class VendorScope:
    def __init__(self, transport):
        self.client = transport

    def buy(self, **_):
        return self.client.request("post", "/uapi/domestic-stock/v1/trading/order-cash")

    def price(self):
        return self.client.request("get", "/uapi/domestic-stock/v1/quotations/inquire-price")


class VendorApi:
    @staticmethod
    def post_order_domestic(kis_account, **_):
        return kis_account.buy()

    @staticmethod
    def post_order_overseas(kis_account, **_):
        return kis_account.buy()

    @staticmethod
    def get_price_domestic(kis_account, **_):
        return kis_account.price()


def _broker(live_safety, script, market="KR", venue=None):
    transport = VendorTransport(script)
    scope = VendorScope(transport)
    vendor = SimpleNamespace(
        client=transport, key=SimpleNamespace(virtual_account=False), account=lambda _account: scope
    )
    broker = KIS(
        account="fixture-account", client=vendor, api=VendorApi(), safety=live_safety, market=market, venue=venue
    )
    return broker, transport


def _place(broker, request_order):
    return broker.place_order(request_order)


def test_the_double_retries_a_post_when_nothing_intervenes():
    # Control: without the guard the vendor really does send three times.
    transport = VendorTransport([ConnectionError("reset")] * 2)
    assert VendorScope(transport).buy()["rt_cd"] == "0"
    assert len(transport.posts()) == 3


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (ConnectionError("reset"), TransportError),
        (TimeoutError("read timed out"), TransportError),
        (ValueError({"status_code": 500}), InvalidResponseError),
        (KisTransientError({"code": "APBK1350"}), InvalidResponseError),
    ],
    ids=["connection-error", "timeout", "http-5xx", "transient-apbk1350"],
)
def test_kis_order_post_goes_out_once_on_any_failure(live_safety, request_order, failure, expected):
    broker, transport = _broker(live_safety, [failure])
    with pytest.raises(expected):
        _place(broker, request_order)
    assert len(transport.posts()) == 1


def test_kis_order_post_goes_out_once_even_when_the_second_attempt_would_have_worked(live_safety, request_order):
    broker, transport = _broker(live_safety, [ConnectionError("reset")])
    with pytest.raises(TransportError):
        _place(broker, request_order)
    assert len(transport.posts()) == 1 and transport.script == []


def test_kis_us_order_post_goes_out_once(live_safety):
    live_safety.valuation = lambda request: Decimal("1000")
    broker, transport = _broker(live_safety, [ConnectionError("reset")], market="US", venue="NASDAQ")
    order = OrderRequest(
        symbol="AAPL", market="US", venue="NASDAQ", side="buy", quantity=1, price="190.5", client_key="us-once"
    )
    with pytest.raises(TransportError):
        broker.place_order(order)
    assert len(transport.posts()) == 1


def test_a_successful_order_is_still_one_post(live_safety, request_order):
    broker, transport = _broker(live_safety, [])
    assert _place(broker, request_order).id == "0001"
    assert len(transport.posts()) == 1


def test_kis_reads_keep_the_vendor_retries(live_safety):
    quote = load_fixture("kis", "quote")
    broker, transport = _broker(live_safety, [ConnectionError("reset"), KisTransientError("x"), quote])
    broker._api.get_quote_domestic = lambda **_: load_fixture("kis", "book")
    # Transient on the third hop is retried by `request`; the first two by tenacity.
    result = broker.get_rules("005930")
    assert result.symbol == "005930"
    assert [m for m, _ in transport.sent] == ["get"] * 3


def test_reads_after_a_write_are_still_retried(live_safety, request_order):
    broker, transport = _broker(live_safety, [ConnectionError("reset")])
    with pytest.raises(TransportError):
        _place(broker, request_order)
    transport.sent.clear()
    transport.script = [ConnectionError("reset"), load_fixture("kis", "quote")]
    broker.get_rules("005930")
    assert len(transport.sent) == 2


def test_an_unbypassable_transport_refuses_the_write(live_safety, request_order):
    class PlainTransport(VendorTransport):
        def _execute_request(self, *args, **kwargs):  # no __wrapped__: the retry layer cannot be told apart
            return super()._execute_request.__wrapped__(self, *args, **kwargs)

    transport = PlainTransport([])
    scope = VendorScope(transport)
    vendor = SimpleNamespace(
        client=transport, key=SimpleNamespace(virtual_account=False), account=lambda _account: scope
    )
    broker = KIS(account="fixture-account", client=vendor, api=VendorApi(), safety=live_safety)
    with pytest.raises(ConfigurationError):
        broker.place_order(request_order)
    assert transport.sent == []


def _real_tenacity_transport(decorate):
    """The same wire as `VendorTransport`, but with the real tenacity `@retry` under `decorate`."""
    tenacity = pytest.importorskip("tenacity")

    class RealRetryTransport(VendorTransport):
        @decorate
        @tenacity.retry(
            stop=tenacity.stop_after_attempt(ATTEMPTS),
            retry=tenacity.retry_if_not_exception_type((KisTransientError, TimeoutError)),
            reraise=True,
            wait=tenacity.wait_none(),
        )
        def _execute_request(self, method, path, headers=None, body=None, params=None, response=None):
            return VendorTransport._execute_request.__wrapped__(self, method, path, headers, body, params, response)

    return RealRetryTransport


def _broker_on(live_safety, transport):
    scope = VendorScope(transport)
    vendor = SimpleNamespace(
        client=transport, key=SimpleNamespace(virtual_account=False), account=lambda _account: scope
    )
    return KIS(account="fixture-account", client=vendor, api=VendorApi(), safety=live_safety)


def _wraps_layer(function):
    @functools.wraps(function)
    def outer(*args, **kwargs):
        return function(*args, **kwargs)

    return outer


def _plain_layer(function):
    """A layer that keeps the chain walkable (`__wrapped__`) but does not copy tenacity's attributes."""

    def outer(*args, **kwargs):
        return function(*args, **kwargs)

    outer.__wrapped__ = function
    return outer


def test_real_tenacity_is_still_sent_once(live_safety, request_order):
    transport = _real_tenacity_transport(lambda function: function)([ConnectionError("reset")])
    with pytest.raises(TransportError):
        _place(_broker_on(live_safety, transport), request_order)
    assert len(transport.posts()) == 1


@pytest.mark.parametrize("layer", [_wraps_layer, _plain_layer], ids=["functools-wraps", "wrapped-only"])
def test_a_layer_over_the_retry_is_refused_not_sent(live_safety, request_order, layer):
    # `__wrapped__` of the outer layer is still the retrying function: one step down is not the raw call.
    transport = _real_tenacity_transport(layer)([ConnectionError("reset")] * 2)
    with pytest.raises(ConfigurationError):
        _place(_broker_on(live_safety, transport), request_order)
    assert transport.sent == []


def test_a_layer_under_the_retry_is_refused_not_sent(live_safety, request_order):
    tenacity = pytest.importorskip("tenacity")

    class Doubled(VendorTransport):
        @tenacity.retry(stop=tenacity.stop_after_attempt(ATTEMPTS), reraise=True, wait=tenacity.wait_none())
        @tenacity.retry(stop=tenacity.stop_after_attempt(ATTEMPTS), reraise=True, wait=tenacity.wait_none())
        def _execute_request(self, method, path, headers=None, body=None, params=None, response=None):
            self.sent.append((method, path))
            raise ConnectionError("reset")

    transport = Doubled([])
    with pytest.raises(ConfigurationError):
        _place(_broker_on(live_safety, transport), request_order)
    assert transport.sent == []


def test_a_client_without_a_vendor_transport_is_untouched(kis, request_order):
    broker, _, api, _ = kis
    assert broker.place_order(request_order).id
    api.post_order_domestic.assert_called_once()
