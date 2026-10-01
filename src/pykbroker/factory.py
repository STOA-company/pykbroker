"""Shared CLI/MCP construction. Configuration is supplied explicitly or via env."""

import json
import os
from typing import Any

from .brokers._common import BaseBroker
from .brokers.dbfi import DBFI
from .brokers.kis import KIS
from .brokers.kiwoom import Kiwoom
from .brokers.ls import LS
from .brokers.toss import Toss
from .exceptions import ConfigurationError

BROKERS: dict[str, type[BaseBroker]] = {"kis": KIS, "dbfi": DBFI, "toss": Toss, "kiwoom": Kiwoom, "ls": LS}


def _options() -> dict[str, Any]:
    try:
        value = json.loads(os.environ.get("PYKBROKER_CLIENT_OPTIONS", "{}"))
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, TypeError):
        raise ConfigurationError("PYKBROKER_CLIENT_OPTIONS must be a JSON object") from None


def create_broker(name: str, *, account: str | None = None, **kwargs: Any) -> BaseBroker:
    key = name.lower()
    if key not in BROKERS:
        raise ConfigurationError(f"Unknown broker; choose {', '.join(sorted(BROKERS))}")
    account = account if account is not None else os.environ.get("PYKBROKER_ACCOUNT", "")
    if key == "toss":
        if "account_seq" not in kwargs:
            try:
                kwargs["account_seq"] = int(os.environ["PYKBROKER_TOSS_ACCOUNT_SEQ"])
            except (ValueError, KeyError):
                raise ConfigurationError("PYKBROKER_TOSS_ACCOUNT_SEQ is required") from None
        if "credentials_options" not in kwargs and "sdk_factory" not in kwargs:
            kwargs["credentials_options"] = _options()
    elif key != "ls" and "client_options" not in kwargs and (
        key in {"kiwoom", "nh"} or not {"client", "client_factory"} & kwargs.keys()
    ):
        kwargs["client_options"] = _options()
    return BROKERS[key](account=account, **kwargs)
