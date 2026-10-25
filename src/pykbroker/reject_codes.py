"""Per-broker «definitive not-accepted» code table.

One table decides, for every broker, which vendor answers prove an order was *not* accepted.
Everything else stays unknown: a timeout, a lost response, or a 5xx without a listed code may
hide a real order, so it is never turned into a rejection here.

Classes
  rejected       the vendor processed the request and refused it (business rule).
  token_expired  the gateway refused the token before the order was processed. The SDK
                 drops its cached token (Pykis client.py:183-192 sets token.empty, Pydb
                 base.py:88-93 re-issues), so the next call carries a fresh token. The
                 order itself is not re-sent: the customer decides again.
  rate_limited   the gateway throttled the call before processing; retrying later is safe.

Every code names where it is documented. A code without a source is not listed.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

REJECTED = "rejected"
TOKEN_EXPIRED = "token_expired"
RATE_LIMITED = "rate_limited"

_CODE = re.compile(r"[A-Za-z0-9_-]{1,40}")


@dataclass(frozen=True)
class RejectCode:
    kind: str
    reason: str
    source: str


#: broker_code -> vendor code -> class. broker_code matches BaseBroker.broker_code.
DEFINITIVE_REJECTS: dict[str, dict[str, RejectCode]] = {
    "meritz": {
        "0760": RejectCode(REJECTED, "rejected", "meritz-securities/open-api@2cef2e9f docs/errors.md 0760"),
        "3174": RejectCode(REJECTED, "rejected", "meritz-securities/open-api@2cef2e9f docs/errors.md 3174"),
        "3176": RejectCode(REJECTED, "rejected", "meritz-securities/open-api@2cef2e9f docs/errors.md 3176"),
        "3180": RejectCode(REJECTED, "rejected", "meritz-securities/open-api@2cef2e9f docs/errors.md 3180"),
        "5018": RejectCode(REJECTED, "rejected", "meritz-securities/open-api@2cef2e9f docs/errors.md 5018"),
        "5806": RejectCode(REJECTED, "rejected", "meritz-securities/open-api@2cef2e9f docs/errors.md 5806"),
        "6102": RejectCode(REJECTED, "rejected", "meritz-securities/open-api@2cef2e9f docs/errors.md 6102"),
        "7936": RejectCode(REJECTED, "rejected", "meritz-securities/open-api@2cef2e9f docs/errors.md 7936"),
        "IGW50004": RejectCode(REJECTED, "rejected", "meritz-securities/open-api@2cef2e9f docs/errors.md IGW50004"),
        "IGW50006": RejectCode(REJECTED, "rejected", "meritz-securities/open-api@2cef2e9f docs/errors.md IGW50006"),
        "IGW50024": RejectCode(REJECTED, "rejected", "meritz-securities/open-api@2cef2e9f docs/errors.md IGW50024"),
        "EGW00103": RejectCode(REJECTED, "rejected", "meritz-securities/open-api@2cef2e9f docs/errors.md EGW00103"),
        "EGW00121": RejectCode(TOKEN_EXPIRED, "token_expired",
                                 "meritz-securities/open-api@2cef2e9f docs/errors.md EGW00121"),
        "EGW00123": RejectCode(TOKEN_EXPIRED, "token_expired",
                                 "meritz-securities/open-api@2cef2e9f docs/errors.md EGW00123"),
        "EGW00200": RejectCode(RATE_LIMITED, "rate_limited",
                                "meritz-securities/open-api@2cef2e9f docs/errors.md EGW00200"),
        "EGW00202": RejectCode(REJECTED, "rejected", "meritz-securities/open-api@2cef2e9f docs/errors.md EGW00202"),
    },
    "kis": {
        # HTTP 500 gateway envelope {"rt_cd":"1","msg_cd":...}: the token is refused before the TR runs.
        "EGW00121": RejectCode(TOKEN_EXPIRED, "token_expired",
                               "Pykis/_pykis/client/client.py:189 «유효하지 않은 token 입니다.»"),
        "EGW00122": RejectCode(TOKEN_EXPIRED, "token_expired",
                               "Pykis/_pykis/client/client.py:190 «token을 찾을 수 없습니다.»"),
        "EGW00123": RejectCode(TOKEN_EXPIRED, "token_expired",
                               "Pykis/_pykis/client/client.py:191 «기간이 만료된 token 입니다.»"),
        "EGW00201": RejectCode(RATE_LIMITED, "rate_limited",
                               "KIS 오류코드 «초당 거래건수를 초과하였습니다.» — "
                               "quantus_backend/brokers/credentials.py:21, tests/contract_walk/kis_mock.py:6-7"),
        # HTTP 200 envelope rt_cd!=0: the order TR ran and refused. Any such answer is definitive
        # (check_response contract); the listed codes only carry a readable reason.
        "APBK0952": RejectCode(REJECTED, "insufficient_funds",
                               "quantus_backend/common/stock_consts.py:51 «주문가능금액을초과»"),
    },
    # KB B2C: public error list https://openapi.kbsec.com/api/codes/public/B2C_error_code (confirmed 2026-10-24,
    # tests/fixtures/kb/official/public-errors.json). The list gives meaning only; WHERE the code sits in the
    # response (dataHeader.resultCode vs processCode, HTTP status) is not documented, so the adapter matches
    # an exact table key in either dataHeader field and nothing else.
    "kb": {
        "T041": RejectCode(TOKEN_EXPIRED, "token_invalid", "KB B2C_error_code T041 «유효하지 않은 접근 토큰입니다.»"),
        "T042": RejectCode(TOKEN_EXPIRED, "token_not_found", "KB B2C_error_code T042 «접근 토큰을 찾을 수 없습니다.»"),
        "T043": RejectCode(TOKEN_EXPIRED, "token_expired", "KB B2C_error_code T043 «접근 토큰 기간이 만료되었습니다.»"),
        "T044": RejectCode(TOKEN_EXPIRED, "token_invalid", "KB B2C_error_code T044 «토큰 검증에 실패했습니다.»"),
        "E041": RejectCode(RATE_LIMITED, "rate_limited",
                           "KB B2C_error_code E041 «서비스 호출 건수룰 초과 하였습니다.»"),
        "I435": RejectCode(RATE_LIMITED, "rate_limited", "KB B2C_error_code I435 «서비스 호출 건수를 초과하였습니다.»"),
        "E042": RejectCode(RATE_LIMITED, "rate_limited", "KB B2C_error_code E042 «유량제어 정책을 확인해주세요.»"),
        "E031": RejectCode(REJECTED, "ip_not_allowed", "KB B2C_error_code E031 «허용되지 않는 IP입니다.»"),
        "I446": RejectCode(REJECTED, "no_api_permission", "KB B2C_error_code I446 «API 사용 권한이 없습니다.»"),
        "T045": RejectCode(REJECTED, "no_api_permission", "KB B2C_error_code T045 «API 사용 권한이 없습니다.»"),
    },
    # NH PLUG gateway errors: https://www.nhplug.com/testbed-howto (official error table, 2026-10-24,
    # tests/fixtures/nh/official/CONTRACT-2026-10-24.md). HTTP/envelope placement is the adapter's assumption.
    "nh": {
        "IGW40019": RejectCode(REJECTED, "invalid_symbol", "nhplug.com/testbed-howto IGW40019 종목코드 오류"),
        "IGW40031": RejectCode(REJECTED, "invalid_app_key", "nhplug.com/testbed-howto IGW40031 AppKey 오류"),
        "IGW40032": RejectCode(REJECTED, "invalid_app_secret", "nhplug.com/testbed-howto IGW40032 AppSecret 오류"),
        "IGW40043": RejectCode(TOKEN_EXPIRED, "token_invalid", "nhplug.com/testbed-howto IGW40043 무효 토큰"),
        "IGW40044": RejectCode(TOKEN_EXPIRED, "token_expired", "nhplug.com/testbed-howto IGW40044 만료 토큰"),
        "IGW42901": RejectCode(RATE_LIMITED, "rate_limited", "nhplug.com/testbed-howto IGW42901 시스템 총 호출 초과"),
        "IGW42902": RejectCode(RATE_LIMITED, "rate_limited", "nhplug.com/testbed-howto IGW42902 APP 총 호출 초과"),
        "IGW42903": RejectCode(RATE_LIMITED, "rate_limited", "nhplug.com/testbed-howto IGW42903 APP+API 총 호출 초과"),
    },
    # LS: the public TR guide / FAQ publish no rsp_cd meaning table (tests/fixtures/ls/contract-20261024/CONTRACT.md),
    # so no row is listed; every non-zero LS write answer stays unknown. See docs/ls-spec.md.
    "ls": {},
    "dbfi": {
        # HTTP 500 + rsp_cd IGW00121: Pydb re-issues the token and raises (base.py:88-98).
        "IGW00121": RejectCode(TOKEN_EXPIRED, "token_expired",
                               "Pydb/pydbfi/service/common/base.py:89 «token 유효성 만료»"),
    },
    # Kiwoom REST returns the code in the body (return_code). Every row is a line of the official error table in
    # Kiwoom-Securities/Kiwoom-REST-API kiwoom/_data/kiwoom_api_spec.json (sha256 42a7b3912c9d…ba79, 2026-10-25).
    # 1999 «예기치 못한 에러» is deliberately absent: it may arrive after the order was taken, so it stays unknown.
    "kiwoom": {
        "1501": RejectCode(REJECTED, "broker_rejected", "Kiwoom spec error table row 1501"),
        "1504": RejectCode(REJECTED, "broker_rejected", "Kiwoom spec error table row 1504"),
        "1505": RejectCode(REJECTED, "broker_rejected", "Kiwoom spec error table row 1505"),
        "1511": RejectCode(REJECTED, "broker_rejected", "Kiwoom spec error table row 1511"),
        "1512": RejectCode(REJECTED, "broker_rejected", "Kiwoom spec error table row 1512"),
        "1513": RejectCode(REJECTED, "broker_rejected", "Kiwoom spec error table row 1513"),
        "1514": RejectCode(REJECTED, "broker_rejected", "Kiwoom spec error table row 1514"),
        "1515": RejectCode(REJECTED, "broker_rejected", "Kiwoom spec error table row 1515"),
        "1516": RejectCode(REJECTED, "broker_rejected", "Kiwoom spec error table row 1516"),
        "1517": RejectCode(REJECTED, "broker_rejected", "Kiwoom spec error table row 1517"),
        "1687": RejectCode(RATE_LIMITED, "rate_limited", "Kiwoom spec error table row 1687"),
        "1700": RejectCode(RATE_LIMITED, "rate_limited", "Kiwoom spec error table row 1700"),
        "1701": RejectCode(RATE_LIMITED, "rate_limited", "Kiwoom spec error table row 1701"),
        "1702": RejectCode(RATE_LIMITED, "rate_limited", "Kiwoom spec error table row 1702"),
        "1901": RejectCode(REJECTED, "broker_rejected", "Kiwoom spec error table row 1901"),
        "1902": RejectCode(REJECTED, "broker_rejected", "Kiwoom spec error table row 1902"),
        "1903": RejectCode(REJECTED, "broker_rejected", "Kiwoom spec error table row 1903"),
        "8001": RejectCode(REJECTED, "broker_rejected", "Kiwoom spec error table row 8001"),
        "8002": RejectCode(REJECTED, "broker_rejected", "Kiwoom spec error table row 8002"),
        "8003": RejectCode(REJECTED, "broker_rejected", "Kiwoom spec error table row 8003"),
        "8005": RejectCode(TOKEN_EXPIRED, "token_expired", "Kiwoom spec error table row 8005"),
        "8006": RejectCode(REJECTED, "broker_rejected", "Kiwoom spec error table row 8006"),
        "8009": RejectCode(REJECTED, "broker_rejected", "Kiwoom spec error table row 8009"),
        "8010": RejectCode(REJECTED, "broker_rejected", "Kiwoom spec error table row 8010"),
        "8011": RejectCode(REJECTED, "broker_rejected", "Kiwoom spec error table row 8011"),
        "8012": RejectCode(REJECTED, "broker_rejected", "Kiwoom spec error table row 8012"),
        "8015": RejectCode(REJECTED, "broker_rejected", "Kiwoom spec error table row 8015"),
        "8016": RejectCode(REJECTED, "broker_rejected", "Kiwoom spec error table row 8016"),
        "8020": RejectCode(REJECTED, "broker_rejected", "Kiwoom spec error table row 8020"),
        "8030": RejectCode(REJECTED, "broker_rejected", "Kiwoom spec error table row 8030"),
        "8031": RejectCode(REJECTED, "broker_rejected", "Kiwoom spec error table row 8031"),
        "8040": RejectCode(REJECTED, "broker_rejected", "Kiwoom spec error table row 8040"),
        "8050": RejectCode(REJECTED, "broker_rejected", "Kiwoom spec error table row 8050"),
        "8103": RejectCode(REJECTED, "broker_rejected", "Kiwoom spec error table row 8103"),
        "8104": RejectCode(REJECTED, "broker_rejected", "Kiwoom spec error table row 8104"),
        "8200": RejectCode(REJECTED, "broker_rejected", "Kiwoom spec error table row 8200"),
    },
}


def _safe(code: Any) -> str | None:
    text = str(code).strip() if code is not None else ""
    return text if _CODE.fullmatch(text) else None


def _vendor_answer(exc: BaseException) -> tuple[str | None, bool]:
    """(vendor code, answered-by-envelope) read from an SDK exception, never its payload text.

    answered-by-envelope is True only for an HTTP 200 envelope with a failure status (Pykis
    raises ValueError({"code", "detail": {"rt_cd"}}) from responses/response.py:48-61): the
    request was processed and refused, whatever the code.
    """
    if type(exc).__name__ == "KisTransientError":  # Pykis marks it retryable: not a final answer
        return None, False
    if isinstance(exc, ValueError) and exc.args and isinstance(exc.args[0], Mapping):
        payload = exc.args[0]
        detail = payload.get("detail")
        if isinstance(detail, Mapping) and "rt_cd" in detail and payload.get("code") is not None:
            return _safe(payload.get("code")), str(detail.get("rt_cd")) != "0"
        if isinstance(detail, Mapping) and isinstance(detail.get("text"), Mapping):
            return _safe(detail["text"].get("msg_cd")), False
        return None, False
    response = getattr(exc, "response", None)
    if response is None:
        return None, False
    try:
        body = response.json()
    except Exception:
        return None, False
    if not isinstance(body, Mapping):
        return None, False
    return _safe(body.get("rsp_cd") or body.get("msg_cd")), False


def rejection_for_code(broker: str, code: Any):
    """BrokerRejectedError for a vendor code listed (with source) in the table, else None (stays unknown)."""
    from pykbroker.exceptions import BrokerRejectedError

    text = _safe(code)
    entry = DEFINITIVE_REJECTS.get(broker, {}).get(text) if text else None
    if entry is None:
        return None
    return BrokerRejectedError("Broker did not accept the request", code=text, kind=entry.kind, reason=entry.reason)


def classify(broker: str, exc: BaseException) -> tuple[str | None, RejectCode | None, bool]:
    code, by_envelope = _vendor_answer(exc)
    entry = DEFINITIVE_REJECTS.get(broker, {}).get(code) if code else None
    return code, entry, by_envelope


def definitive_rejection(broker: str, exc: BaseException):
    """BrokerRejectedError when the vendor's answer proves «not accepted», else None (stays unknown)."""
    from pykbroker.exceptions import BrokerRejectedError

    code, entry, by_envelope = classify(broker, exc)
    if entry is not None:
        return BrokerRejectedError(
            "Broker did not accept the request", code=code, kind=entry.kind, reason=entry.reason)
    if by_envelope:
        return BrokerRejectedError("Broker rejected the request; inspect vendor status securely",
                                   code=code, kind=REJECTED, reason="broker_rejected")
    return None
