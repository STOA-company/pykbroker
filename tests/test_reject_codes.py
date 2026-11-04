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


def test_kb_order_code_is_not_a_common_definitive_rejection():
    from pykbroker.reject_codes import definitive_rejection

    assert "kb" not in DEFINITIVE_REJECTS
    assert definitive_rejection("kb", _HTTPError({"rsp_cd": "1853"})) is None
@pytest.mark.parametrize("broker,body,code,message", [
    ("kis", {"rt_cd": "1", "msg_cd": "APBK0952", "msg1": "fixture-KIS 거절"}, "APBK0952", "fixture-KIS 거절"),
    ("dbfi", {"rsp_cd": "fixture-code", "rsp_msg": "fixture-DB 거절"}, "fixture-code", "fixture-DB 거절"),
])
def test_broker_body_common_envelope(broker, body, code, message):
    """SDK envelope field shapes; failure values are offline compatibility fixtures."""
    from types import SimpleNamespace

    from pykbroker.brokers._common import check_response

    with pytest.raises(BrokerRejectedError) as info:
        check_response(SimpleNamespace(json_data=body, status_code=200), broker)
    assert (info.value.code, info.value.message, info.value.http_status) == (code, message, 200)
    assert (info.value.kind, info.value.reason) == ("rejected", None)
    assert message not in str(info.value) and message not in repr(info.value)


@pytest.mark.parametrize("exc,broker,code,message,status,kind,reason", [
    (_kis_envelope("APBK0952"), "kis", "APBK0952", "x", 403, "rejected", "insufficient_funds"),
    (_kis_gateway("EGW00123"), "kis", "EGW00123", "x", 500, "token_expired", "token_expired"),
    (_HTTPError({"rsp_cd": "IGW00121", "rsp_msg": "fixture-expired"}),
     "dbfi", "IGW00121", "fixture-expired", None, "token_expired", "token_expired"),
    (_kis_envelope(" APBK0952 "), "kis", " APBK0952 ", "x", 403, "rejected", "insufficient_funds"),
])
def test_broker_body_sdk_exception_preserves_raw_without_reclassifying(
    exc, broker, code, message, status, kind, reason
):
    with pytest.raises(BrokerRejectedError) as info:
        _Broker(broker)._call(_raise(exc))
    assert (info.value.code, info.value.message, info.value.http_status) == (code, message, status)
    assert (info.value.kind, info.value.reason) == (kind, reason)


def test_broker_body_kb_official_refusal():
    import json
    from pathlib import Path

    from pykbroker.reject_codes import kb_order_rejection

    sample = json.loads((Path(__file__).parent / "fixtures/kb/official/SSAM1801.json").read_text())
    # The saved official fixture retains the verbatim response under output.
    body = sample["output"]
    error = kb_order_rejection("ssam1801", body)
    assert (error.code, error.message, error.http_status) == ("1853", body["dataBody"]["o_msg"], None)
    assert (error.kind, error.reason) == ("rejected", "broker_rejected")
    assert error.message not in str(error) and error.message not in repr(error)


def test_broker_body_kiwoom_official_error_table():
    import json
    from pathlib import Path

    from pykbroker.brokers.kiwoom import _unpack

    row = json.loads((Path(__file__).parent / "fixtures/kiwoom/official/error_codes.json").read_text())[0]
    code, message = int(row["오류코드"]), row["오류메시지"]
    with pytest.raises(BrokerRejectedError) as info:
        _unpack(({"return_code": code, "return_msg": message}, {}), write=True)
    assert (info.value.code, info.value.message, info.value.http_status) == (code, message, None)
    assert info.value.return_code == code
    assert (info.value.kind, info.value.reason) == ("rejected", None)
    assert message not in str(info.value) and message not in repr(info.value)


@pytest.mark.parametrize("status,body", [
    (200, {"rsp_cd": " IGW40012 ", "message": {"usr_msg": "요청 형식이 유효하지 않습니다."}}),
    (401, {"rsp_cd": "IGW40043", "rsp_msg": "fixture-token-refused"}),
])
def test_broker_body_nh_known_status_and_raw_code(status, body):
    from types import SimpleNamespace

    from pykbroker.brokers.nh import NH

    broker = NH.__new__(NH)
    broker._ensure_token = lambda: "fixture"
    broker._discard_token = lambda: None
    broker._app_key = "fixture"
    broker._client = None
    broker._limiter = SimpleNamespace(wait=lambda _: None)
    broker._post_raw = lambda *args: (status, body, {})
    broker._definite_rejects = True
    message = body.get("rsp_msg") or body["message"]["usr_msg"]
    with pytest.raises(BrokerRejectedError) as info:
        broker._write("/fixture", {})
    assert (info.value.code, info.value.message, info.value.http_status) == (body["rsp_cd"], message, status)
    assert (info.value.kind, info.value.reason) == ("rejected", "broker_rejected" if status == 200 else None)
    assert message not in str(info.value) and message not in repr(info.value)


def test_broker_body_ls_read_fields_without_invented_http_status():
    from pykbroker.brokers.ls import LS

    broker = LS.__new__(LS)
    broker._client = None
    broker._verify_mode = lambda _: None
    broker._request = lambda *args, **kwargs: {"rsp_cd": "fixture-rejected", "rsp_msg": "fixture-LS 거절"}
    with pytest.raises(BrokerRejectedError) as info:
        broker._response("/fixture", "t1101", {})
    assert (info.value.code, info.value.message, info.value.http_status) == (
        "fixture-rejected", "fixture-LS 거절", None)
    assert (info.value.kind, info.value.reason) == ("rejected", None)


def test_broker_body_exception_bounds_message_without_masking_or_printing():
    message = "fixture-원문 " * 1000
    error = BrokerRejectedError(vendor_message=message, code="fixture", http_status=422)
    assert error.message == message[:4096]
    assert error.http_status == 422
    assert str(error) == "Broker rejected the request"
    assert "fixture-원문" not in repr(error)
