from importlib import import_module
from unittest.mock import Mock

import pytest

from pykbroker.exceptions import NotSupportedError, SpecUnverifiedError
from pykbroker.models import CancelRequest

CASES = [
    ("ls", "LS", "LS_SPEC_VERIFIED"),
    ("kiwoom", "Kiwoom", "KIWOOM_SPEC_VERIFIED"),
    ("nh", "NH", "NH_SPEC_VERIFIED"),
    ("kb", "KB", "KB_SPEC_VERIFIED"),
]


@pytest.mark.parametrize("module_name,class_name,constant", CASES)
@pytest.mark.parametrize("operation", ["place_order", "place", "cancel_order", "cancel"])
def test_every_mutation_is_locked(module_name, class_name, constant, operation, request_order, live_safety):
    module = import_module("pykbroker.experimental." + module_name)
    assert getattr(module, constant) is False
    broker = getattr(module, class_name)(account="fixture", safety=live_safety)
    request = (
        request_order
        if operation.startswith("place")
        else CancelRequest(order_id="42", symbol="005930", quantity=1, client_key="cancel")
    )
    with pytest.raises(SpecUnverifiedError):
        getattr(broker, operation)(request)
    assert not broker.capabilities().supports_market_order


@pytest.mark.parametrize("module_name,class_name,constant", CASES[2:])
def test_nh_kb_remain_offline_even_when_flag_changed(module_name, class_name, constant, monkeypatch, request_order):
    module = import_module("pykbroker.experimental." + module_name)
    monkeypatch.setattr(module, constant, True)
    broker = getattr(module, class_name)(account="fixture")
    with pytest.raises(NotSupportedError):
        broker.place_order(request_order)
    with pytest.raises(NotSupportedError):
        broker.get_quote("005930")


@pytest.mark.parametrize("module_name,class_name,constant", CASES[:2])
def test_flag_alone_cannot_submit(module_name, class_name, constant, monkeypatch, request_order, live_safety):
    module = import_module("pykbroker.experimental." + module_name)
    monkeypatch.setattr(module, constant, True)
    client = Mock()
    broker = getattr(module, class_name)(account="fixture", client=client, safety=live_safety)
    with pytest.raises(Exception, match="code"):
        broker.place_order(request_order)
    client.post.assert_not_called()


@pytest.mark.parametrize("module_name,class_name,constant", CASES[:2])
@pytest.mark.parametrize("side", ["buy", "sell"])
def test_reviewed_fixture_codes_choose_exact_tr(
    module_name, class_name, constant, side, monkeypatch, request_order, live_safety
):
    module = import_module("pykbroker.experimental." + module_name)
    monkeypatch.setattr(module, constant, True)
    client, limiter = Mock(), Mock()
    client.post.return_value = (
        {"rsp_cd": "00000", "CSPAT00601OutBlock2": {"OrdNo": "42"}}
        if module_name == "ls"
        else {"return_code": 0, "ord_no": "42"}
    )
    codes = {key: "fixture-" + key for key in ("buy", "sell", "limit", "cash", "condition", "member", "exchange")}
    broker = getattr(module, class_name)(
        account="fixture", client=client, codes=codes, rate_limiter=limiter, safety=live_safety
    )
    result = broker.place_order(request_order.model_copy(update={"side": side}))
    assert result.id == "42"
    tr = "CSPAT00601" if module_name == "ls" else "kt10000" if side == "buy" else "kt10001"
    limiter.wait.assert_called_once_with(tr)
    args, kwargs = client.post.call_args
    assert args == (("/stock/order",) if module_name == "ls" else ("/api/dostk/ordr",))
    assert kwargs["headers"]["tr_cd" if module_name == "ls" else "api-id"] == tr
    if module_name == "ls":
        assert kwargs["json"]["CSPAT00601InBlock1"]["BnsTpCode"] == "fixture-" + side
        assert kwargs["json"]["CSPAT00601InBlock1"]["OrdQty"] == 1
    else:
        assert kwargs["json"]["ord_qty"] == "1"
        assert kwargs["json"]["trde_tp"] == "fixture-limit"
