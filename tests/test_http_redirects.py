"""Redirect regressions use two disposable loopback servers, never a broker host."""

import socket
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import pytest

from pykbroker._http import urlopen
from pykbroker.brokers.kiwoom import Kiwoom, _KiwoomRestClient
from pykbroker.exceptions import TransportError
from pykbroker.models import OrderRequest

_CREATE_CONNECTION = socket.create_connection
_CONNECT = socket.socket.connect
_GETADDRINFO = socket.getaddrinfo


@contextmanager
def redirect_servers(monkeypatch, status):
    first_requests, second_requests = [], []

    class Destination(BaseHTTPRequestHandler):
        def do_GET(self):
            second_requests.append(dict(self.headers))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"return_code":0}')

        do_POST = do_GET

        def log_message(self, *args):
            pass

    second = HTTPServer(("127.0.0.1", 0), Destination)

    class Redirect(Destination):
        def do_GET(self):
            first_requests.append((self.path, dict(self.headers)))
            self.send_response(status)
            self.send_header("Location", f"http://127.0.0.1:{second.server_port}/destination")
            self.end_headers()

        do_POST = do_GET

    first = HTTPServer(("127.0.0.1", 0), Redirect)
    allowed = {("127.0.0.1", server.server_port) for server in (first, second)}

    def connect(sock, address):
        assert address in allowed, "Only these two fake servers may be contacted"
        return _CONNECT(sock, address)

    def getaddrinfo(host, port, *args, **kwargs):
        assert (host, port) in allowed, "External DNS is forbidden"
        return _GETADDRINFO(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "create_connection", _CREATE_CONNECTION)
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    # Ignore any machine proxy settings; an external proxy remains blocked above.
    for key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(key, raising=False)
    threads = [threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
               for server in (first, second)]
    for thread in threads:
        thread.start()
    try:
        yield f"http://127.0.0.1:{first.server_port}", first_requests, second_requests
    finally:
        for server, thread in zip((first, second), threads, strict=True):
            server.shutdown()
            thread.join()
            server.server_close()


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("operation", ["quote", "token", "order"])
def test_kiwoom_redirect_never_reaches_second_server(monkeypatch, status, operation, live_safety):
    with redirect_servers(monkeypatch, status) as (base_url, first, second):
        def factory(**kwargs):
            return _KiwoomRestClient(base_url, timeout=2)
        caught = None
        try:
            if operation != "token":
                broker = Kiwoom(
                    account="fake-account", client_options={"token": "fake-token", "app_key": "fake-app"},
                    client_factory=factory, rate_limiter=SimpleNamespace(wait=lambda tr: None), safety=live_safety,
                )
                if operation == "quote":
                    broker.get_quote("005930")
                else:
                    broker.place_order(OrderRequest(
                        symbol="005930", side="buy", quantity=1, price="70000", client_key="fake-redirect",
                    ))
            else:
                Kiwoom.issue_token(app_key="fake-app", secret_key="fake-secret", client_factory=factory)
        except Exception as exc:
            caught = exc
        assert len(first) == 1
        assert second == [], "Redirect destination received a request (possibly with a token)"
        assert isinstance(caught, TransportError)
        assert "fake-token" not in str(caught) and "fake-secret" not in str(caught)
        if operation != "token":
            assert first[0][1]["Authorization"] == "Bearer fake-token"


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("method", ["GET", "POST"])
def test_shared_opener_blocks_redirect_status(monkeypatch, status, method):
    with redirect_servers(monkeypatch, status) as (base_url, first, second):
        request = urllib.request.Request(
            base_url, method=method, headers={"Authorization": "Bearer fake-token"},
            data=b"{}" if method == "POST" else None,
        )
        with pytest.raises(urllib.error.HTTPError) as caught:
            urlopen(request, timeout=2)
        assert caught.value.code == status
        caught.value.close()
        assert len(first) == 1
        assert second == []


@pytest.mark.parametrize("status", [400, 401, 429, 500])
def test_shared_opener_preserves_http_error(monkeypatch, status):
    with redirect_servers(monkeypatch, status) as (base_url, first, second):
        with pytest.raises(urllib.error.HTTPError) as caught:
            urlopen(urllib.request.Request(base_url), timeout=2)
        assert caught.value.code == status
        caught.value.close()
        assert len(first) == 1 and second == []


def test_shared_opener_preserves_success(monkeypatch):
    with redirect_servers(monkeypatch, 200) as (base_url, first, second):
        with urlopen(urllib.request.Request(base_url), timeout=2) as response:
            assert response.status == 200
            assert response.read() == b""
        assert len(first) == 1 and second == []


def test_local_server_allowlist_still_blocks_external_connections(monkeypatch):
    with redirect_servers(monkeypatch, 302):
        with pytest.raises(AssertionError, match="External DNS is forbidden"):
            socket.getaddrinfo("broker.invalid", 443)
        with socket.socket() as sock:
            with pytest.raises(AssertionError, match="Only these two fake servers"):
                sock.connect(("192.0.2.1", 443))
