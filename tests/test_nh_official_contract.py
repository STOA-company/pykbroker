"""Offline regressions using the 2026-10-08 official PLUG schema excerpts."""

import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from pykbroker.brokers.nh import NH
from pykbroker.exceptions import ConfigurationError, InvalidResponseError, SafetyError
from pykbroker.models import CancelRequest, Order, OrderRequest
from tests.test_nh_order_contract import make as make_order
from tests.test_nh_order_contract import reply as order_reply
from tests.test_nh_read_contract import make as make_read
from tests.test_nh_read_contract import reply
from tests.test_nh_sdk_finish import make as make_sdk

OFFICIAL = Path(__file__).parent / "fixtures" / "nh" / "official"
BALANCE = "/krstock/inquiry/v1/balance"
DAILY = "/krstock/inquiry/v1/dailyOrderExecution"
US_LEDGER = "/gbstock/inquiry/v1/unexecuted"


def operation(group, path):
    return json.loads((OFFICIAL / f"{group}.json").read_text())["paths"][path]["post"]


def response_schema(group, path, block="Output_0"):
    return operation(group, path)["responses"]["200"]["content"]["application/json"]["schema"]["properties"][block]


def check_fields(schema, data):
    """Check declared fields, required keys and JSON types; no new validator dependency."""
    kind = schema["type"]
    if kind == "object":
        assert isinstance(data, dict)
        assert set(schema.get("required", [])) <= data.keys()
        assert data.keys() <= schema["properties"].keys()
        for name, value in data.items():
            check_fields(schema["properties"][name], value)
    elif kind == "array":
        assert isinstance(data, list)
        for row in data:
            check_fields(schema["items"], row)
    elif kind == "integer":
        assert type(data) is int
    elif kind == "number":
        assert type(data) in (int, float)
    else:
        assert kind == "string" and isinstance(data, str)


def check_input(group, path, body):
    schema = operation(group, path)["requestBody"]["content"]["application/json"]["schema"]
    check_fields(schema, body)


def test_official_snapshot_has_expected_cash_and_ledger_contracts():
    fields = response_schema("krstock", BALANCE)["properties"]
    assert "orr_pbl_amt" not in fields
    assert "30%" in fields["orr_pbl_amt2"]["description"]
    assert "40%" in fields["orr_pbl_amt3"]["description"]
    assert "100%" in fields["orr_pbl_amt4"]["description"]
    assert response_schema("krstock", DAILY)["type"] == "array"
    for path in ("/oauth2/token", "/n2/acctinfo"):
        assert operation("common", path)["x-available-env"] == ["live"]


@pytest.mark.parametrize("deposit,orderable,expected", [(50000, 40000, 40000), (30000, 40000, 30000), (0, 0, 0)])
@pytest.mark.parametrize("brand", ["nhplug", "n2plug"])
def test_official_balance_uses_deposit_and_100_percent_cash(deposit, orderable, expected, brand):
    row = {
        "dca": deposit,
        "orr_pbl_amt4": orderable,
        "orr_pbl_amt2": 90000,
        "orr_pbl_amt3": 80000,
        "tot_eal_amt": 30000,
    }
    check_fields(response_schema("krstock", BALANCE), row)
    broker, calls = make_read(
        {BALANCE: reply({"rsp_cd": "00000", "Output_0": row})},
        options={"brand": brand, "app_key": "fixture-key", "token": "fixture-token"},
    )
    funds = broker.get_balance()
    assert (funds.cash_available, funds.position_value) == (expected, 30000)
    assert broker.available_cash() == expected
    assert all(call[0] == f"https://api.{brand}.com:8443" for call in calls)
    check_input("krstock", BALANCE, json.loads(calls[0][3]))


@pytest.mark.parametrize("legacy,expected", [(20000, 20000), (60000, 40000), (0, 0)])
def test_official_balance_also_caps_cash_by_present_legacy_field(legacy, expected):
    row = {"dca": 50000, "orr_pbl_amt4": 40000, "tot_eal_amt": 0}
    check_fields(response_schema("krstock", BALANCE), row)
    # Legacy wire compatibility is not part of the official balance schema.
    row["orr_pbl_amt"] = legacy
    broker, _ = make_read({BALANCE: reply({"rsp_cd": "00000", "Output_0": row})})
    assert broker.get_balance().cash_available == expected


@pytest.mark.parametrize(
    "fields",
    [{"dca": 123}, {"orr_pbl_amt4": 123}, {"orr_pbl_amt": 123}, {"orr_pbl_amt4": 123, "orr_pbl_amt": 100}],
)
def test_cash_requires_deposit_and_orderable_amount(fields):
    row = {**fields, "tot_eal_amt": 0}
    broker, _ = make_read({BALANCE: reply({"rsp_cd": "00000", "Output_0": row})})
    with pytest.raises(InvalidResponseError, match="cash balance fields are missing"):
        broker.get_balance()


@pytest.mark.parametrize("deposit,orderable,expected", [(50000, 40000, 40000), (30000, 40000, 30000), (0, 0, 0)])
def test_balance_uses_deposit_and_legacy_orderable_cash(deposit, orderable, expected):
    row = {"dca": deposit, "orr_pbl_amt": orderable, "tot_eal_amt": 0}
    broker, _ = make_read({BALANCE: reply({"rsp_cd": "00000", "Output_0": row})})
    assert broker.get_balance().cash_available == expected


def test_official_margin_only_balance_fails_closed():
    row = {"orr_pbl_amt2": 90000, "orr_pbl_amt3": 80000, "tot_eal_amt": 0}
    check_fields(response_schema("krstock", BALANCE), row)
    broker, _ = make_read({BALANCE: reply({"rsp_cd": "00000", "Output_0": row})})
    with pytest.raises(InvalidResponseError, match="cash balance fields are missing"):
        broker.get_balance()


@pytest.mark.parametrize("field", ["dca", "orr_pbl_amt4", "orr_pbl_amt"])
@pytest.mark.parametrize("invalid", [None, True, "", "bad", "NaN", "Infinity", -1])
def test_malformed_present_cash_candidate_is_never_ignored(field, invalid):
    row = {"dca": 50000, "orr_pbl_amt4": 40000, "tot_eal_amt": 0, field: invalid}
    broker, _ = make_read({BALANCE: reply({"rsp_cd": "00000", "Output_0": row})})
    with pytest.raises(InvalidResponseError):
        broker.get_balance()


def kr_row(number=456):
    row = {
        "itg_orr_no": number,
        "iem_cd": "005930",
        "orr_qty": 1,
        "orr_pr": 70000,
        "tot_cns_qty": 1,
        "ny_cns_qty": 0,
        "cns_avg_uit_pr": 69950,
    }
    check_fields(response_schema("krstock", DAILY)["items"], row)
    return row


@pytest.mark.parametrize("blocks", ["Output_0", "Output_1", "both"])
@pytest.mark.parametrize("empty", [False, True])
def test_daily_accepts_single_or_identical_blocks(blocks, empty):
    rows = [] if empty else [kr_row()]
    body = {"rsp_cd": "00000"}
    for block in ("Output_0", "Output_1") if blocks == "both" else (blocks,):
        body[block] = [dict(row) for row in rows]
    broker, calls = make_order({DAILY: [(200, body, {})]})
    orders = broker.list_orders()
    if empty:
        assert orders == []
    else:
        (order,) = orders
        assert (order.id, order.status, order.fill_price) == ("itg:456", "filled", Decimal(69950))
    check_input("krstock", DAILY, calls[0][3])


@pytest.mark.parametrize(
    "blocks", [([], [kr_row()]), ([kr_row()], []), ([kr_row()], [kr_row(789)]), ([kr_row()], "malformed")]
)
@pytest.mark.parametrize("page_index", [0, 1])
@pytest.mark.parametrize("method", ["list_orders", "status"])
def test_daily_conflicting_blocks_fail_closed_for_list_status_and_pages(blocks, page_index, method):
    pages = []
    if page_index:
        pages.append(
            (200, {"rsp_cd": "00165", "Output_0": [kr_row()], "Output_1": [kr_row()]},
             {"cts_flag": "Y", "cts": "next"})
        )
    pages.append((200, {"rsp_cd": "00000", "Output_0": blocks[0], "Output_1": blocks[1]}, {}))
    broker, calls = make_order({DAILY: pages})
    with pytest.raises(InvalidResponseError, match="daily order rows are invalid"):
        if method == "status":
            broker.status(Order(id="itg:456", symbol="005930", status="accepted"))
        else:
            broker.list_orders()
    assert len(calls) == page_index + 1
    if page_index:
        assert calls[1][2]["cts"] == "next"


@pytest.mark.parametrize("invalid", [None, {}, "bad"])
def test_invalid_official_daily_block_does_not_fall_back_to_legacy(invalid):
    broker, _ = make_order({DAILY: [(200, {"rsp_cd": "00000", "Output_0": invalid, "Output_1": [kr_row()]}, {})]})
    with pytest.raises(InvalidResponseError):
        broker.list_orders()


def test_official_daily_pages_accept_identical_blocks_on_each_page():
    first = (
        200, {"rsp_cd": "00165", "Output_0": [kr_row()], "Output_1": [kr_row()]}, {"cts_flag": "Y", "cts": "next"}
    )
    last = (200, {"rsp_cd": "00000", "Output_0": [kr_row(789)], "Output_1": [kr_row(789)]}, {"cts_flag": "N"})
    broker, calls = make_order({DAILY: [first, last]})
    assert [order.id for order in broker.list_orders()] == ["itg:456", "itg:789"]
    assert calls[1][2]["cts"] == "next"


@pytest.mark.parametrize(
    "brand,live,mock",
    [
        ("nhplug", "https://api.nhplug.com:8443", "https://moapi.nhplug.com:8443"),
        ("n2plug", "https://api.n2plug.com:8443", "https://moapi.n2plug.com:8443"),
    ],
)
@pytest.mark.parametrize("virtual", [False, True])
def test_official_sdk_brand_reaches_factory_options(monkeypatch, brand, live, mock, virtual):
    evidence = (OFFICIAL / "sdk-domains.txt").read_text()
    assert brand in evidence and live.removeprefix("https://").removesuffix(":8443") in evidence
    assert mock.removeprefix("https://").removesuffix(":8443") in evidence
    options = {
        "brand": brand,
        "app_key": "fixture-key",
        "token": "fixture-token",
        "acct_type": "03" if virtual else "01",
    }
    monkeypatch.setenv("PYKBROKER_CLIENT_OPTIONS", json.dumps(options))
    broker, calls, hosts = make_sdk(factory=True, options=False, virtual=virtual)
    assert calls == [] and hosts == [mock if virtual else live, live]
    assert broker.verify_account() == options["acct_type"]
    assert calls[0][0] == live
    check_input("common", "/n2/acctinfo", json.loads(calls[0][3]))


@pytest.mark.parametrize("brand", [None, "", "N2", "n2", "namuh", "https://api.n2plug.com:8443", [], True])
def test_unknown_brand_is_rejected_before_client_creation(brand):
    hosts = []
    with pytest.raises(ConfigurationError, match="brand"):
        NH(account="fixture-account", client_options={"brand": brand}, client_factory=lambda **kw: hosts.append(kw))
    assert hosts == []


@pytest.mark.parametrize(
    "bad_host",
    [
        "https://api.nhplug.com:8443",
        "https://moapi.n2plug.com:8443",
        "https://api.n2plug.com",
        "https://api.n2plug.com:443",
        "http://api.n2plug.com:8443",
        "https://api.n2plug.com:8443/",
        "https://api.n2plug.com.evil.example:8443",
        "https://api.n2plug.com:8443@evil.example",
    ],
)
@pytest.mark.parametrize("bad_client", ["order", "auth"])
def test_n2_injected_client_requires_exact_selected_host(bad_host, bad_client):
    hosts = []

    def factory(*, base_url):
        hosts.append(base_url)
        return SimpleNamespace(base_url=bad_host if len(hosts) == (1 if bad_client == "order" else 2) else base_url)

    with pytest.raises(ConfigurationError, match="host"):
        NH(
            account="fixture-account",
            client_options={"brand": "n2plug", "app_key": "fixture-key", "token": "fixture-token", "acct_type": "01"},
            client_factory=factory,
        )


@pytest.mark.parametrize("market,venue,prefix", [("KR", "KRX", "mkt:"), ("US", "US", "us:")])
def test_n2_live_buy_sell_cancel_remain_locked_with_enabled_safety(market, venue, prefix, live_safety):
    options = {"brand": "n2plug", "app_key": "fixture-key", "token": "fixture-token", "acct_type": "01"}
    broker, calls, _ = make_sdk(options=options, market=market, venue=venue, safety=live_safety)
    symbol = "005930" if market == "KR" else "AAPL"
    for side in ("buy", "sell"):
        with pytest.raises(SafetyError, match="live orders are locked"):
            broker.place_order(
                OrderRequest(symbol=symbol, market=market, side=side, quantity=1, price=100, client_key=side)
            )
    with pytest.raises(SafetyError, match="live orders are locked"):
        broker.cancel_order(
            CancelRequest(symbol=symbol, market=market, order_id=prefix + "123", quantity=1, client_key="cancel")
        )
    assert calls == []


@pytest.mark.parametrize("side", ["buy", "sell"])
def test_official_us_limit_order_cancel_and_ledger_share_order_number(side, live_safety):
    path = "/gbstock/order/v1/" + side
    cancel_path = "/gbstock/order/v1/cancel"
    ack = {"orr_no": 123}
    check_fields(response_schema("gbstock", path), ack)
    check_fields(response_schema("gbstock", cancel_path), {"orr_no": 124})
    row = {
        "orr_no": 123,
        "fc_sec_trd_nat_cd": "200",
        "iem_cd": "AAPL",
        "orr_qty": 1,
        "fc_orr_uit_pr": 230.25,
        "cns_qty": 1,
        "cns_pr": 230.20,
        "ny_cns_orr_qty": 0,
        "oss_sby_dit_cd": "2" if side == "buy" else "1",
        "ahi_nmn_pr_tp_cd": "00",
    }
    check_fields(response_schema("gbstock", US_LEDGER), [row])
    assert (
        response_schema("gbstock", path)["properties"]["orr_no"]
        == response_schema("gbstock", US_LEDGER)["items"]["properties"]["orr_no"]
    )
    broker, calls = make_order(
        {path: [order_reply(ack)], cancel_path: [order_reply({"orr_no": 124})], US_LEDGER: [order_reply([row])]},
        market="US",
        venue="US",
        virtual=True,
        safety=live_safety,
    )
    live_safety.valuation = lambda request: Decimal(1000)
    placed = broker.place_order(
        OrderRequest(symbol="AAPL", market="US", side=side, quantity=1, price="230.25", client_key="official-us")
    )
    assert placed.id == "us:123"
    cancelled = broker.cancel_order(
        CancelRequest(symbol="AAPL", market="US", order_id=placed.id, quantity=1, client_key="official-cancel")
    )
    assert cancelled.id == placed.id
    observed = broker.status(placed)
    assert (observed.id, observed.status, observed.side, observed.fill_price) == (placed.id, "filled", side, None)
    for call in calls:
        check_input("gbstock", call[1], call[3])
    wire = calls[0][3]["Input_0"]
    assert (wire["fc_sec_trd_nat_cd"], wire["ahi_nmn_pr_tp_cd"], wire["fc_orr_uit_pr"]) == ("200", "00", 230.25)
    assert ("wtm_cur_knd_cd" in wire) is (side == "buy")
    assert calls[1][3]["Input_0"]["org_orr_no"] == 123
