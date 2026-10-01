"""PR #14 review regressions: fakes only, with one loopback-only redirect test."""

import socket
import threading
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import TCPServer

import pytest

from pykbroker.brokers.nh import NH, NH_API_BASE_URL, NH_MOCK_BASE_URL, _NhRestClient
from pykbroker.exceptions import ConfigurationError, InvalidResponseError, SafetyError, TransportError
from pykbroker.factory import create_broker
from pykbroker.mcp.server import create_server
from pykbroker.models import CancelRequest, OrderRequest
from pykbroker.safety.rate_limit import RateLimiter
from tests.test_nh_read_contract import BALANCE, KEY, NOW, SECRET, make, reply
from tests.test_nh_sdk_finish import make as make_sdk

QUOTE = "/krstock/quote/v1/currentPrice"
SELLABLE = "/krstock/inquiry/v1/sellableQuantity"
_CONNECT = socket.socket.connect


@pytest.mark.parametrize("virtual,client_index", [(False, 0), (False, 1), (True, 0), (True, 1)])
@pytest.mark.parametrize("bad_url", [None, NH_API_BASE_URL + "/", "https://example.invalid", NH_MOCK_BASE_URL])
def test_injected_transport_must_disclose_exact_host(virtual, client_index, bad_url):
    expected = NH_MOCK_BASE_URL if virtual and client_index == 0 else NH_API_BASE_URL
    if bad_url == expected:
        bad_url = NH_API_BASE_URL
    made, posts = [], []

    class IgnoringFactory:
        def __init__(self, *, base_url):
            self.base_url = bad_url if len(made) == client_index else base_url
            made.append(self)

        def post(self, *args, **kwargs):
            posts.append(args)
            return reply({"Output_0": {"mkt_orr_no": 123}, "rsp_cd": "00000"})

    with pytest.raises(ConfigurationError, match="host"):
        NH(
            account="fixture",
            virtual=virtual,
            client_options={
                "app_key": KEY,
                "token": "fixture-token",
                "acct_type": "03" if virtual else "01",
            },
            client_factory=IgnoringFactory,
        )
    assert posts == []


@pytest.mark.parametrize("operation", ["get_quote", "get_rules"])
def test_mock_account_quote_and_rules_use_live_host(operation):
    broker, calls, _ = make_sdk(virtual=True)
    getattr(broker, operation)("005930")
    assert [(host, path) for host, path, _, _ in calls] == [(NH_API_BASE_URL, QUOTE)]


@pytest.mark.parametrize("operation", ["place_order", "place", "cancel_order", "cancel"])
@pytest.mark.parametrize("secret_only", [False, True])
def test_factory_live_safety_cannot_unlock_nh(operation, secret_only, live_safety):
    options = {"app_key": KEY, "acct_type": "01", "app_secret" if secret_only else "token": "fixture-value"}
    broker, calls, _ = make_sdk(factory=True, options=options, safety=live_safety)
    request = (
        OrderRequest(symbol="005930", side="buy", quantity=1, price=70000, client_key="lock")
        if operation.startswith("place")
        else CancelRequest(symbol="005930", order_id="mkt:123", quantity=1, client_key="lock")
    )
    with pytest.raises(SafetyError, match="NH live orders"):
        getattr(broker, operation)(request)
    assert calls == []  # Includes token issuance: no POST of any kind.


@pytest.mark.parametrize("tool", ["place_order", "cancel_order"])
async def test_mcp_live_limits_cannot_unlock_nh(tmp_path, monkeypatch, tool):
    path = tmp_path / "limits.toml"
    path.write_text("allow_orders=true\ndry_run=false\nper_order_krw_cap=100000\ndaily_krw_cap=200000\n")
    calls = []

    class Fake:
        def __init__(self, *, base_url):
            self.base_url = base_url

        def post(self, *args, **kwargs):
            calls.append(args)
            return reply({"Output_0": {"mkt_orr_no": 123}, "rsp_cd": "00000"})

    def factory(name, *, safety):
        assert name == "nh"
        return create_broker(
            name,
            account="fixture",
            safety=safety,
            client_factory=Fake,
            client_options={"app_key": KEY, "app_secret": SECRET, "acct_type": "01"},
        )

    monkeypatch.setenv("PYKBROKER_BROKER", "nh")
    monkeypatch.setattr("pykbroker.mcp.server.create_broker", factory)
    server = create_server(limits_path=path)
    args = dict(symbol="005930", quantity=1, client_key="mcp-lock")
    args.update(dict(side="buy", price="70000") if tool == "place_order" else dict(order_id="mkt:123"))
    with pytest.raises(Exception, match="NH live orders"):
        await server.call_tool(tool, args)
    assert calls == []


@pytest.mark.parametrize("token_kind", ["stored", "issued"])
def test_older_valid_token_cannot_overwrite_newer_cache(token_kind):
    fresh = {"token": "fixture-fresh", "expires_at": (NOW + timedelta(days=2)).isoformat()}
    cache = {KEY: fresh.copy()}
    options = {"app_key": KEY, "app_secret": SECRET, "acct_type": "01"}
    if token_kind == "stored":
        options.update(token="fixture-older", expires_at=(NOW + timedelta(hours=1)).isoformat())
    broker, calls = make(
        {"/oauth2/token": reply({"access_token": "fixture-issued", "expires_in": 86400})},
        options=options,
        cache=cache,
        rate_limiter=RateLimiter({"read": 10000}),
    )
    if token_kind == "issued":
        broker.issue_token()
    assert cache[KEY] == fresh
    assert len(calls) == (token_kind == "issued")


def test_valid_local_token_yields_to_newer_shared_token():
    cache = {}
    broker, calls = make(
        {QUOTE: reply({"rsp_cd": "00000", "Output_0": {"stck_prpr": 1}})},
        options={
            "app_key": KEY,
            "token": "fixture-local",
            "expires_at": (NOW + timedelta(hours=1)).isoformat(),
            "acct_type": "01",
        },
        cache=cache,
        rate_limiter=RateLimiter({"read": 10000}),
    )
    fresh = {"token": "fixture-shared", "expires_at": (NOW + timedelta(hours=2)).isoformat()}
    cache[KEY] = fresh.copy()  # Another worker refreshes after this instance was created.
    assert broker.get_quote("005930").last == 1
    assert calls[0][2]["authorization"] == "Bearer fixture-shared"
    assert broker.token_state() == fresh


@pytest.mark.parametrize("code", ["00000", "00166", "00221"])
def test_absent_sellable_block_is_not_zero_for_other_success_codes(code):
    broker, _ = make({SELLABLE: reply({"rsp_cd": code})})
    with pytest.raises(InvalidResponseError):
        broker.available_qty("005930")


@pytest.mark.parametrize(
    "message",
    ["조회가 정상적으로 완료되었습니다.", "조회 완료하였습니다.", "조회 처리가 완료되었습니다.", "조회 정상완료"],
)
def test_success_code_with_required_block_accepts_success_wording(message):
    broker, _ = make(
        {
            "/krstock/inquiry/v1/balance": reply(
                {
                    "rsp_cd": "00000",
                    "rsp_msg": message,
                    "Output_0": BALANCE["Output_0"],
                }
            )
        },
        rate_limiter=RateLimiter({"read": 10000}),
    )
    assert broker.get_balance().cash_available == 40000


@pytest.mark.parametrize(
    "operation,path", [("get_quote", QUOTE), ("list_orders", "/krstock/inquiry/v1/dailyOrderExecution")]
)
def test_success_wording_without_required_block_fails_closed(operation, path):
    broker, _ = make({path: reply({"rsp_cd": "00000", "rsp_msg": "조회가 정상적으로 완료되었습니다."})})
    with pytest.raises(InvalidResponseError):
        getattr(broker, operation)(*([] if operation == "list_orders" else ["005930"]))


@pytest.mark.parametrize("expiry", ["not-iso", "2026-10-02T12:00:00", 42])
@pytest.mark.parametrize("stored", [False, True])
def test_malformed_shared_expiry_is_replaced_by_fresh_token(expiry, stored):
    cache = {KEY: {"token": "fixture-bad", "expires_at": expiry}}
    options = {"app_key": KEY, "app_secret": SECRET, "acct_type": "01"}
    if stored:
        options.update(token="fixture-good", expires_at=(NOW + timedelta(hours=1)).isoformat())
    broker, calls = make(
        {
            "/oauth2/token": reply({"access_token": "fixture-good", "expires_in": 86400}),
            QUOTE: reply({"rsp_cd": "00000", "Output_0": {"stck_prpr": 1}}),
        },
        options=options,
        cache=cache,
        rate_limiter=RateLimiter({"read": 10000}),
    )
    assert broker.get_quote("005930").last == 1
    assert cache[KEY] == broker.token_state()
    assert sum(path == "/oauth2/token" for _, path, _, _ in calls) == (not stored)


@pytest.mark.parametrize("virtual,value", [(True, "01"), (True, "02"), (True, None), (False, None), (False, "")])
def test_binding_requires_explicit_matching_account_type(virtual, value):
    with pytest.raises(ConfigurationError, match="account type"):
        make(virtual=virtual, options={"app_key": KEY, "token": "fixture-token", "acct_type": value})


def test_redirect_never_resends_authorization_to_second_server(monkeypatch):
    hits = [[], []]

    class LocalServer(HTTPServer):
        def server_bind(self):
            TCPServer.server_bind(self)
            self.server_name, self.server_port = self.server_address

    class Sink(BaseHTTPRequestHandler):
        def do_GET(self):
            hits[1].append(self.headers.get("authorization"))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        do_POST = do_GET

        def log_message(self, *args):
            pass

    with LocalServer(("127.0.0.1", 0), Sink) as sink:

        class Redirect(Sink):
            def do_POST(self):
                hits[0].append(self.headers.get("authorization"))
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{sink.server_port}/sink")
                self.end_headers()

        with LocalServer(("127.0.0.1", 0), Redirect) as source:

            def loopback_connection(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None):
                assert address[0] == "127.0.0.1" and address[1] in {source.server_port, sink.server_port}
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                    sock.settimeout(timeout)
                try:
                    _CONNECT(sock, address)
                except BaseException:
                    sock.close()
                    raise
                return sock

            monkeypatch.setattr(socket, "create_connection", loopback_connection)
            threads = [threading.Thread(target=server.serve_forever) for server in (source, sink)]
            for thread in threads:
                thread.start()
            try:
                client = _NhRestClient(base_url=f"http://127.0.0.1:{source.server_port}")
                with pytest.raises(TransportError, match="redirect"):
                    client.post("/start", headers={"authorization": "Bearer fixture-token"}, data=b"{}")
                assert hits == [["Bearer fixture-token"], []]
            finally:
                for server in (source, sink):
                    server.shutdown()
                for thread in threads:
                    thread.join(timeout=2)
                    assert not thread.is_alive()


@pytest.mark.parametrize("virtual", [False, True])
@pytest.mark.parametrize("operation", ["place_order", "cancel_order"])
def test_explicit_live_unlock_still_uses_the_bound_host(virtual, operation, live_safety):
    broker, calls, _ = make_sdk(virtual=virtual, safety=live_safety, allow_live_orders=True)
    request = (
        OrderRequest(symbol="005930", side="buy", quantity=1, price=70000, client_key="explicit")
        if operation == "place_order"
        else CancelRequest(symbol="005930", order_id="mkt:123", quantity=1, client_key="explicit")
    )
    assert getattr(broker, operation)(request).status == "accepted"
    assert len(calls) == 1
    assert calls[0][0] == (NH_MOCK_BASE_URL if virtual else NH_API_BASE_URL)


@pytest.mark.parametrize("value", [None, "true", "false", 1, 0])
def test_live_unlock_requires_a_real_boolean(value):
    with pytest.raises(ConfigurationError, match="allow_live_orders"):
        make_sdk(allow_live_orders=value)
