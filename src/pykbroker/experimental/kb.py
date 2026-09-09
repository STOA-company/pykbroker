"""Offline KB skeleton; endpoint/TR details have not been obtained."""

from pykbroker.brokers._common import BaseBroker
from pykbroker.exceptions import NotSupportedError, SpecUnverifiedError
from pykbroker.models import CancelRequest, Order, OrderRequest

KB_SPEC_VERIFIED = False


class KB(BaseBroker):
    broker_code = "kb"

    def __init__(self, *, account: str, market: str = "KR", **kwargs) -> None:
        if market != "KR":
            raise NotSupportedError("KB skeleton is domestic only")
        super().__init__(account=account, market=market, **kwargs)

    def place_order(self, request: OrderRequest) -> Order:
        if KB_SPEC_VERIFIED is not True:
            raise SpecUnverifiedError("KB_SPEC_VERIFIED is not True")
        raise NotSupportedError("KB endpoint/TR contract has not been obtained")

    def cancel_order(self, request: CancelRequest) -> Order:
        if KB_SPEC_VERIFIED is not True:
            raise SpecUnverifiedError("KB_SPEC_VERIFIED is not True")
        raise NotSupportedError("KB endpoint/TR contract has not been obtained")

    # TODO: obtain official endpoint/TR schemas before adding any transport or parsing.
