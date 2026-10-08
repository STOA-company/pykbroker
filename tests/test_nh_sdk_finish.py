"""BS-08 contracts: explicit mock binding, fixed hosts, rules and safe factory."""

import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from pykbroker.brokers.nh import NH, NH_API_BASE_URL, NH_MOCK_BASE_URL
from pykbroker.exceptions import ConfigurationError, InvalidResponseError, SafetyError, TransportError
from pykbroker.factory import create_broker
from pykbroker.models import CancelRequest, OrderRequest
from pykbroker.safety.rate_limit import RateLimiter

QUOTE = "/krstock/quote/v1/currentPrice"
NOW = datetime(2026, 10, 1, 1, 0, tzinfo=UTC)
ROW = {"iem_cd": "005930", "stck_prpr": 70000, "stck_prdy_clpr": 69000, "stck_mxpr": 89700, "stck_llam": 48300}


def make(*, row=None, virtual=False, options=None, factory=False, **kwargs):
    calls, hosts = [], []

    class Fake:
        def __init__(self, *, base_url):
            self.base_url = self.host = base_url
            hosts.append(base_url)

        def post(self, path, *, headers, data):
            calls.append((self.host, path, headers, data))
            if path == "/oauth2/token":
                return 200, {"access_token": "fixture-issued", "expires_in": 86400}, {}
            if path == "/n2/acctinfo":
                body = {"Output_0": [{"acct_no": "fixture-account", "acct_type": "03" if virtual else "01"}]}
            elif path == QUOTE:
                body = {"Output_0": ROW.copy() if row is None else row}
            else:
                body = {"Output_0": {"mkt_orr_no": 123}}
            return 200, {"rsp_cd": "00000", **body}, {}

    options = (
        {"app_key": "fixture-key", "token": "fixture-token", "acct_type": "03" if virtual else "01"}
        if options is None
        else options
    )
    args = dict(
        account="fixture-account",
        virtual=virtual,
        client_factory=Fake,
        clock=lambda: NOW,
        rate_limiter=RateLimiter({"read": 10000}),
        **kwargs,
    )
    if factory:
        broker = create_broker("NH", **args, **({"client_options": options} if options is not False else {}))
    else:
        broker = NH(client_options=options, **args)
    return broker, calls, hosts


@pytest.mark.parametrize(
    "options",
    [
        {"app_key": "fixture-key", "token": "fixture-token"},
        {"app_key": "fixture-key", "token": "fixture-token", "acct_type": None},
        {"app_key": "fixture-key", "token": "fixture-token", "acct_type": "01"},
        {"app_key": "fixture-key", "token": "fixture-token", "acct_type": "02"},
        {"app_key": "fixture-key", "token": "fixture-token", "acct_type": 3},
    ],
)
def test_mock_binding_requires_type_03_before_client_creation(options):
    hosts = []
    with pytest.raises(ConfigurationError, match="account type"):
        NH(
            account="fixture-account",
            virtual=True,
            client_options=options,
            client_factory=lambda **kwargs: hosts.append(kwargs),
        )
    assert hosts == []


@pytest.mark.parametrize("virtual", [False, True])
def test_all_paths_keep_order_host_and_auth_host_separate(virtual, live_safety):
    broker, calls, hosts = make(
        virtual=virtual,
        safety=live_safety,
        allow_live_orders=True,
        options={"app_key": "fixture-key", "app_secret": "fixture-secret", "acct_type": "03" if virtual else "01"},
    )
    assert calls == []
    assert broker.verify_account() == ("03" if virtual else "01")
    broker.get_quote("005930")
    for side in ("buy", "sell"):
        broker.place_order(OrderRequest(symbol="005930", side=side, quantity=1, price=70000, client_key=side))
    broker.cancel_order(CancelRequest(order_id="mkt:123", symbol="005930", quantity=1, client_key="cancel"))
    order_host = NH_MOCK_BASE_URL if virtual else NH_API_BASE_URL
    assert hosts == [order_host, NH_API_BASE_URL]
    assert [(host, path) for host, path, _, _ in calls] == [
        (NH_API_BASE_URL, "/oauth2/token"),
        (NH_API_BASE_URL, "/n2/acctinfo"),
        (NH_API_BASE_URL, QUOTE),
        (order_host, "/krstock/order/v1/cashBuy"),
        (order_host, "/krstock/order/v1/cashSell"),
        (order_host, "/krstock/order/v1/cancel"),
    ]


@pytest.mark.parametrize("halted", [True, False, None, "false", 0])
def test_rules_use_documented_band_and_tristate_halt_provider(halted):
    symbols = []

    def provider(symbol):
        symbols.append(symbol)
        return halted

    broker, calls, _ = make(halt_provider=provider)
    rules = broker.get_rules("A005930", now=NOW, trading_day=lambda day: True)
    assert (rules.symbol, rules.limit_up, rules.limit_down, rules.limits_source) == (
        "005930",
        Decimal(89700),
        Decimal(48300),
        "venue",
    )
    assert rules.halted is (halted if type(halted) is bool else None)
    assert rules.tradable is (halted is False)
    assert (rules.tick, rules.min_notional, rules.session_open) == (100, 70000, True)
    assert symbols == ["005930"]
    assert len(calls) == 1 and calls[0][1] == QUOTE
    assert json.loads(calls[0][3]) == {"Input_0": {"market_cd": "KRX", "iem_cd": "005930"}}


@pytest.mark.parametrize(
    "band",
    [
        {},
        {"stck_mxpr": 89700},
        {"stck_llam": 48300},
        {"stck_mxpr": 0, "stck_llam": 0},
        {"stck_mxpr": "", "stck_llam": None},
    ],
)
def test_missing_or_half_band_stays_unknown_without_derivation(band):
    row = {k: v for k, v in ROW.items() if k not in ("stck_mxpr", "stck_llam")}
    broker, _, _ = make(row={**row, **band})
    rules = broker.get_rules("005930")
    assert (rules.limit_up, rules.limit_down, rules.limits_source) == (None, None, None)
    assert rules.halted is None and rules.session_open is None and rules.tradable is False


@pytest.mark.parametrize(
    "changes",
    [
        {"stck_mxpr": 40000},
        {"stck_llam": 80000},
        {"stck_mxpr": "bad"},
        {"stck_mxpr": -1},
        {"stck_llam": "NaN"},
        {"stck_mxpr": True},
        {"stck_prdy_clpr": 100000},
        {"stck_prpr": 0},
        {"iem_cd": "000660"},
    ],
)
def test_rules_reject_malformed_or_inconsistent_quote(changes):
    broker, calls, _ = make(row={**ROW, **changes})
    with pytest.raises(InvalidResponseError):
        broker.get_rules("005930")
    assert len(calls) == 1


def test_halt_provider_failure_never_becomes_trading():
    def broken(symbol):
        raise RuntimeError("fixture-internal")

    broker, _, _ = make(halt_provider=broken)
    with pytest.raises(TransportError, match="Vendor call failed"):
        broker.get_rules("005930")


@pytest.mark.parametrize("venue", ["KRX", "NXT", "UNT"])
def test_rules_request_the_bound_venue(venue):
    broker, calls, _ = make(venue=venue)
    assert broker.get_rules("005930").limit_up == 89700
    assert json.loads(calls[0][3])["Input_0"]["market_cd"] == venue


@pytest.mark.parametrize("virtual", [False, True])
def test_factory_registration_keeps_mutations_disabled_by_default(virtual):
    broker, calls, _ = make(factory=True, virtual=virtual)
    assert isinstance(broker, NH)
    with pytest.raises(SafetyError):
        broker.place_order(OrderRequest(symbol="005930", side="buy", quantity=1, price=70000, client_key="off"))
    with pytest.raises(SafetyError):
        broker.cancel_order(CancelRequest(order_id="mkt:123", symbol="005930", quantity=1, client_key="off"))
    assert calls == []


def test_factory_loads_nh_options_with_injected_http_client(monkeypatch):
    options = {"app_key": "fixture-key", "token": "fixture-token", "acct_type": "01"}
    monkeypatch.setenv("PYKBROKER_CLIENT_OPTIONS", json.dumps(options))
    broker, calls, _ = make(factory=True, options=False)
    assert broker.get_quote("005930").last == 70000
    assert len(calls) == 1

@pytest.mark.parametrize("previous,expected", [("190.25", Decimal("190.25")), (None, None), ("0", None)])
def test_us_previous_close_uses_native_historical_price_without_last_fallback(previous, expected):
    broker = NH.__new__(NH)
    broker._auth_client = object()
    broker._read = lambda *args, **kwargs: {"Output_0": {
        "iem_cd": "AAPL", "trdprc": "200", "best_bid1": "199", "best_ask1": "201",
        "hst_trdprc": previous, "base_prc": "999"}}
    quote = broker._us_quote("AAPL")
    assert quote.last == Decimal("200") and quote.prev_close == expected
