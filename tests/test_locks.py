from importlib import import_module
from types import SimpleNamespace
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
    kwargs = {"client": SimpleNamespace(base_url=module.LS_API_BASE_URL)} if module_name == "ls" else {}
    broker = getattr(module, class_name)(account="fixture", safety=live_safety, **kwargs)
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
    if module_name == "ls":
        client.base_url = module.LS_API_BASE_URL
    broker = getattr(module, class_name)(account="fixture", client=client, safety=live_safety)
    expected = SpecUnverifiedError if module_name == "ls" else Exception
    message = "live writes are locked" if module_name == "ls" else "code"
    with pytest.raises(expected, match=message):
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
    kwargs = {}
    if module_name == "ls":
        # UNVERIFIED: fake host/codes/response, never a broker account.
        monkeypatch.setattr(module, "LS_MOCK_BASE_URL", "https://ls-mock.invalid:8080")
        client.base_url = module.LS_MOCK_BASE_URL
        kwargs = {"virtual": True, "spec_verified": True}
    broker = getattr(module, class_name)(
        account="fixture", client=client, codes=codes, rate_limiter=limiter, safety=live_safety, **kwargs
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


@pytest.mark.parametrize("operation", ["place_order", "cancel_order"])
def test_kb_official_adapter_defaults_closed(operation, request_order, live_safety):
    from pykbroker.brokers.kb import KB, KB_API_BASE_URL
    client = Mock(base_url=KB_API_BASE_URL)
    broker = KB(account="fixture", bound_account="fixture", app_key="fake-app", token="fake-token",
                client=client, safety=live_safety)
    request = request_order if operation == "place_order" else CancelRequest(
        order_id="42", symbol="005930", quantity=1, client_key="kb-cancel")
    with pytest.raises(SpecUnverifiedError, match="disabled"):
        getattr(broker, operation)(request)
    client.post.assert_not_called()


def test_kb_uncertain_write_is_not_retried(request_order, live_safety):
    from pykbroker.brokers.kb import KB, KB_API_BASE_URL
    from pykbroker.exceptions import InvalidResponseError
    client = Mock(base_url=KB_API_BASE_URL)
    client.post.return_value = {"dataHeader": {"resultCode": "200", "processFlag": "B"}, "dataBody": {}}
    broker = KB(account="fixture", bound_account="fixture", app_key="fake-app", token="fake-token",
                client=client, safety=live_safety, writes_enabled=True)
    with pytest.raises(InvalidResponseError):
        broker.place_order(request_order)
    assert client.post.call_count == 1
    assert client.post.call_args.args == ("/api/v1/ssam1802",)



def test_kb_us_cancel_refuses_partial_quantity_before_write(live_safety):
    from pykbroker.brokers.kb import KB, KB_API_BASE_URL
    from pykbroker.models import Order
    client = Mock(base_url=KB_API_BASE_URL)
    broker = KB(account="fixture", bound_account="fixture", app_key="fake-app", token="fake-token", market="US",
                venue="NASDAQ", is_integrated=True, writes_enabled=True, client=client, safety=live_safety)
    broker.list_orders = Mock(return_value=[Order(id="42", symbol="TSLA", market="US", price="10", remaining=2)])
    request = CancelRequest(order_id="42", symbol="TSLA", market="US", venue="NASDAQ", quantity=1,
                            client_key="kb-us-partial-cancel")
    with pytest.raises(SpecUnverifiedError, match="full remainder"):
        broker.cancel_order(request)
    client.post.assert_not_called()


def test_kb_us_exact_cancel_ack_remains_unknown(live_safety):
    from pykbroker.brokers.kb import KB, KB_API_BASE_URL
    from pykbroker.models import Order
    client = Mock(base_url=KB_API_BASE_URL)
    # SKAM2102 official success example carries processFlag "B" (test_kb_process_flag_1028).
    client.post.return_value = {"dataHeader": {"resultCode": "200", "processFlag": "B"},
                              "dataBody": {"ordr_no": "43"}}
    broker = KB(account="fixture", bound_account="fixture", app_key="fake-app", token="fake-token", market="US",
                venue="NASDAQ", is_integrated=True, writes_enabled=True, client=client, safety=live_safety)
    broker.list_orders = Mock(return_value=[Order(id="42", symbol="TSLA", market="US", price="10", remaining=2)])
    request = CancelRequest(order_id="42", symbol="TSLA", market="US", venue="NASDAQ", quantity=2,
                            client_key="kb-us-full-cancel")
    answer = broker.cancel_order(request)
    assert answer.id == "42" and answer.status == "unknown"
    assert client.post.call_count == 1
    assert client.post.call_args.kwargs["body"]["dataBody"] == {
        "crct_cncl_clsf": "2", "is_cd": "TSLA", "orgn_ordr_no": "42", "frgn_ordr_prc_p4": "10"}
