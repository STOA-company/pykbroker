"""DB's shared REST host requires positive evidence from the authenticated session.

Official spec 564b510: CSPAQ01800 is read-only and unavailable in demo (M100 reply).
Observed 2026-10-30 on the shared host with a demo key: the same probe now answers
00000 "모의투자 조회가 완료되었습니다." — the demo-prefixed success is also demo proof.
Official error table (564b510 docs/errors_and_rate_limits.md:106,108,109) lists the live
"조회가 완료되었습니다." apart from the demo-prefixed 00000 replies. Only these exact
(rsp_cd, rsp_msg) pairs prove demo; a plain success is never proof.
Never log credentials, headers, response bodies or the process-local fingerprints.
"""
import hashlib
import json
from copy import copy
from threading import RLock
from typing import Any, Mapping

from pykbroker.exceptions import ConfigurationError

_REST_URL = "https://openapi.dbsec.co.kr:8443"
_PROBE_PATH = "/api/v1/trading/kr-stock/inquiry/daily-ernrate"
_DEMO_MESSAGE = "모의투자에서는 해당업무가 제공되지 않습니다."
_DEMO_PROOFS = frozenset({
    ("M100", _DEMO_MESSAGE),
    ("00000", "모의투자 조회가 완료되었습니다."),
    ("00000", "모의투자 데이터가 없습니다."),
})
_REFUSED = "DBFI demo key was not positively verified; execution refused"
_verified: set[bytes] = set()
_rejected: set[bytes] = set()
_lock = RLock()
# Per-request random/transport columns (Pydb adds uuid4 X-Session-ID, a rotating
# User-Agent and, 1 in 5, X-Forwarded-For). They carry no credential identity, so
# they must not make every request look like a new session. Every other header,
# Authorization above all, still belongs to the fingerprint.
_VOLATILE_HEADERS = frozenset({
    "x-session-id", "user-agent", "x-forwarded-for", "x-real-ip", "x-request-id",
    "x-correlation-id", "x-trace-id", "traceparent", "tracestate",
})


def _verified_headers(auth: Any, *, account: str, credentials: Mapping[str, Any]) -> dict[str, str]:
    """Prove exactly the header snapshot the transport is about to receive."""
    try:
        key, secret = auth.appkey, auth.appsecretkey
        if not all(isinstance(v, str) and v for v in (key, secret, account)):
            raise ValueError
        for name, actual in (("app_key", key), ("app_secret_key", secret)):
            if name in credentials and credentials[name] != actual:
                raise ValueError
        headers = auth.get_auth_header()
        if not isinstance(headers, dict) or not headers or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in headers.items()
        ):
            raise ValueError
        if not any(k.lower() == "authorization" and v.strip() for k, v in headers.items()):
            raise ValueError
        headers = dict(headers)
        fingerprint = hashlib.sha256(json.dumps(
            [key, secret, account,
             sorted((k.lower(), v) for k, v in headers.items() if k.lower() not in _VOLATILE_HEADERS)],
            ensure_ascii=True
        ).encode()).digest()
    except Exception:
        raise ConfigurationError("DBFI demo credentials/session cannot be established") from None
    failed = False
    with _lock:
        if fingerprint in _verified:
            return headers
        if fingerprint in _rejected:
            raise ConfigurationError(_REFUSED) from None
        try:
            import requests
            response = requests.request(
                method="POST", url=_REST_URL + _PROBE_PATH,
                headers={**headers, "Content-Type": "application/json", "cont_yn": "N", "cont_key": ""},
                json={"In": {"QryTp": "2"}}, timeout=(5, 10), allow_redirects=False,
            )
            body = response.json()
            if (response.status_code != 200 or not isinstance(body, dict)
                    or (body.get("rsp_cd"), body.get("rsp_msg")) not in _DEMO_PROOFS):
                failed = True
            else:
                _verified.add(fingerprint)
        except Exception:
            failed = True
        if failed:
            _rejected.add(fingerprint)
    if failed:
        raise ConfigurationError(_REFUSED) from None
    return headers


class _DemoAuth:
    """Per-client guard: Pydb retries must prove refreshed headers before sending."""

    def __init__(self, source: Any, account: str, credentials: Mapping[str, Any]):
        object.__setattr__(self, "_source", source)
        object.__setattr__(self, "_account", account)
        object.__setattr__(self, "_credentials", dict(credentials))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._source, name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._source, name, value)

    def get_auth_header(self) -> dict[str, str]:
        return _verified_headers(self._source, account=self._account, credentials=self._credentials)


def verify_demo_client(client: Any, *, account: str, credentials: Mapping[str, Any]) -> Any:
    """Return a private API/service copy whose actual transport proves every session.

    Keep the vendor's token refresh/retry flow. Only demo instances receive the
    header guard; original clients, service instances and live callers stay intact.
    A cached service with another auth object is refused before any order request.
    """
    services = ("_trading_service", "_quote_service", "_chart_service")
    try:
        auth = client.domestic.auth
        apis = [(name, getattr(client, name)) for name in ("domestic", "overseas", "domestic_futures")
                if getattr(client, name, None) is not None]
        for _, api in apis:
            if api.auth is not auth:
                raise ValueError
            for name in services:
                service = getattr(api, name, None)
                if service is not None and service.auth is not auth:
                    raise ValueError
    except Exception:
        raise ConfigurationError("DBFI demo transport session does not match its API session") from None
    if isinstance(auth, _DemoAuth):
        source = auth._source
        if auth._account == account and auth._credentials == dict(credentials):
            auth.get_auth_header()
            return client
    else:
        source = auth
    _verified_headers(source, account=account, credentials=credentials)
    guarded_auth = _DemoAuth(source, account, credentials)
    guarded_client = copy(client)
    for name, api in apis:
        guarded_api = copy(api)
        guarded_api.auth = guarded_auth
        for service_name in services:
            service = getattr(api, service_name, None)
            if service is not None:
                guarded_service = copy(service)
                guarded_service.auth = guarded_auth
                setattr(guarded_api, service_name, guarded_service)
        setattr(guarded_client, name, guarded_api)
    return guarded_client
