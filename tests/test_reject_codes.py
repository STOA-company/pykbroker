"""reject_codes: a listed vendor answer proves «not accepted»; anything else stays unknown."""

import pytest

from pykbroker.brokers._common import BaseBroker
from pykbroker.exceptions import BrokerRejectedError, InvalidResponseError, TransportError
from pykbroker.reject_codes import DEFINITIVE_REJECTS


class _Broker(BaseBroker):
    def __init__(self, code):
        self.broker_code = code
        super().__init__(account="A1")


def _kis_envelope(msg_cd, rt_cd="1"):
    return ValueError({"status_code": 403, "code": msg_cd, "msg": "x", "detail": {"rt_cd": rt_cd, "uid": "u"}})


def _kis_gateway(msg_cd):
    return ValueError({"status_code": 500, "detail": {"text": {"rt_cd": "1", "msg_cd": msg_cd, "msg1": "x"}}})


class _Resp:
    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


class _HTTPError(Exception):
    def __init__(self, body):
        super().__init__("500")
        self.response = _Resp(body)


def _raise(exc):
    def run():
        raise exc
    return run


@pytest.mark.parametrize("code,kind,reason", [
    ("APBK0952", "rejected", "insufficient_funds"),
    ("APBK9999", "rejected", "broker_rejected"),  # any HTTP 200 rt_cd!=0 answer was processed and refused
])
def test_kis_envelope_rejection_is_definite(code, kind, reason):
    with pytest.raises(BrokerRejectedError) as info:
        _Broker("kis")._call(_raise(_kis_envelope(code)))
    assert (info.value.code, info.value.kind, info.value.reason) == (code, kind, reason)


@pytest.mark.parametrize("code,kind", [("EGW00123", "token_expired"), ("EGW00121", "token_expired"),
                                       ("EGW00201", "rate_limited")])
def test_kis_gateway_listed_codes_are_definite(code, kind):
    with pytest.raises(BrokerRejectedError) as info:
        _Broker("kis")._call(_raise(_kis_gateway(code)))
    assert info.value.kind == kind


def test_kis_gateway_unlisted_code_stays_unknown():
    with pytest.raises(InvalidResponseError):
        _Broker("kis")._call(_raise(_kis_gateway("EGW00999")))


def test_kis_transient_error_stays_unknown():
    transient = type("KisTransientError", (ValueError,), {})
    with pytest.raises(InvalidResponseError):
        _Broker("kis")._call(_raise(transient({"status_code": 403, "code": "APBK1350", "detail": {"rt_cd": "1"}})))


def test_dbfi_expired_token_is_definite_and_unknown_5xx_is_not():
    with pytest.raises(BrokerRejectedError) as info:
        _Broker("dbfi")._call(_raise(_HTTPError({"rsp_cd": "IGW00121"})))
    assert info.value.kind == "token_expired"
    with pytest.raises(TransportError):
        _Broker("dbfi")._call(_raise(_HTTPError({"rsp_cd": "IGW99999"})))
    with pytest.raises(TransportError):
        _Broker("dbfi")._call(_raise(TimeoutError()))


def test_every_code_names_its_source():
    for broker, table in DEFINITIVE_REJECTS.items():
        for code, entry in table.items():
            assert entry.source.strip(), (broker, code)
            assert entry.kind in {"rejected", "token_expired", "rate_limited"}
