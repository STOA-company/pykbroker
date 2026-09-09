"""Execution SDK typing contract only. No scheduler, dispatcher, or resume logic.

See docs/execution-sdk-contract.md for semantics and obligations.
"""

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Literal, NotRequired, Protocol, TypedDict

Market = Literal["KR", "US", "COIN"]
Side = Literal["buy", "sell"]
State = Literal["open", "partial", "filled", "cancelled", "rejected"]
RejectionReason = Literal[
    "kill_switch", "dry_run", "not_owner", "cap_exceeded", "price_band", "session_closed", "rate_limit", "duplicate"
]


class Clarify(Exception):
    """Missing build/dry-run input; ask the caller the question."""

    question: str


class Rejected(Exception):
    """Intent refused; uncaught rejection terminates the execution."""

    reason: RejectionReason


class ExecutionEnded(Exception):
    """Close, runtime limit, or caller cancellation; executor cancels open intents."""


class Intent(TypedDict):
    execution_id: str
    seq: int
    account_id: str
    ticker: str
    market: Market
    venue: NotRequired[str]
    side: Side
    qty: int | float
    kind: Literal["limit", "market"]
    price: float | None
    ttl_sec: int | None
    client_key: str


class AcceptedIntent(TypedDict):
    intent_id: str
    status: Literal["accepted"]
    broker_order_id: str


class RejectedIntent(TypedDict):
    status: Literal["rejected"]
    reason: RejectionReason


class Account(Protocol):
    @property
    def account_id(self) -> str:
        raise NotImplementedError

    @property
    def broker(self) -> str:
        raise NotImplementedError

    @property
    def cash_available(self) -> float:
        raise NotImplementedError

    @property
    def holdings(self) -> Mapping[str, int | float]:
        raise NotImplementedError


class Symbol(Protocol):
    @property
    def code(self) -> str:
        raise NotImplementedError

    @property
    def name(self) -> str:
        raise NotImplementedError

    @property
    def market(self) -> Market:
        raise NotImplementedError

    @property
    def venue(self) -> str | None:
        raise NotImplementedError

    @property
    def tick(self) -> float:
        raise NotImplementedError

    @property
    def lot(self) -> float:
        raise NotImplementedError

    @property
    def min_notional(self) -> float | None:
        raise NotImplementedError


class Bar(Protocol):
    @property
    def t(self) -> datetime:
        raise NotImplementedError

    @property
    def o(self) -> float:
        raise NotImplementedError

    @property
    def h(self) -> float:
        raise NotImplementedError

    @property
    def l(self) -> float:  # noqa: E743 - OHLCV contract field
        raise NotImplementedError

    @property
    def c(self) -> float:
        raise NotImplementedError

    @property
    def v(self) -> float:
        raise NotImplementedError


class Session(Protocol):
    @property
    def open(self) -> datetime:
        raise NotImplementedError

    @property
    def close(self) -> datetime:
        raise NotImplementedError

    @property
    def is_open(self) -> bool:
        raise NotImplementedError


class Status(Protocol):
    @property
    def state(self) -> State:
        raise NotImplementedError

    @property
    def filled_qty(self) -> int:
        raise NotImplementedError

    @property
    def avg_price(self) -> float:
        raise NotImplementedError


class ExecutionSnapshot(Protocol):
    @property
    def intents(self) -> tuple[AcceptedIntent | RejectedIntent, ...]:
        raise NotImplementedError

    @property
    def filled_qty(self) -> int:
        raise NotImplementedError

    @property
    def avg_price(self) -> float:
        raise NotImplementedError

    @property
    def remaining(self) -> int:
        raise NotImplementedError


class TraderQuote(Protocol):
    @property
    def last(self) -> float:
        raise NotImplementedError

    @property
    def bid1(self) -> float:
        raise NotImplementedError

    @property
    def ask1(self) -> float:
        raise NotImplementedError

    @property
    def prev_close(self) -> float:
        raise NotImplementedError

    @property
    def vwap(self) -> float:
        raise NotImplementedError


class Order(Protocol):
    def status(self) -> Status:
        raise NotImplementedError

    def wait(self, timeout: str) -> Status:
        raise NotImplementedError


class ExecutionContext(Protocol):
    @property
    def account(self) -> Account:
        raise NotImplementedError

    @property
    def symbol(self) -> Symbol:
        raise NotImplementedError

    @property
    def side(self) -> Side:
        raise NotImplementedError

    def target_qty(self) -> int:
        raise NotImplementedError

    def remaining(self) -> int:
        raise NotImplementedError

    def filled_qty(self) -> int:
        raise NotImplementedError

    def filled_avg(self) -> float:
        raise NotImplementedError

    def qty_for_krw(self, krw: int, price: float) -> int:
        raise NotImplementedError

    def split(self, total: int, n: int, i: int) -> int:
        raise NotImplementedError

    def round_price(self, price: float) -> int:
        raise NotImplementedError

    def held(self) -> int:
        raise NotImplementedError

    def last(self) -> float:
        raise NotImplementedError

    def best_bid(self) -> float:
        raise NotImplementedError

    def best_ask(self) -> float:
        raise NotImplementedError

    def prev_close(self) -> float:
        raise NotImplementedError

    def vwap_today(self) -> float:
        raise NotImplementedError

    def bars(self, n: int) -> list[Bar]:
        raise NotImplementedError

    def now(self) -> datetime:
        raise NotImplementedError

    def session(self) -> Session:
        raise NotImplementedError

    def wait(self, duration: str) -> None:
        raise NotImplementedError

    def wait_until(self, hhmm: str) -> None:
        raise NotImplementedError

    def wait_for(self, pred: Callable[[], bool], timeout: str) -> None:
        raise NotImplementedError

    def place_limit(self, qty: int, price: float, ttl: str = "5m") -> Order:
        raise NotImplementedError

    def place_market(self, qty: int) -> Order:
        raise NotImplementedError

    def cancel(self, order: Order) -> None:
        raise NotImplementedError

    def cancel_open(self) -> None:
        raise NotImplementedError

    def note(self, text: str) -> None:
        raise NotImplementedError

    def done(self, reason: str) -> None:
        raise NotImplementedError


class Trader(Protocol):
    def submit_intent(self, intent: Intent) -> AcceptedIntent | RejectedIntent:
        raise NotImplementedError

    def cancel_intent(self, intent_id: str) -> None:
        raise NotImplementedError

    def get_execution(self, execution_id: str) -> ExecutionSnapshot:
        raise NotImplementedError

    def get_quote(self, ticker: str) -> TraderQuote:
        raise NotImplementedError
