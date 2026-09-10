"""A′2-5: the contract change stays additive, the lock stays untouched, and `[dev]` installs.

Streams B and B′ import this package as their stock exit. Anything they already call has
to keep binding, or A′ lands as a break disguised as a feature. Three things are checked
here because a reviewer had to find each of them by hand:

* every A′ addition to `base.py` is additive — new methods, and new parameters that are
  keyword-only with defaults, so a pre-A′ call site still binds;
* `experimental/_rest.py` takes no mock-account argument — the lock is not a side door,
  and A′ has no business reaching into it;
* `pip install -e ".[dev]"` can actually run the suite it ships.
"""

import inspect
import tomllib
from pathlib import Path

import pytest

from pykbroker.base import BrokerAdapter, StockBroker
from pykbroker.brokers.dbfi import DBFI
from pykbroker.brokers.kis import KIS
from pykbroker.brokers.toss import Toss
from pykbroker.experimental._rest import RestBroker

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"
# What B/B′ already call, from before A′ touched the contract.
PRE_A = {
    StockBroker: "get_quote get_balance get_positions list_orders get_rules capabilities "
    "place_order cancel_order close".split(),
    BrokerAdapter: "quote balance positions rules place cancel status".split(),
}


def _extras(name):
    return tomllib.loads(PYPROJECT.read_text())["project"]["optional-dependencies"][name]


# --- additive, so B/B′ keep binding --------------------------------------


@pytest.mark.parametrize("protocol,names", PRE_A.items(), ids=lambda value: getattr(value, "__name__", ""))
def test_no_pre_a_method_was_removed_or_renamed(protocol, names):
    for name in names:
        assert hasattr(protocol, name), name


@pytest.mark.parametrize("broker_type", [KIS, DBFI, Toss])
@pytest.mark.parametrize("method", ["get_rules", "rules"])
def test_the_new_rules_parameters_are_keyword_only_with_defaults(broker_type, method):
    """A pre-A′ caller says rules(symbol); the clock and the calendar must stay optional."""
    parameters = inspect.signature(getattr(broker_type, method)).parameters
    for name in ("now", "trading_day"):
        parameter = parameters[name]
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY, name
        assert parameter.default is None, name
    positional = [p for p in parameters.values() if p.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD]
    assert [p.name for p in positional] == ["self", "symbol"]


@pytest.mark.parametrize("broker_type", [KIS, DBFI, Toss])
def test_a_pre_a_call_site_still_binds(broker_type):
    inspect.signature(getattr(broker_type, "rules")).bind(object(), "005930")


# --- the experimental lock was not touched --------------------------------


@pytest.mark.parametrize("argument", ["virtual", "halt_provider"])
def test_the_locked_rest_mapping_takes_no_mock_account_argument(argument):
    """Bypass 1: A′ must not widen an unverified mapping's constructor on its way past."""
    assert argument not in inspect.signature(RestBroker.__init__).parameters


def test_the_locked_rest_mapping_never_claims_a_mock_domain():
    """Bypass 2: whatever the constructor accepts, the lock declares no mock account."""
    assert RestBroker.supports_virtual is False
    with pytest.raises(TypeError):
        RestBroker(account="fixture", virtual=True)


# --- the shipped dev extra installs what the suite imports ----------------


def test_the_dev_extra_installs_the_mcp_package_the_suite_imports():
    """`pip install -e ".[dev]"` alone left tests/test_mcp_registration.py uncollectable."""
    requirement = next((item for item in _extras("dev") if item.split("[")[0].strip() == "mcp"), None)
    assert requirement is not None, _extras("dev")
    assert "<2" in requirement


def test_the_dev_extra_agrees_with_the_optional_mcp_extra():
    dev = next(item for item in _extras("dev") if item.split("[")[0].strip() == "mcp")
    assert dev in _extras("mcp")
