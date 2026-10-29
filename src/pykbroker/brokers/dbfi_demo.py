"""DB's shared REST host requires positive evidence from the authenticated session.

Official spec 564b510: CSPAQ01800 is read-only and unavailable in demo.
Never log credentials, headers, response bodies or the process-local fingerprints.
"""
import hashlib
import json
from threading import RLock
from typing import Any, Mapping

from pykbroker.exceptions import ConfigurationError

_REST_URL = "https://openapi.dbsec.co.kr:8443"
_PROBE_PATH = "/api/v1/trading/kr-stock/inquiry/daily-ernrate"
_DEMO_MESSAGE = "모의투자에서는 해당업무가 제공되지 않습니다."
_verified: set[bytes] = set()
_lock = RLock()


def verify_demo_client(client: Any, *, account: str, credentials: Mapping[str, Any]) -> None:
    """Prove this exact key/session; failed or changed sessions never inherit proof.

    Recheck credential binding on every call. Cache only a digest, additionally
    scoped to account and effective headers so a substituted token cannot pass.
    The probe bypasses Pydb retries/continuations and never submits an order.
    """
    try:
        auth = client.domestic.auth
        key, secret = auth.appkey, auth.appsecretkey
        if not all(isinstance(v, str) and v for v in (key, secret, account)):
            raise ValueError
        for name, actual in (("app_key", key), ("app_secret_key", secret)):
            if name in credentials and credentials[name] != actual:
                raise ValueError
        # Overseas must execute under the same authenticated session as the probe.
        overseas = getattr(client, "overseas", None)
        if overseas is not None and overseas.auth is not auth:
            raise ValueError
        headers = auth.get_auth_header()
        if not isinstance(headers, dict) or not headers or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in headers.items()
        ):
            raise ValueError
        if not any(k.lower() == "authorization" and v.strip() for k, v in headers.items()):
            raise ValueError
        fingerprint = hashlib.sha256(json.dumps(
            [key, secret, account, sorted(headers.items())], ensure_ascii=True
        ).encode()).digest()
    except Exception:
        raise ConfigurationError("DBFI demo credentials/session cannot be established") from None
    failed = False
    with _lock:
        if fingerprint in _verified:
            return
        try:
            import requests
            response = requests.request(
                method="POST", url=_REST_URL + _PROBE_PATH,
                headers={**headers, "Content-Type": "application/json", "cont_yn": "N", "cont_key": ""},
                json={"In": {"QryTp": "2"}}, timeout=(5, 10), allow_redirects=False,
            )
            body = response.json()
            if (response.status_code != 200 or not isinstance(body, dict)
                    or body.get("rsp_cd") != "M100" or body.get("rsp_msg") != _DEMO_MESSAGE):
                failed = True
            else:
                _verified.add(fingerprint)
        except Exception:
            failed = True
    if failed:
        raise ConfigurationError("DBFI demo key was not positively verified; execution refused") from None
