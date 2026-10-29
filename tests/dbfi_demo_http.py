"""Loopback-only HTTP boundary used by DB demo integration tests."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
import requests

DEMO = {"rsp_cd": "M100", "rsp_msg": "모의투자에서는 해당업무가 제공되지 않습니다."}
PROBE = "/api/v1/trading/kr-stock/inquiry/daily-ernrate"


@pytest.fixture
def demo_http(monkeypatch):
    state = SimpleNamespace(probe=DEMO.copy(), status=200, calls=[], headers=[], bodies=[], responses={})

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            state.calls.append(self.path)
            state.headers.append(dict(self.headers))
            state.bodies.append(json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0")))))
            body = state.probe if self.path == PROBE else state.responses.get(self.path, {"rsp_cd": "00000", "Out": {"OrdNo": 42}})
            self.send_response(state.status if self.path == PROBE else 200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body if isinstance(body, bytes) else json.dumps(body).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever)
    thread.start()
    original = requests.request

    def route(method, url, **kwargs):
        parsed = urlsplit(url)
        assert parsed.hostname == "openapi.dbsec.co.kr" and parsed.port == 8443
        return original(method, f"http://127.0.0.1:{server.server_port}{parsed.path}", **kwargs)

    monkeypatch.setattr(requests, "request", route)
    # Each test starts with an empty process-local cache. Real instances share it.
    from pykbroker.brokers import dbfi_demo
    monkeypatch.setattr(dbfi_demo, "_verified", set())
    monkeypatch.setattr(dbfi_demo, "_rejected", set())
    try:
        yield state
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


class Auth:
    def __init__(self, key="synthetic-demo-key", secret="synthetic-demo-secret", token="synthetic-demo-token"):
        self.appkey, self.appsecretkey, self.token = key, secret, token

    def get_auth_header(self):
        return {"authorization": "Bearer " + self.token}
