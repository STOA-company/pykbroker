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
    "dbfi": {
        # HTTP 500 + rsp_cd IGW00121: Pydb re-issues the token and raises (base.py:88-98).
        "IGW00121": RejectCode(TOKEN_EXPIRED, "token_expired",
                               "Pydb/pydbfi/service/common/base.py:89 «token 유효성 만료»"),
    },
}


# ---------------------------------------------------------------------------------------------
# Saxo OpenAPI (public docs, read 2026-10-27). Only «not accepted» facts are listed; every code that
# is pending / timed out / unspecified (OrderCommandPending, OrderCommandTimeout, CouldNotCompleteRequest,
# OtherError, BrokerOption, AlreadyPendingCancelReplace, TradeNotCompleted), HTTP 409 (duplicate within
# 15 s), 202, 5xx and any code not listed stays UNKNOWN: never turned into a rejection.
# ---------------------------------------------------------------------------------------------
_SAXO_REF = "https://www.developer.saxo/openapi/referencedocs/trade/v2/orders/"
_SAXO_POST = """
ActiveFollowerCannotDoManualTrade AlgoOrderTimeBeforeExchangeOpen AmountBelowMinimumLotSize
AmountCannotBeLessThanFilledAmount AmountNotInLotSize BlocksExistForOwnedContracts
BreakoutUpPriceCannotBeLessThanDownPrice CashAmountLessThanMinInitialInvestmentAmount ClientCountrySanctioned
ClientExposureLimitation ClientNotEnabledForExtendedTradingHours ClientOnReduceForcedExposureReductionViolation
ClosingPositionNotAllowedForMultiDayExecutionOrder ClosingPositionNotAllowedOnJointExecutionModel
ConditionDurationNotSupported ConditionOnWrongSideOfMarket ConditionPriceExceedsAggressiveTolerance
ConditionTooFarFromMarket ContactExposureLimitation CrossCurrencyOrderOnMarginLendingAccountNotAllowed
DealCaptureAllocationKeyNotCorrect DealCaptureInformationNotCorrect DurationNotSupported ExpirationDateInPast
ExpirationDateRequired ExplicitCloseNotAllowedForIntradayNetting ExtendedHoursTradingCannotBeChanged
ForceOpenNotAllowed ForcedExposureReductionViolation ForwardDateInPast ForwardDateRequired
GtdOrderCannotBeLaterThanExpiry IllegalAccount IllegalAmount IllegalAssetType IllegalDate IllegalInstrumentId
IllegalRequest IllegalStrike IncorrectValuationTypeAndNettingMethod InstrumentDisabledForTrading
InstrumentForcedExposureReductionViolation InstrumentHasExpired InstrumentNotAllowed
InstrumentNotSupportedForExtendedHours InsufficientCash InsufficientCash_CausedByOvernightAddOn
InvalidAllocationKeyUsed InvalidModelState InvalidOrderRelation InvalidRequest InvalidUic InvalidValueDate
LimitTooFarFromStop MarketClosed NotOwned NotPrimarySession NotSuitable NotTradableAtPresent OnWrongSideOfMarket
OnlyLimitOrderAllowedForExtendedHours OnlySidedReductionAllowed OptionExerciseAfterCutoff
OptionExerciseNotAllowedDueExDateOfUpcomingCorporateActionOnInstrument OrderExceedsMarketLimitUpDownTolerance
OrderNotFound OrderNotPlaced OrderNotSupportedForAccountType OrderPriceOutsideLimit OrderRejectedByBroker
OrderRelatedPositionIsClosed OrderRelatedPositionMissMatch OrderRequestAfterLastTradingDate
OrderSizeGreaterThanMaximumAllowed OrderTypeNotSupported OrderValueToSmall OrderValueTooLarge
PriceExceedsAggressiveTolerance PriceHasMoved PriceNotInTickSizeIncrements
RelatedOrClosingOrderNotAllowedForIntradayNettingClients RelatedOrderNotAllowedForInstrument
RelatedOrderNotAllowedForIpo RelatedOrderNotAllowedForShortLived RelatedOrderNotAllowedOnChildOrder
RelatedOrderNotAllowedOnMultiLegPartialFilled RelatedOrderNotAllowedOnTrailingStopOrder
RelatedPositionAlreadyHasImplicitClosingOrders RelatedPositionAlreadyHasRelatedOrderType
RelatedPositionAlreadyHasRelatedOrders RelatedPositionNotFound RepeatTradeOnAutoQuote
ReservedByShortContractOptions SellExceedsSettledExposure SellOrdersAlreadyExistForOwnedContracts
ShortTradeDisabled ToOpenNotAllowedOnInstrument TooCloseToMarket TooCloseToOcoRelatedOrderPrice
TooFarFromEntryOrder TooFarFromMarket TradingDisabled WouldComeTooCloseToAccountValueProtectionLimit
WouldExceedAccountValueProtectionLimit WouldExceedMargin WouldExceedMarginCeiling WouldExceedMaxCreditLine
WouldExceedMaxCreditLineLimit WouldExceedTradingLine WrongSideOfRelatedOrder"""
_SAXO_PATCH = """
ActiveFollowerCannotChangeOrderManually AlreadyPendingCancelReplace AmendmentsNotSupportedInBreakSession
AmountChangeNotAllowedWhenAmountIsInAllocationUnitKey IllegalOrderTypeChange LimitOrderHigherStopOnSell
LimitOrderLowerThenStopOnBuy OrderCannotBeChangedAtThisTime StopOrderLowerLimitOnSell TooLateToChangeOrder"""
_SAXO_DELETE = """
ActiveFollowerCannotCancelOrderManually ClientCannotCancelStopOutOrder ClientCannotChangeAllocatedOrder
OrderCannotBeCancelledAtThisTime TooLateToCancelOrder"""


_SAXO_UNKNOWN = ("OrderCommandPending", "OrderCommandTimeout", "CouldNotCompleteRequest", "OtherError",
                 "BrokerOption", "AlreadyPendingCancelReplace", "TradeNotCompleted")


def _saxo_table() -> dict[str, RejectCode]:
    table: dict[str, RejectCode] = {}
    for names, page in ((_SAXO_POST, "post__trade"), (_SAXO_PATCH, "patch__trade"),
                        (_SAXO_DELETE, "delete__trade__orderids"), ("AllocationKeyDoesNotMatchAccount",
                                                                    "post__trade__precheck")):
        for code in names.split():
            table.setdefault(code, RejectCode(REJECTED, "business_rejected", _SAXO_REF + page))
    for code in _SAXO_UNKNOWN:  # listed in the docs but they do not prove «not accepted»
        table.pop(code, None)
    # HTTP status rows (key = «HTTP» + status): the gateway answered before processing the order.
    table["HTTP401"] = RejectCode(
        TOKEN_EXPIRED, "token_expired",
        _SAXO_REF + "post__trade «401 Unauthorized. Missing or invalid Authorization header or security token»")
    table["HTTP429"] = RejectCode(
        RATE_LIMITED, "rate_limited", "https://www.developer.saxo/openapi/learn/rate-limiting «429 - Too Many Requests»")
    return table


DEFINITIVE_REJECTS["saxo"] = _saxo_table()


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
