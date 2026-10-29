"""Positive demo proof, cache/session binding and actual loopback HTTP requests."""
from types import SimpleNamespace

import pytest
import requests

from pykbroker.brokers.dbfi import DBFI
from pykbroker.exceptions import ConfigurationError, NotSupportedError
from pykbroker.models import CancelRequest, OrderRequest

from .dbfi_demo_http import Auth, DEMO, PROBE, demo_http  # noqa: F401


def client(auth=None):
    auth = auth or Auth()
    class Client:
        def __init__(self):
            self.domestic = SimpleNamespace(auth=auth)
            self.overseas = SimpleNamespace(auth=auth)

        def send(self, path, **kwargs):
            return requests.request("POST", "https://openapi.dbsec.co.kr:8443" + path,
                                    json=kwargs, headers=self.domestic.auth.get_auth_header()).json()

        def buy(self, **kwargs):
            return self.send("/api/v1/trading/kr-stock/order", **kwargs)

        sell = buy

        def cancel(self, **kwargs):
            return self.send("/api/v1/trading/kr-stock/order-cancel", **kwargs)

        def get_transaction_history(self, **kwargs):
            return self.send("/api/v1/trading/kr-stock/inquiry/transaction-history", **kwargs)

    return Client()


@pytest.mark.parametrize("reply", [
    {"rsp_cd": "00000", "rsp_msg": "success"}, {}, [],
    {"rsp_cd": "M100", "rsp_msg": "모의투자 조회실패"},
    {"rsp_cd": "m100", "rsp_msg": DEMO["rsp_msg"]},
    {"rsp_cd": "OTHER", "rsp_msg": DEMO["rsp_msg"]},
    {"rsp_cd": "", "rsp_msg": DEMO["rsp_msg"]},
    {"rsp_msg": DEMO["rsp_msg"]},
    {"rsp_cd": "M100", "rsp_msg": DEMO["rsp_msg"] + " "}, b"", b"{broken",
])
def test_unproven_key_constructor_refuses_with_zero_order_posts(demo_http, reply):
    demo_http.probe = reply
    for _ in range(2):
        with pytest.raises(ConfigurationError):
            DBFI(account="synthetic-account", client=client(), virtual=True)
    assert demo_http.calls == [PROBE]


@pytest.mark.parametrize("status", [302, 400, 403, 500])
def test_bad_http_status_is_not_demo_proof(demo_http, status):
    demo_http.status = status
    with pytest.raises(ConfigurationError):
        DBFI(account="synthetic-account", client=client(), virtual=True)
    assert demo_http.calls == [PROBE]


@pytest.mark.parametrize("error", [requests.ConnectionError, requests.ConnectTimeout, requests.ReadTimeout])
def test_transport_failure_is_single_attempt_and_never_echoes_secrets(demo_http, monkeypatch, error, caplog):
    sent = []
    def fail(**kwargs):
        sent.append(kwargs["url"])
        raise error("synthetic-demo-secret synthetic-demo-token")
    monkeypatch.setattr(requests, "request", fail)
    with pytest.raises(ConfigurationError) as caught:
        DBFI(account="synthetic-account", client=client(), virtual=True)
    assert len(sent) == 1 and sent[0].endswith(PROBE)
    assert "synthetic-demo" not in str(caught.value) + caplog.text
    assert demo_http.calls == []


def test_verified_key_cached_but_order_and_cancel_reach_http(demo_http, live_safety):
    vendor = client()
    brokers = [DBFI(account="synthetic-account", client=vendor, virtual=True, safety=live_safety) for _ in range(3)]
    assert demo_http.calls == [PROBE]
    broker = brokers[0]
    assert broker.demo_verified and broker.capabilities().virtual
    assert broker.capabilities().supports_virtual_account and not broker.capabilities().supports_nxt
    order = broker.place(OrderRequest(symbol="005930", side="buy", quantity=1, price=70000, client_key="demo-order"))
    assert order.id == "42" and order.virtual
    cancellation = broker.cancel(CancelRequest(order_id="42", symbol="005930", quantity=1, client_key="demo-cancel"))
    assert cancellation.virtual
    assert demo_http.calls == [PROBE, "/api/v1/trading/kr-stock/order", "/api/v1/trading/kr-stock/order-cancel"]
    assert demo_http.bodies[0] == {"In": {"QryTp": "2"}}
    assert demo_http.headers[0]["cont_yn"] == "N"


@pytest.mark.parametrize("virtual", [False, True])
def test_history_and_status_preserve_account_mode(demo_http, virtual):
    demo_http.responses["/api/v1/trading/kr-stock/inquiry/transaction-history"] = {
        "rsp_cd": "00000", "Out1": [{"OrdNo": 42, "IsuNo": "A005930",
        "OrdQty": 1, "AllExecQty": 1, "MrcAbleQty": 0, "AvrExecPrc": "70000"}]}
    broker = DBFI(account="synthetic-account", client=client(), virtual=virtual)
    order = broker.list_orders()[0]
    assert order.virtual is virtual
    assert broker.status(order).virtual is virtual


@pytest.mark.parametrize("change", ["key", "secret", "token", "account"])
def test_cache_never_crosses_credentials_token_or_account(demo_http, change):
    DBFI(account="synthetic-account", client=client(), virtual=True)
    auth = Auth()
    account = "synthetic-account"
    if change == "key": auth.appkey += "-changed"
    if change == "secret": auth.appsecretkey += "-changed"
    if change == "token": auth.token += "-changed"
    if change == "account": account += "-changed"
    demo_http.probe = {"rsp_cd": "00000"}
    with pytest.raises(ConfigurationError):
        DBFI(account=account, client=client(auth), virtual=True)
    assert demo_http.calls == [PROBE, PROBE]


def test_changed_session_after_creation_is_reproved_before_order(demo_http, live_safety):
    vendor = client()
    broker = DBFI(account="synthetic-account", client=vendor, virtual=True, safety=live_safety)
    vendor.domestic.auth.token = "synthetic-live-token"
    demo_http.probe = {"rsp_cd": "00000"}
    with pytest.raises(ConfigurationError):
        broker.place(OrderRequest(symbol="005930", side="buy", quantity=1, price=70000, client_key="demo-changed"))
    assert demo_http.calls == [PROBE, PROBE]


def test_injected_client_keys_must_match_options(demo_http):
    with pytest.raises(ConfigurationError):
        DBFI(account="synthetic-account", client=client(), virtual=True,
             client_options={"app_key": "synthetic-other-key"})
    assert demo_http.calls == []


def test_overseas_session_cannot_differ_from_probe(demo_http):
    vendor = client()
    vendor.overseas.auth = Auth(token="synthetic-other-token")
    with pytest.raises(ConfigurationError):
        DBFI(account="synthetic-account", client=vendor, virtual=True, market="US", venue="NASDAQ")
    assert demo_http.calls == []


@pytest.mark.parametrize("operation", ["order", "cancel"])
def test_demo_nxt_refuses_without_order_http(demo_http, live_safety, operation):
    broker = DBFI(account="synthetic-account", client=client(), virtual=True, safety=live_safety)
    with pytest.raises(NotSupportedError):
        if operation == "order":
            broker.place(OrderRequest(symbol="005930", side="buy", quantity=1, price=70000, venue="NXT", client_key="demo-nxt"))
        else:
            broker.cancel(CancelRequest(order_id="42", symbol="005930", quantity=1, venue="NXT", client_key="demo-nxt-cancel"))
    assert demo_http.calls == [PROBE]


def test_live_path_does_not_probe_or_claim_virtual(demo_http, live_safety):
    broker = DBFI(account="synthetic-account", client=client(), safety=live_safety)
    order = broker.place(OrderRequest(symbol="005930", side="buy", quantity=1, price=70000, client_key="live-unchanged"))
    assert not order.virtual
    assert demo_http.calls == ["/api/v1/trading/kr-stock/order"]
    assert not broker.virtual and not broker.demo_verified
