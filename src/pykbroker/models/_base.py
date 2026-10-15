from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

Money = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
PositiveMoney = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
Quantity = Annotated[int, Field(ge=0, strict=True)]
PositiveQuantity = Annotated[int, Field(gt=0, strict=True)]
# Fractional share quantity (Toss US MARKET SELL only). Strict Decimal: an int stays
# PositiveQuantity, a float or str is refused — nothing is ever truncated into an int.
FractionalQuantity = Annotated[Decimal, Field(gt=0, strict=True, allow_inf_nan=False)]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
