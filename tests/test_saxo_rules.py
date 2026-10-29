"""Saxo classification rules 1-4 (review of the #644 contract mock). Each rule is tested with the reply
given directly, not through the mock's current behaviour."""

import io
from urllib.error import HTTPError

import pytest

from pykbroker.brokers.saxo import SaxoOutcomeUnknownError, _Http, _Reply
from pykbroker.exceptions import BrokerRejectedError, InvalidResponseError
from pykbroker.reject_codes import TOKEN_EXPIRED

from .test_saxo_adapter import OK, Fake, make, order


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_rule1_every_5xx_is_unknown_even_with_error_info(status):
    fake = Fake(_Reply(status, {"ErrorInfo": {"ErrorCode": "InsufficientCash"}}, {}), OK)
    with pytest.raises(SaxoOutcomeUnknownError):
        make(fake).place_order(order())
    assert len(fake.sent) == 1  # never re-sent


def test_rule2_400_with_order_id_and_error_info_keeps_the_order():
    fake = Fake(_Reply(400, {"OrderId": "77", "ErrorInfo": {"ErrorCode": "RelatedOrderNotAllowedForInstrument"}}, {}))
    with pytest.raises(SaxoOutcomeUnknownError) as info:
        make(fake).place_order(order())
    assert not isinstance(info.value, BrokerRejectedError) and info.value.order_id == "77"
    # the same code without an OrderId is a definite rejection
    with pytest.raises(BrokerRejectedError):
        make(Fake(_Reply(400, {"ErrorInfo": {"ErrorCode": "InsufficientCash"}}, {}))).place_order(order(client_key="x2"))


class _Resp:
    def __init__(self, status, raw):
        self.status, self._raw, self.headers = status, raw, {}

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_rule3_non_json_body_is_no_body_and_status_decides(monkeypatch):
    import pykbroker.brokers.saxo as mod

    def html_403(request, *, timeout):
        raise HTTPError(request.full_url, 403, "Forbidden", {}, io.BytesIO(b"<html>Access Denied</html>"))

    monkeypatch.setattr(mod, "urlopen", html_403)
    reply = _Http().send("POST", "https://x/y", headers={}, body={}, timeout=1)
    assert reply.status == 403 and reply.body is None
    broker = make(None)
    broker._http = _Http()
    with pytest.raises(SaxoOutcomeUnknownError):  # 403 HTML: unknown, no parse crash
        broker.place_order(order())
    monkeypatch.setattr(mod, "urlopen", lambda r, *, timeout: _Resp(200, b"\xff\xfe not json"))
    assert _Http().send("GET", "https://x/y", headers={}, timeout=1).body is None
    monkeypatch.setattr(mod, "urlopen", lambda r, *, timeout: _Resp(200, b"[1, 2]"))
    other = make(None)
    other._http = _Http()
    with pytest.raises(InvalidResponseError):  # JSON but not an object: a read refuses, never a crash
        other.get_balance()


def test_rule3_html_401_still_classifies_by_status():
    with pytest.raises(BrokerRejectedError) as info:
        make(Fake(_Reply(401, None, {}))).place_order(order())
    assert info.value.kind == TOKEN_EXPIRED


def test_rule4_open_order_list_returns_unfilled_only():
    def row(oid, filled, status):
        return {"OrderId": oid, "Uic": 9001, "AssetType": "Stock", "Amount": 10, "FilledAmount": filled,
                "Status": status, "BuySell": "Buy", "OpenOrderType": "Limit", "Price": 190.0}

    fake = Fake(_Reply(200, {"Data": [row("1", 0, "Working"), row("2", 4, "Working"), row("3", 10, "Filled"),
                                      row("4", 0, "Cancelled")]}, {}))
    got = make(fake).list_orders("AAPL")
    assert [(o.id, o.status) for o in got] == [("1", "open"), ("2", "partial")]
    assert "Status=All" not in fake.sent[0][1]
