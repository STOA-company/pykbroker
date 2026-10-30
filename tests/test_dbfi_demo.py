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


DEMO_SUCCESS = {"rsp_cd": "00000", "rsp_msg": "모의투자 조회가 완료되었습니다."}


@pytest.mark.parametrize("reply", [DEMO_SUCCESS, {"rsp_cd": "00000", "rsp_msg": "모의투자 데이터가 없습니다."}])
def test_demo_prefixed_success_reply_is_demo_proof(demo_http, reply):
    """2026-10-30 dev 실측: 공유 호스트가 모의 키의 탐침에 00000 + «모의투자 …완료» 로 답한다(공식 오류표 106·108·109)."""
    demo_http.probe = reply
    DBFI(account="synthetic-account", client=client(), virtual=True)
    assert demo_http.calls == [PROBE]


@pytest.mark.parametrize("reply", [
    {"rsp_cd": "00000", "rsp_msg": "success"}, {},
    {"rsp_cd": "00000", "rsp_msg": "조회가 완료되었습니다."},
    {"rsp_cd": "00000", "rsp_msg": "데이터가 없습니다."},
    {"rsp_cd": "00000", "rsp_msg": "모의투자 조회가 완료되었습니다. "},
    {"rsp_cd": "M100", "rsp_msg": "모의투자 조회가 완료되었습니다."},
    {"rsp_cd": "00000", "rsp_msg": DEMO["rsp_msg"]},
    {"rsp_cd": "M100", "rsp_msg": "모의투자 조회실패"},
    {"rsp_cd": "m100", "rsp_msg": DEMO["rsp_msg"]},
    {"rsp_cd": "OTHER", "rsp_msg": DEMO["rsp_msg"]},
    {"rsp_cd": "", "rsp_msg": DEMO["rsp_msg"]},
    {"rsp_msg": DEMO["rsp_msg"]},
    {"rsp_cd": "M100", "rsp_msg": DEMO["rsp_msg"] + " "},
])
def test_unproven_key_constructor_refuses_with_zero_order_posts(demo_http, reply):
    demo_http.probe = reply
    demo_http.clock.step = 0  # same instant: cooldown (or cache) keeps the second request off the host
    for _ in range(2):
        with pytest.raises(ConfigurationError):
            DBFI(account="synthetic-account", client=client(), virtual=True)
    assert demo_http.calls == [PROBE]


@pytest.mark.parametrize("reply", [[], b"", b"{broken"])
def test_non_dict_or_non_json_reply_refuses_each_request_without_caching(demo_http, reply):
    demo_http.probe = reply  # clock jumps past the cooldown each read: nothing is cached
    for _ in range(2):
        with pytest.raises(ConfigurationError):
            DBFI(account="synthetic-account", client=client(), virtual=True)
    assert demo_http.calls == [PROBE, PROBE]


@pytest.mark.parametrize("status", [302, 400, 403, 500])
def test_bad_http_status_is_not_demo_proof(demo_http, status):
    demo_http.status = status
    for _ in range(2):
        with pytest.raises(ConfigurationError):
            DBFI(account="synthetic-account", client=client(), virtual=True)
    assert demo_http.calls == [PROBE, PROBE]


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


class _VolatileAuth(Auth):
    """Mimics Pydb: fresh random session/UA (and sometimes X-Forwarded-For) per call."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.n = 0

    def get_auth_header(self):
        self.n += 1
        headers = {"Authorization": "Bearer " + self.token, "X-Session-ID": f"sid-{self.n}",
                   "User-Agent": f"ua-{self.n}"}
        if self.n % 5 == 0:
            headers["X-Forwarded-For"] = f"10.0.0.{self.n}"
        return headers


def _buy(broker, key):
    return broker.place(OrderRequest(symbol="005930", side="buy", quantity=1, price=100, client_key=key))


def test_volatile_headers_only_never_add_probes(demo_http, live_safety):
    broker = DBFI(account="synthetic-account", client=client(_VolatileAuth()), virtual=True, safety=live_safety)
    for i in range(10):
        _buy(broker, f"vol-{i}")
    assert demo_http.calls.count(PROBE) == 1
    assert demo_http.calls.count("/api/v1/trading/kr-stock/order") == 10


def test_token_reissue_adds_exactly_one_probe(demo_http, live_safety):
    auth = _VolatileAuth()
    broker = DBFI(account="synthetic-account", client=client(auth), virtual=True, safety=live_safety)
    _buy(broker, "a")
    auth.token = "synthetic-demo-token-2"
    _buy(broker, "b")
    _buy(broker, "c")
    assert demo_http.calls.count(PROBE) == 2


def test_unproven_authorization_is_refused_without_order(demo_http, live_safety):
    auth = _VolatileAuth()
    broker = DBFI(account="synthetic-account", client=client(auth), virtual=True, safety=live_safety)
    demo_http.probe = {"rsp_cd": "00000"}
    auth.token = "synthetic-live-token"
    demo_http.clock.step = 0  # unconfirmed reply: not cached, the cooldown keeps "y" off the host
    with pytest.raises(ConfigurationError):
        _buy(broker, "x")
    assert "/api/v1/trading/kr-stock/order" not in demo_http.calls
    with pytest.raises(ConfigurationError):
        _buy(broker, "y")
    assert demo_http.calls.count(PROBE) == 2


def test_probe_header_sends_volatile_headers_and_proven_authorization(demo_http, live_safety):
    broker = DBFI(account="synthetic-account", client=client(_VolatileAuth()), virtual=True, safety=live_safety)
    _buy(broker, "h")
    sent = {k.lower(): v for k, v in demo_http.headers[-1].items()}
    assert sent["authorization"] == "Bearer synthetic-demo-token"
    assert sent["x-session-id"].startswith("sid-") and sent["user-agent"].startswith("ua-")


def test_transient_probe_timeout_is_not_cached(demo_http, live_safety, monkeypatch):
    auth = _VolatileAuth()
    broker = DBFI(account="synthetic-account", client=client(auth), virtual=True, safety=live_safety)
    auth.token = "synthetic-demo-token-2"
    real = requests.request

    def boom(*a, **k):
        monkeypatch.setattr(requests, "request", real)
        raise requests.Timeout()

    monkeypatch.setattr(requests, "request", boom)
    with pytest.raises(ConfigurationError):
        _buy(broker, "t1")
    assert "/api/v1/trading/kr-stock/order" not in demo_http.calls
    assert _buy(broker, "t2").id == "42"
    assert demo_http.calls.count(PROBE) == 2


def test_probe_429_is_not_cached(demo_http, live_safety):
    auth = _VolatileAuth()
    broker = DBFI(account="synthetic-account", client=client(auth), virtual=True, safety=live_safety)
    auth.token = "synthetic-demo-token-2"
    demo_http.status = 429
    with pytest.raises(ConfigurationError):
        _buy(broker, "r1")
    demo_http.status = 200
    assert _buy(broker, "r2").id == "42"
    assert demo_http.calls.count(PROBE) == 3


def test_confirmed_live_reply_is_cached_as_rejected(demo_http, live_safety):
    auth = _VolatileAuth()
    broker = DBFI(account="synthetic-account", client=client(auth), virtual=True, safety=live_safety)
    auth.token = "synthetic-live-token"
    demo_http.probe = {"rsp_cd": "00000", "rsp_msg": "조회가 완료되었습니다."}
    for key in ("l1", "l2"):
        with pytest.raises(ConfigurationError):
            _buy(broker, key)
    assert demo_http.calls.count(PROBE) == 2


def test_case_colliding_header_keys_are_refused(demo_http, live_safety):
    class Dup(Auth):
        def get_auth_header(self):
            return {"Authorization": "Bearer a", "authorization": "Bearer b"}

    with pytest.raises(ConfigurationError):
        DBFI(account="synthetic-account", client=client(Dup()), virtual=True, safety=live_safety)
    assert demo_http.calls == []


def _fail_then_ask_again(demo_http, probe):
    demo_http.probe = probe
    demo_http.clock.step = 0
    for _ in range(3):
        with pytest.raises(ConfigurationError):
            DBFI(account="synthetic-account", client=client(), virtual=True)
    assert demo_http.calls == [PROBE]  # cooldown: one probe, zero orders


def test_failed_probe_cooldown_refuses_without_probe_then_reprobes_and_orders(demo_http, live_safety):
    from pykbroker.brokers import dbfi_demo
    auth = _VolatileAuth()
    broker = DBFI(account="synthetic-account", client=client(auth), virtual=True, safety=live_safety)
    auth.token = "synthetic-demo-token-2"
    base = demo_http.calls.count(PROBE)
    demo_http.status = 503
    demo_http.clock.step = 0
    for key in ("c1", "c2", "c3"):
        with pytest.raises(ConfigurationError):
            _buy(broker, key)
    assert demo_http.calls.count(PROBE) == base + 1
    assert "/api/v1/trading/kr-stock/order" not in demo_http.calls
    demo_http.status = 200
    demo_http.clock.now += dbfi_demo._PROBE_COOLDOWN_SECONDS - 0.1
    with pytest.raises(ConfigurationError):
        _buy(broker, "c4")
    assert demo_http.calls.count(PROBE) == base + 1
    demo_http.clock.now += 0.2  # cooldown over: re-probe, prove, order goes through
    demo_http.probe = DEMO_SUCCESS
    assert _buy(broker, "c5").id == "42"
    assert demo_http.calls.count(PROBE) == base + 2


@pytest.mark.parametrize("probe", [
    {}, {"rsp_cd": "99999", "rsp_msg": "업무 오류"}, {"rsp_cd": "EGW00123", "rsp_msg": "토큰 만료"},
    {"rsp_cd": "00000", "rsp_msg": "모의투자 조회실패"},
])
def test_unconfirmed_200_reply_is_not_cached_as_rejected(demo_http, probe):
    from pykbroker.brokers import dbfi_demo
    _fail_then_ask_again(demo_http, probe)
    assert not dbfi_demo._rejected
    demo_http.clock.now += dbfi_demo._PROBE_COOLDOWN_SECONDS + 1
    demo_http.probe = DEMO_SUCCESS
    DBFI(account="synthetic-account", client=client(), virtual=True)
    assert demo_http.calls == [PROBE, PROBE]


def test_confirmed_live_reply_is_cached_with_zero_reprobes(demo_http):
    from pykbroker.brokers import dbfi_demo
    demo_http.probe = {"rsp_cd": "00000", "rsp_msg": "조회가 완료되었습니다."}
    demo_http.clock.step = 0
    for _ in range(2):
        with pytest.raises(ConfigurationError):
            DBFI(account="synthetic-account", client=client(), virtual=True)
    demo_http.clock.now += 10_000  # long after the cooldown: still cached, no re-probe
    with pytest.raises(ConfigurationError):
        DBFI(account="synthetic-account", client=client(), virtual=True)
    assert demo_http.calls == [PROBE] and len(dbfi_demo._rejected) == 1
