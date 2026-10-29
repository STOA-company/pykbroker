"""KB live server rejects empty dataHeader.ipAddr on TR requests (9999); fill ipAddr/macAddr (10-29)."""
import re
from unittest.mock import Mock

import pytest

from pykbroker.brokers import kb as kbmod
from pykbroker.brokers.kb import KB, KB_API_BASE_URL
from pykbroker.exceptions import ConfigurationError, InvalidResponseError

OK = {"dataHeader": {"resultCode": "200", "processFlag": "A"},
      "dataBody": {"Record1": [], "grid_cnt1": "0", "nxt_key": ""}}


def _broker(client, safety, **kw):
    return KB(account="fixture", bound_account="fixture", app_key="fake-app", token="fake-token",
              client=client, safety=safety, **kw)


def _sent_header(client):
    return client.post.call_args.kwargs["body"]["dataHeader"]


def test_request_has_nonempty_ip_and_mac(live_safety, monkeypatch):
    monkeypatch.setattr(kbmod, "_local_ip_addr", lambda: "10.1.2.3")
    client = Mock(base_url=KB_API_BASE_URL)
    client.post.return_value = OK
    _broker(client, live_safety).list_orders()
    h = _sent_header(client)
    assert h["ipAddr"] == "10.1.2.3"
    assert re.fullmatch(r"([0-9A-F]{2}:){5}[0-9A-F]{2}", h["macAddr"])


def test_header_ip_option_wins(live_safety, monkeypatch):
    monkeypatch.setattr(kbmod, "_local_ip_addr", lambda: "10.1.2.3")
    client = Mock(base_url=KB_API_BASE_URL)
    client.post.return_value = OK
    _broker(client, live_safety, header_ip="203.0.113.9").list_orders()
    assert _sent_header(client)["ipAddr"] == "203.0.113.9"
    client2 = Mock(base_url=KB_API_BASE_URL)
    client2.post.return_value = OK
    _broker(client2, live_safety, client_options={"header_ip": "203.0.113.8"}).list_orders()
    assert _sent_header(client2)["ipAddr"] == "203.0.113.8"


def test_no_ip_means_no_send(live_safety, monkeypatch):
    def boom():
        raise OSError("no route")
    monkeypatch.setattr(kbmod, "_local_ip_addr", boom)
    client = Mock(base_url=KB_API_BASE_URL)
    with pytest.raises(ConfigurationError):
        _broker(client, live_safety).list_orders()
    assert client.post.call_count == 0


def test_empty_header_ip_rejected(live_safety):
    with pytest.raises(ConfigurationError):
        _broker(Mock(base_url=KB_API_BASE_URL), live_safety, header_ip=" ")


def test_9999_stays_invalid_response(live_safety, monkeypatch):
    monkeypatch.setattr(kbmod, "_local_ip_addr", lambda: "10.1.2.3")
    client = Mock(base_url=KB_API_BASE_URL)
    client.post.return_value = {"dataHeader": {"resultCode": "200", "processFlag": "B", "processCode": "9999",
                                               "processMessage": "ipAddr"}, "dataBody": {}}
    with pytest.raises(InvalidResponseError):
        _broker(client, live_safety).list_orders()


def test_token_issuance_header_stays_empty():
    client = Mock(base_url=KB_API_BASE_URL)
    client.post.return_value = {"dataHeader": {"resultCode": "200", "processFlag": "A"},
                                "dataBody": {"access_token": "t", "token_type": "bearer", "expires_in": 100}}
    KB.issue_token(app_key="a" * 36, secret_key="b" * 32, client_factory=lambda: client)
    assert client.post.call_args.kwargs["body"]["dataHeader"] == {"ipAddr": "", "macAddr": ""}


@pytest.mark.parametrize("bad", ["not-an-ip", "999.1.1.1", "1.2.3", "::1", "a b", "10.0.0.1/24", "10.0.0.1,10.0.0.2"])
def test_non_ipv4_header_ip_rejected(live_safety, bad):
    client = Mock(base_url=KB_API_BASE_URL)
    with pytest.raises(ConfigurationError):
        _broker(client, live_safety, header_ip=bad)
    with pytest.raises(ConfigurationError):
        _broker(client, live_safety, client_options={"header_ip": bad})
    assert client.post.call_count == 0
