"""Kiwoom mock (paper-trading) order -> cancel round trip: verification only.

Purpose: prove that pykbroker's existing Kiwoom adapter (src/pykbroker/brokers/kiwoom.py) can place one
small, deliberately unfillable limit buy on Kiwoom's KRW *mock* venue and cancel it again -- without ever
touching the real venue, without retrying an ambiguous vendor answer, and without an authentication
header ever being forwarded to a host nobody asked for.

STATUS (2026-09-27): this script has never been executed against Kiwoom's mock API. The CEO has not yet
applied for Kiwoom mock (모의투자) trading or received a mock app key/secret key (09-27 morning decision
sheet, item 37: "키움 모의투자 신청 + 모의 앱키 받기 ... 허용 IP는 dev 주문 서버 IP"). Do not pass
--execute until that key exists, its IP allowlist includes the calling host, and someone has read the
guard list below. --dry-run (the default -- no flag needed) only prints the plan: it never reads a
credential and never opens a socket.

This script adds no new order logic. Every vendor call goes through the broker adapter's own public
methods (get_quote, get_rules, place_order, cancel_order, list_orders); only the HTTP transport
(client_factory) is supplied here, to add a redirect guard the adapter's default transport does not have
(see "리다이렉트" below). The adapter itself is not modified.

Fixed by design, never a CLI flag:
    - symbol:   005930 (Samsung Electronics, KRX) -- liquid, so a lower-limit bid is a realistic, cheap
                probe and its price band is well defined.
    - quantity: 1 share.
Changing either is a deliberate follow-up edit to this file, not something a caller should be able to do
by accident from the command line.

Usage:
    python examples/kiwoom_mock_roundtrip.py             # --dry-run (default): prints the plan, no I/O
    python examples/kiwoom_mock_roundtrip.py --execute    # makes real calls to the Kiwoom MOCK host only

Required environment for --execute (read by name only; never printed, logged, or placed in an exception):
    KIWOOM_MOCK_APP_KEY     Kiwoom mock (모의투자) app key
    KIWOOM_MOCK_SECRET_KEY  Kiwoom mock secret key
    KIWOOM_MOCK_ACCOUNT     Optional local label for this run. Not sent to Kiwoom -- TR bodies carry no
                             account field (the account is implied by the token) -- used only for this
                             SDK's own idempotency/rate-limit scoping, and only ever shown masked.

Guards (see ~/coding/q-gateway-main-0925/plans/kiwoom_roundtrip_report.md for the full write-up):
    host       Refuses to build a client unless the target host is exactly the documented mock host
               (mockapi.kiwoom.com); refuses outright if it is the documented real host
               (api.kiwoom.com). Checked twice: once against the adapter's imported constants before
               anything else runs, and again against the literal base_url the adapter hands the
               transport at construction time.
    redirect   The transport this script injects never follows a 3xx. The adapter's own default
               transport (`pykbroker.brokers.kiwoom._KiwoomRestClient`, built on
               `urllib.request.urlopen`) does follow redirects and resends every header -- including
               Authorization -- to the new location; that is decision-sheet item 28 (09-27) and is
               *not* fixed here, because fixing the adapter's default transport is a security change
               that needs the CEO's sign-off first. This script simply never uses that default
               transport, for its own calls only.
    unknown/1999   One POST per step, never repeated. A vendor answer this script cannot read as
               either "accepted" or "explicitly refused" (an unclear return_code, 1999, a transport
               failure, a symbol/quantity mismatch) aborts the whole round trip immediately as
               "수동 확인 필요" (manual check needed) -- it never retries, and it never guesses.
    fixed inputs   Symbol and quantity are constants in this file, not CLI-settable (see above).
    credentials    Read from the environment by name only (KIWOOM_MOCK_APP_KEY / _SECRET_KEY /
               _ACCOUNT); never interpolated into a print, a log line, or an exception message. The
               account label is masked in every output. The order id is reported only by its last 4
               digits.
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Mapping
from typing import Any

from pykbroker.brokers.kiwoom import KIWOOM_API_BASE_URL, KIWOOM_MOCK_BASE_URL, Kiwoom
from pykbroker.exceptions import BrokerError, BrokerRejectedError, SpecUnverifiedError
from pykbroker.models import CancelRequest, OrderRequest
from pykbroker.safety.gate import OrderSafety
from pykbroker.safety.idempotency import MemoryIdempotencyStore
from pykbroker.safety.kill_switch import KillSwitch
from pykbroker.safety.rate_limit import RateLimiter
from pykbroker.safety.risk_caps import MemoryRiskLedger, RiskLimits

# --- Fixed inputs (see module docstring: never exposed as CLI flags). ---
SYMBOL = "005930"
QUANTITY = 1

# --- Host guard: independent of whatever the adapter's constants say at import time. ---
EXPECTED_MOCK_HOST = "mockapi.kiwoom.com"
EXPECTED_REAL_HOST = "api.kiwoom.com"

APP_KEY_ENV = "KIWOOM_MOCK_APP_KEY"
SECRET_KEY_ENV = "KIWOOM_MOCK_SECRET_KEY"
ACCOUNT_ENV = "KIWOOM_MOCK_ACCOUNT"
DEFAULT_ACCOUNT = "kiwoom-mock-roundtrip"


class RoundTripAbort(Exception):
    """Stop the round trip. `reason` is safe to print: never build one from a vendor body or a secret."""

    def __init__(self, reason: str, *, manual_check: bool) -> None:
        super().__init__(reason)
        self.reason = reason
        self.manual_check = manual_check


def _last4(order_id: str) -> str:
    return order_id[-4:] if len(order_id) > 4 else order_id


def _mask(value: str) -> str:
    return "***" + (value[-4:] if len(value) > 4 else "")


def _dumps(value: dict) -> bytes:
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


def _loads(value: bytes) -> dict:
    decoded = json.loads(value.decode("utf-8"))
    if not isinstance(decoded, dict):
        raise ValueError("Kiwoom response must be a JSON object")
    return decoded


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Never build a redirected request.

    Returning None from `redirect_request` makes the base handler raise HTTPError(code) for the 3xx
    instead of following it (see cpython's `urllib.request.HTTPRedirectHandler.http_error_302`). That
    HTTPError is not one of the (KeyError, TypeError, ValueError, AttributeError) types the adapter's
    `BaseBroker._call` reads as InvalidResponseError, so it surfaces as TransportError -- "outcome may
    be unknown, reconcile before retrying" -- exactly the honest answer for a redirect nobody followed.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102, ANN001, ANN201
        return None


class _NoRedirectClient:
    """Same narrow `.post(path, *, headers, json) -> (body, headers)` interface as the adapter's own
    `_KiwoomRestClient`, plus: refuse to follow a redirect, and refuse to talk to any host but the one
    it was built for."""

    def __init__(self, base_url: str, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._opener = urllib.request.build_opener(_NoRedirectHandler)

    def post(self, path: str, *, headers: dict, json: dict) -> tuple[dict, dict[str, str]]:
        request = urllib.request.Request(f"{self.base_url}{path}", data=_dumps(json), headers=headers, method="POST")
        with self._opener.open(request, timeout=self._timeout) as response:
            return _loads(response.read()), {key.lower(): value for key, value in response.headers.items()}


def _guarded_client_factory(*, base_url: str, timeout: float = 30.0) -> _NoRedirectClient:
    host = urllib.parse.urlparse(base_url).hostname or ""
    if host == EXPECTED_REAL_HOST:
        raise RoundTripAbort(f"base_url is the REAL Kiwoom host ({EXPECTED_REAL_HOST}); refusing", manual_check=True)
    if host != EXPECTED_MOCK_HOST:
        raise RoundTripAbort(
            f"base_url host {host!r} is not the documented mock host ({EXPECTED_MOCK_HOST}); refusing",
            manual_check=True,
        )
    return _NoRedirectClient(base_url=base_url, timeout=timeout)


def _safety() -> OrderSafety:
    """Mock-account safety wiring: generous caps (this is paper money), but every gate still runs."""
    return OrderSafety(
        dry_run=False,
        limits=RiskLimits(allow_orders=True, dry_run=False, per_order_krw_cap="100000000", daily_krw_cap="100000000"),
        risk_ledger=MemoryRiskLedger(),
        idempotency=MemoryIdempotencyStore(),
        kill_switch=KillSwitch(lambda broker, account: False),
    )


def run(
    *,
    execute: bool,
    env: Mapping[str, str] | None = None,
    client_factory: Callable[..., Any] | None = None,
    rate_limiter: RateLimiter | None = None,
) -> dict:
    """Run the plan (execute=False) or the real round trip (execute=True). Never raises: every stop is
    recorded in the returned dict's "status"/"reason" fields instead."""
    env = env if env is not None else os.environ
    result: dict[str, Any] = {"mode": "execute" if execute else "dry_run", "symbol": SYMBOL, "quantity": QUANTITY}
    steps: list[dict[str, Any]] = []
    result["steps"] = steps
    broker = None

    def step(name: str, ok: bool, **fields: Any) -> None:
        steps.append({"name": name, "ok": ok, **fields})

    try:
        # Guard: host, before anything else -- no credential is read and no client is built yet.
        if (
            KIWOOM_MOCK_BASE_URL != f"https://{EXPECTED_MOCK_HOST}"
            or KIWOOM_API_BASE_URL != f"https://{EXPECTED_REAL_HOST}"
        ):
            step("host_check", False)
            raise RoundTripAbort(
                f"adapter host constants do not match the documented mock/real pair "
                f"(mock={KIWOOM_MOCK_BASE_URL!r}, real={KIWOOM_API_BASE_URL!r}); refusing to run",
                manual_check=True,
            )
        step("host_check", True, mock_host=KIWOOM_MOCK_BASE_URL)
        result["host"] = KIWOOM_MOCK_BASE_URL

        if not execute:
            result["status"] = "dry_run"
            result["would_do"] = [
                "issue_token (au10001, mock host)",
                f"get_quote({SYMBOL}) -- record whether a signed price is refused (SpecUnverifiedError)",
                f"get_rules({SYMBOL}) -- read the daily lower limit (하한가) and tick",
                f"place_order(buy {QUANTITY} {SYMBOL} @ 하한가, limit)",
                "list_orders -- confirm accepted and still open (not filled)",
                "cancel_order",
                "list_orders -- confirm no longer open",
            ]
            return result

        app_key, secret_key = env.get(APP_KEY_ENV), env.get(SECRET_KEY_ENV)
        if not app_key or not secret_key:
            raise RoundTripAbort(f"{APP_KEY_ENV} / {SECRET_KEY_ENV} are not set", manual_check=True)
        account = env.get(ACCOUNT_ENV) or DEFAULT_ACCOUNT
        result["account"] = _mask(account)
        factory = client_factory or _guarded_client_factory

        try:
            issued = Kiwoom.issue_token(app_key=app_key, secret_key=secret_key, virtual=True, client_factory=factory)
        except RoundTripAbort:
            raise
        except BrokerError as exc:
            step("issue_token", False, error=type(exc).__name__)
            raise RoundTripAbort(f"token issuance failed ({type(exc).__name__})", manual_check=True) from None
        step("issue_token", True, expires_dt=issued.get("expires_dt"))

        broker = Kiwoom(
            account=account,
            client_options={**issued, "app_key": app_key},
            market="KR",
            virtual=True,
            safety=_safety(),
            client_factory=factory,
            rate_limiter=rate_limiter,
        )

        try:
            quote = broker.get_quote(SYMBOL, market="KR")
        except SpecUnverifiedError:
            step("read_quote", False, error="SpecUnverifiedError", minus_sign_seen=True)
            raise RoundTripAbort(
                "현재가에 '-' 부호가 있어 어댑터가 거부(SpecUnverifiedError) -- 부호 의미 미확정", manual_check=True
            ) from None
        except RoundTripAbort:
            raise
        except BrokerError as exc:
            step("read_quote", False, error=type(exc).__name__)
            raise RoundTripAbort(f"quote read failed ({type(exc).__name__})", manual_check=True) from None
        step(
            "read_quote",
            True,
            minus_sign_seen=False,
            last=str(quote.last),
            bid=str(quote.bid) if quote.bid is not None else None,
            ask=str(quote.ask) if quote.ask is not None else None,
        )
        result["price_sign_note"] = (
            "this read carried no '-' prefix; the sign's meaning (direction vs. value) is still not "
            "fully settled -- recheck on a day the stock is down, per plans/kiwoom_rest_spec.md §부호"
        )

        try:
            rules = broker.get_rules(SYMBOL)
        except SpecUnverifiedError:
            step("read_rules", False, error="SpecUnverifiedError", minus_sign_seen=True)
            raise RoundTripAbort(
                "하한가/상한가에 '-' 부호가 있어 어댑터가 거부 -- 수동 확인 필요", manual_check=True
            ) from None
        except RoundTripAbort:
            raise
        except BrokerError as exc:
            step("read_rules", False, error=type(exc).__name__)
            raise RoundTripAbort(f"rules read failed ({type(exc).__name__})", manual_check=True) from None
        if rules.limit_down is None or rules.tick is None:
            step("read_rules", False, reason="no daily lower limit or tick reported")
            raise RoundTripAbort("하한가 또는 호가단위를 알 수 없음 -- 수동 확인 필요", manual_check=True)
        if rules.limit_down % rules.tick != 0:
            step("read_rules", False, reason="limit_down not on a documented tick")
            raise RoundTripAbort("하한가가 명세상 호가단위와 맞지 않음 -- 수동 확인 필요", manual_check=True)
        order_price = rules.limit_down
        step("read_rules", True, limit_down=str(order_price), limit_up=str(rules.limit_up), tick=str(rules.tick))

        request = OrderRequest(
            symbol=SYMBOL,
            side="buy",
            quantity=QUANTITY,
            order_type="limit",
            price=order_price,
            market="KR",
            client_key=f"kiwoom-mock-rt-{uuid.uuid4().hex}",
        )
        try:
            order = broker.place_order(request)
        except BrokerRejectedError:
            step("place_order", False, error="BrokerRejectedError")
            raise RoundTripAbort("주문이 명시적으로 거부됨 (수동 확인 불필요)", manual_check=False) from None
        except RoundTripAbort:
            raise
        except BrokerError as exc:
            step("place_order", False, error=type(exc).__name__)
            raise RoundTripAbort(f"주문 결과 불명({type(exc).__name__}) -- 수동 확인 필요", manual_check=True) from None
        step("place_order", True, order_id_last4=_last4(order.id), status=order.status, price=str(order_price))
        result["order_id_last4"] = _last4(order.id)

        try:
            open_orders = broker.list_orders(SYMBOL)
        except RoundTripAbort:
            raise
        except BrokerError as exc:
            step("confirm_open", False, error=type(exc).__name__)
            raise RoundTripAbort(
                f"미체결 조회 실패({type(exc).__name__}) -- 수동 확인 필요", manual_check=True
            ) from None
        mine = next((o for o in open_orders if o.id == order.id), None)
        if mine is None:
            step("confirm_open", False, reason="order not in open list")
            raise RoundTripAbort(
                "주문이 미체결 목록에 없음(체결됐거나 상태 불명) -- 취소 시도 안 함, 수동 확인 필요", manual_check=True
            )
        step("confirm_open", True, status=mine.status, remaining=mine.remaining)

        cancel_request = CancelRequest(
            order_id=order.id, symbol=SYMBOL, quantity=QUANTITY, client_key=f"kiwoom-mock-rt-cancel-{uuid.uuid4().hex}"
        )
        try:
            cancelled = broker.cancel_order(cancel_request)
        except BrokerRejectedError:
            step("cancel", False, error="BrokerRejectedError")
            raise RoundTripAbort(
                "취소가 명시적으로 거부됨 -- 주문이 아직 열려 있을 수 있음, 수동 확인 필요", manual_check=True
            ) from None
        except RoundTripAbort:
            raise
        except BrokerError as exc:
            step("cancel", False, error=type(exc).__name__)
            raise RoundTripAbort(f"취소 결과 불명({type(exc).__name__}) -- 수동 확인 필요", manual_check=True) from None
        step("cancel", True, status=cancelled.status)

        try:
            after = broker.list_orders(SYMBOL)
        except RoundTripAbort:
            raise
        except BrokerError as exc:
            step("confirm_cancel", False, error=type(exc).__name__)
            raise RoundTripAbort(
                f"취소 후 조회 실패({type(exc).__name__}) -- 수동 확인 필요", manual_check=True
            ) from None
        still_open = any(o.id == order.id for o in after)
        step("confirm_cancel", not still_open, still_listed=still_open)
        if still_open:
            raise RoundTripAbort("취소 뒤에도 미체결 목록에 남아 있음 -- 수동 확인 필요", manual_check=True)

        result["status"] = "ok"
    except RoundTripAbort as exc:
        result["status"] = "manual_check_needed" if exc.manual_check else "rejected"
        result["reason"] = exc.reason
    except Exception as exc:  # noqa: BLE001 -- last resort: never crash with a raw traceback, never guess
        result["status"] = "manual_check_needed"
        result["reason"] = f"unexpected failure ({type(exc).__name__}) -- 수동 확인 필요"
    finally:
        if broker is not None:
            broker.close()
    return result


def main(
    argv: list[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    client_factory: Callable[..., Any] | None = None,
) -> int:
    parser = argparse.ArgumentParser(description="Kiwoom mock order -> cancel round trip (verification only)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--execute", action="store_true", help="Make real calls to the Kiwoom MOCK host")
    mode.add_argument("--dry-run", action="store_true", help="Print the plan only (default)")
    args = parser.parse_args(argv)

    result = run(execute=args.execute, env=env, client_factory=client_factory)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["status"] == "manual_check_needed":
        return 2
    if result["status"] == "rejected":
        return 3
    return 0 if result["status"] in ("ok", "dry_run") else 1


if __name__ == "__main__":
    raise SystemExit(main())
