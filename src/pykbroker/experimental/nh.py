"""Offline NH skeleton; endpoint/TR details have not been obtained."""

from pykbroker.brokers._common import BaseBroker
from pykbroker.exceptions import NotSupportedError, SpecUnverifiedError
from pykbroker.models import CancelRequest, Order, OrderRequest

NH_SPEC_VERIFIED = False


class NH(BaseBroker):
    broker_code = "nh"

    def __init__(self, *, account: str, market: str = "KR", **kwargs) -> None:
        if market != "KR":
            raise NotSupportedError("NH skeleton is domestic only")
        super().__init__(account=account, market=market, **kwargs)

    def place_order(self, request: OrderRequest) -> Order:
        if NH_SPEC_VERIFIED is not True:
            raise SpecUnverifiedError("NH_SPEC_VERIFIED is not True")
        raise NotSupportedError("NH endpoint/TR contract has not been obtained")

    def cancel_order(self, request: CancelRequest) -> Order:
        if NH_SPEC_VERIFIED is not True:
            raise SpecUnverifiedError("NH_SPEC_VERIFIED is not True")
        raise NotSupportedError("NH endpoint/TR contract has not been obtained")

    # TODO: obtain official endpoint/TR schemas before adding any transport or parsing.
