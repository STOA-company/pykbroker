import builtins
import importlib
import sys
from unittest.mock import Mock

import pytest

from pykbroker import create_broker
from pykbroker.cli import main
from pykbroker.exceptions import ConfigurationError, MissingDependencyError


def test_import_needs_no_optional_sdk(monkeypatch):
    original = builtins.__import__
    attempted = []

    def deny_vendor(name, *args, **kwargs):
        if name.split(".")[0] in {"Pykis", "Pydb", "toss_openapi"}:
            attempted.append(name)
            raise ImportError(name)
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", deny_vendor)
    import pykbroker

    importlib.reload(pykbroker)
    for name in ("kis", "dbfi", "toss"):
        importlib.reload(importlib.import_module("pykbroker.brokers." + name))
    assert attempted == []


def test_factory_lazy_and_account_explicit(monkeypatch):
    monkeypatch.delenv("PYKBROKER_ACCOUNT", raising=False)
    with pytest.raises(ConfigurationError):
        create_broker("kis")
    broker = create_broker("dbfi", account="fixture", client=Mock())
    assert broker.broker_code == "dbfi"
    with pytest.raises(ConfigurationError):
        create_broker("unknown", account="fixture")


def test_cli_read_only_help(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["pykbroker", "--help"])
    with pytest.raises(SystemExit) as result:
        main()
    assert result.value.code == 0
    text = capsys.readouterr().out
    assert "quote" in text and "place_order" not in text


def test_missing_vendor_is_actionable(monkeypatch):
    module = importlib.import_module("pykbroker.brokers.kis")
    monkeypatch.setattr(module, "import_module", Mock(side_effect=ImportError("missing")))
    with pytest.raises(MissingDependencyError, match="inject"):
        module.KIS(account="fixture").get_quote("005930")
