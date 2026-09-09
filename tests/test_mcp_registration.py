import json
from decimal import Decimal
from unittest.mock import Mock

import pytest

from pykbroker.brokers._common import BaseBroker
from pykbroker.mcp.server import create_server, load_limits
from pykbroker.models import Balance, Order, Quote
from pykbroker.safety.gate import OrderSafety

READ_TOOLS = {"get_quote", "get_balance", "get_positions", "list_orders", "get_rules"}


class FakeBroker(BaseBroker):
    broker_code = "fixture"

    def __init__(self, safety):
        super().__init__(account="fixture-account", safety=safety)
        self.calls = []

    def get_quote(self, symbol, *, market="KR"):
        return Quote(symbol=symbol, last=Decimal("70000"))

    def get_balance(self):
        return Balance(cash_available=Decimal(0), position_value=Decimal(0))

    def get_positions(self):
        return []

    def list_orders(self, symbol=None):
        return []

    def _place_order(self, request):
        self.calls.append(request)
        return Order(id="fixture-order", symbol=request.symbol, status="accepted")

    def _cancel_order(self, request):
        self.calls.append(request)
        return Order(id=request.order_id, symbol=request.symbol, status="accepted")


def write_limits(path, *, dry_run="true", extra=""):
    path.write_text(
        "allow_orders = true\nper_order_krw_cap = 100000\ndaily_krw_cap = 200000\n"
        + (f"dry_run = {dry_run}\n" if dry_run is not None else "")
        + extra
    )


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        "allow_orders = false",
        "per_order_krw_cap=1",
        "allow_orders = true",
        'allow_orders = "true"',
        "allow_orders=true\nper_order_krw_cap=-1\ndaily_krw_cap=5",
        'allow_orders=true\nper_order_krw_cap=10\ndaily_krw_cap=20\ndry_run="false"',
        "invalid [toml",
    ],
)
async def test_invalid_settings_hide_mutation_tools(tmp_path, text):
    path = tmp_path / "limits.toml"
    if text is not None:
        path.write_text(text)
    factory = Mock()
    server = create_server(limits_path=path, broker_factory=factory)
    assert {tool.name for tool in await server.list_tools()} == READ_TOOLS
    factory.assert_not_called()


@pytest.mark.parametrize("dry_run", ["true", "false", None])
async def test_valid_settings_register_tools(tmp_path, dry_run):
    path = tmp_path / "limits.toml"
    write_limits(path, dry_run=dry_run)
    server = create_server(limits_path=path)
    assert {tool.name for tool in await server.list_tools()} == READ_TOOLS | {"place_order", "cancel_order"}
    if dry_run is None:
        assert load_limits(path).dry_run is True


async def test_read_tools_execute_without_limits(tmp_path):
    server = create_server(limits_path=tmp_path / "absent", broker_factory=FakeBroker)
    for tool, arguments in [
        ("get_quote", {"symbol": "005930"}),
        ("get_balance", {}),
        ("get_positions", {}),
        ("list_orders", {}),
        ("get_rules", {"symbol": "005930"}),
    ]:
        response = await server.call_tool(tool, arguments)
        content = response[0] if isinstance(response, tuple) else response
        assert content and content[0].type == "text"
        json.loads(content[0].text)


@pytest.mark.parametrize(
    "tool,args",
    [
        ("place_order", dict(symbol="005930", side="buy", quantity=1, price="70000", client_key="key")),
        ("cancel_order", dict(symbol="005930", order_id="42", quantity=1, client_key="key")),
    ],
)
async def test_mcp_default_dry_run_blocks_both(tmp_path, tool, args):
    path = tmp_path / "limits.toml"
    write_limits(path)
    made = []

    def factory(safety):
        broker = FakeBroker(safety)
        made.append(broker)
        return broker

    server = create_server(limits_path=path, broker_factory=factory)
    with pytest.raises(Exception, match="dry_run"):
        await server.call_tool(tool, args)
    assert all(not broker.calls for broker in made)


async def test_real_mcp_dispatch_caps_and_dedup_persist(tmp_path):
    path = tmp_path / "limits.toml"
    write_limits(path, dry_run="false")
    made = []

    def factory(safety):
        broker = FakeBroker(safety)
        made.append(broker)
        return broker

    args = dict(symbol="005930", side="buy", quantity=1, price="70000", client_key="stable-key")
    server = create_server(limits_path=path, broker_factory=factory)
    await server.call_tool("place_order", args)
    assert len(made[0].calls) == 1
    restarted = create_server(limits_path=path, broker_factory=factory)
    with pytest.raises(Exception, match="duplicate"):
        await restarted.call_tool("place_order", args)
    with pytest.raises(Exception, match="daily cap"):
        await server.call_tool("place_order", {**args, "client_key": "new"})
    assert sum(len(b.calls) for b in made) == 1


async def test_file_revocation_blocks_already_registered_tools(tmp_path):
    path = tmp_path / "limits.toml"
    write_limits(path, dry_run="false")
    factory = Mock(side_effect=FakeBroker)
    server = create_server(limits_path=path, broker_factory=factory)
    path.unlink()
    with pytest.raises(Exception, match="changed or were removed"):
        await server.call_tool("cancel_order", dict(order_id="42", symbol="005930", quantity=1, client_key="cancel"))
    factory.assert_not_called()


async def test_factory_cannot_replace_the_guard(tmp_path):
    path = tmp_path / "limits.toml"
    write_limits(path, dry_run="false")
    server = create_server(limits_path=path, broker_factory=lambda safety: FakeBroker(OrderSafety()))
    with pytest.raises(Exception, match="supplied safety gate"):
        await server.call_tool("get_balance", {})


async def test_injected_policy_cannot_weaken_file(tmp_path, live_safety):
    path = tmp_path / "limits.toml"
    write_limits(path, dry_run="true")
    server = create_server(limits_path=path, broker_factory=FakeBroker, safety=live_safety)
    with pytest.raises(Exception, match="match"):
        await server.call_tool("place_order", dict(symbol="005930", side="buy", quantity=1, price="1", client_key="x"))


@pytest.mark.parametrize("quantity", [True, 1.5])
async def test_mcp_rejects_non_integer_quantity(tmp_path, quantity):
    path = tmp_path / "limits.toml"
    write_limits(path, dry_run="false")
    factory = Mock(side_effect=FakeBroker)
    server = create_server(limits_path=path, broker_factory=factory)
    with pytest.raises(Exception):
        await server.call_tool(
            "place_order", dict(symbol="005930", side="buy", quantity=quantity, price="1", client_key="x")
        )
    factory.assert_not_called()


async def test_mcp_validation_error_does_not_echo_client_key(tmp_path, caplog):
    path = tmp_path / "limits.toml"
    write_limits(path, dry_run="false")
    server = create_server(limits_path=path, broker_factory=FakeBroker)
    key = "fixture-value-must-not-be-logged"
    with pytest.raises(Exception) as error:
        await server.call_tool(
            "place_order", dict(symbol="005930", side="invalid", quantity=1, price="1", client_key=key)
        )
    assert key not in str(error.value)
    assert key not in caplog.text
