"""v1.2.17 이음매. 가짜 SDK·기록 픽스처만. 네트워크는 conftest 가 막는다."""

from decimal import Decimal

import pytest

from pykbroker import Toss
from pykbroker.exceptions import DryRunError, InvalidResponseError, SpecUnverifiedError
from pykbroker.safety.rate_limit import RateLimiter


class _SDK:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.calls = []

    async def get_prices(self, symbols):
        self.calls.append("get_prices")
        return [{"symbol": symbols[0], "lastPrice": "70000"}]

    async def get_orderbook(self, symbol):
        self.calls.append("get_orderbook")
        return {"asks": [{"price": "70100"}], "bids": [{"price": "69900"}]}

    async def get_buying_power(self, currency):
        self.calls.append(("get_buying_power", currency))
        return {"currency": currency, "cashBuyingPower": "5000"}

    async def get_positions(self):
        return {"marketValue": {"amount": {"krw": "0", "usd": "0"}}, "items": []}

    async def get_sellable_quantity(self, symbol):
        return {"sellableQuantity": "0"}

    async def list_open_orders(self, symbol=None):
        return {
            "hasNext": False,
            "orders": [
                {
                    "orderId": "42",
                    "symbol": "005930",
                    "quantity": "2",
                    "execution": {"filledQuantity": "1"},
                    "status": "PARTIAL_FILLED",
                }
            ],
        }

    async def submit_order(self, **kwargs):
        self.calls.append(("submit_order", kwargs))
        return {"submitted": True, "order_id": "live-order", "status": "SUBMITTED"}

    async def cancel_order(self, order_id):
        return {"status": "CANCELLED"}

    async def aclose(self):
        return None


def _broker(factory, **kwargs):
    made = {}

    def build(**options):
        sdk = factory(**options)
        made["sdk"] = sdk
        made["options"] = options
        return sdk

    broker = Toss(account="fixture-v1217", account_seq=7, sdk_factory=build, **kwargs)
    return broker, made


def test_available_cash_rejects_non_krw_buying_power():
    """KRW 매수가능이 아니면 잔고를 만들지 않는다. USD 금액을 KRW 로 읽으면 실패."""

    class Wrong(_SDK):
        async def get_buying_power(self, currency):
            return {"currency": "USD", "cashBuyingPower": "10"}

    broker, _made = _broker(Wrong)
    with pytest.raises(InvalidResponseError):
        broker.available_cash()


def test_available_cash_is_krw_buying_power():
    broker, made = _broker(_SDK)
    assert broker.available_cash() == Decimal("5000")
    assert ("get_buying_power", "KRW") in made["sdk"].calls


def test_partial_filled_is_the_v1217_status_name():
    broker, _made = _broker(_SDK)
    assert broker.list_orders()[0].status == "partial"


def test_default_dry_run_does_not_submit_when_the_safety_gate_is_open(live_safety, request_order):
    """뮤테이션: 드라이런을 무시하고 submit_order 를 호출하면 이 테스트가 빨강."""
    broker, made = _broker(_SDK, safety=live_safety)
    with pytest.raises(DryRunError):
        broker.place_order(request_order)
    submitted = made["sdk"].calls if "sdk" in made else []
    assert not any(isinstance(item, tuple) and item[0] == "submit_order" for item in submitted)


def test_token_store_is_forwarded_to_the_sdk():
    store = object()
    broker, made = _broker(_SDK, token_store=store)
    broker.available_cash()
    assert made["options"]["token_store"] is store
    assert made["options"]["account_seq"] == 7


def test_market_data_calls_are_spaced_by_the_token_bucket():
    class _Clock:
        def __init__(self) -> None:
            self.now = 0.0
            self.sleeps: list[float] = []

        def __call__(self) -> float:
            return self.now

        def sleep(self, seconds: float) -> None:
            self.sleeps.append(seconds)
            self.now += seconds

    clock = _Clock()
    limiter = RateLimiter({"MARKET_DATA": 15}, clock=clock, sleeper=clock.sleep)
    broker, _made = _broker(_SDK, rate_limiter=limiter)
    broker.get_quote("005930")
    assert clock.sleeps == [pytest.approx(1 / 15)]


def test_capabilities_do_not_claim_an_unverified_market():
    """뮤테이션: markets 에 US 를 넣거나 US 생성을 허용하면 빨강."""
    broker, _made = _broker(_SDK)
    assert broker.capabilities().markets == ("KR",)
    with pytest.raises(SpecUnverifiedError):
        Toss(account="fixture-v1217", account_seq=7, market="US", sdk_factory=_SDK)


def test_rates_follow_sdk_token_buckets():
    toss_openapi = pytest.importorskip("toss_openapi")
    from toss_openapi.transport.retry import TOKEN_BUCKETS

    from pykbroker.brokers.toss import toss_call_rates

    rates = toss_call_rates()
    assert rates["MARKET_DATA"] == float(TOKEN_BUCKETS["MARKET_DATA"].tps)
    assert rates["ORDER"] == float(TOKEN_BUCKETS["ORDER"].tps)
    assert rates["ORDER_INFO"] == float(TOKEN_BUCKETS["ORDER_INFO"].tps)
    assert rates["ACCOUNT"] == float(TOKEN_BUCKETS["ACCOUNT"].tps)
    assert toss_openapi is not None


class _MemStore:
    def __init__(self):
        self.value = None
        self.sets = 0

    def get(self):
        return self.value

    def set(self, token, expires_at):
        self.sets += 1
        self.value = (token, expires_at)


def test_recorded_fixture_attaches_account_header_and_reuses_token_store():
    """실 SDK + httpx 목. 토큰 엔드포인트는 저장소가 비어 있을 때 한 번만."""
    httpx = pytest.importorskip("httpx")
    pytest.importorskip("toss_openapi")
    import toss_openapi.transport.http as http_mod

    posts = []
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if path == "/oauth2/token":
            posts.append(path)
            return httpx.Response(
                200,
                json={"access_token": "fixture-token", "token_type": "Bearer", "expires_in": 3600},
            )
        if path == "/api/v1/buying-power":
            assert request.headers["X-Tossinvest-Account"] == "7"
            return httpx.Response(200, json={"result": {"currency": "KRW", "cashBuyingPower": "5000"}})
        if path == "/api/v1/holdings":
            assert request.headers["X-Tossinvest-Account"] == "7"
            return httpx.Response(
                200,
                json={"result": {"marketValue": {"amount": {"krw": "0", "usd": "0"}}, "items": []}},
            )
        return httpx.Response(500, json={"error": {"code": "unexpected", "message": path}})

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        kwargs = dict(kwargs)
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(http_mod.httpx, "AsyncClient", client_factory)
    store = _MemStore()
    broker = None
    try:
        broker = Toss(
            account="fixture-recorded",
            account_seq=7,
            credentials_options={"api_key": "client-fixture", "secret_key": "secret-fixture"},
            token_store=store,
            sdk_dry_run=True,
        )
        assert broker.available_cash() == Decimal("5000")
        assert broker.available_cash() == Decimal("5000")
    finally:
        monkeypatch.undo()
        if broker is not None:
            broker.close()
    assert posts == ["/oauth2/token"]
    assert store.sets == 1
    assert store.get()[0] == "fixture-token"
