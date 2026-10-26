"""KRW-settled US buys (D4897): wire body, limit column, enrollment verdict, currency label.

Contract mocks built from the vendor sheets only -- no live call. A column the sheet leaves
undefined is marked in the broker code as 'column-name basis, pending a real order'.
"""

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pykbroker.brokers._common import OrderLimit
from pykbroker.exceptions import (
    ConfigurationError,
    IntegratedNotSubscribedError,
    InvalidResponseError,
    NotSupportedError,
)
from pykbroker.brokers.ls import PREXCH_UNIT_UNVERIFIED
from pykbroker.models import OrderRequest

from .test_ls_contract import us_make as ls_us_make
from .test_nh_order_contract import US_POWER, make as nh_make, reply

KRW_FIELDS = ("NH", "KIWOOM", "KB", "LS", "MERITZ")


# NH ---------------------------------------------------------------------------------
@pytest.mark.parametrize("integrated,wire", [(False, "1"), (True, "2")])
def test_nh_buy_body_currency_code_follows_krw_flag_and_nothing_else_changes(integrated, wire, live_safety):
    # `integrated` below is the krw_settlement option (customer-confirmed), not the account policy.
    live_safety.valuation = lambda request: Decimal(1000)
    broker, calls = nh_make(
        {"/gbstock/order/v1/buy": [reply({"orr_no": 7})]},
        market="US", venue="US", virtual=True, safety=live_safety, krw_settlement=integrated,
    )
    broker.place_order(OrderRequest(symbol="AAPL", market="US", side="buy", quantity=2,
                                    price=Decimal("230.25"), client_key="k"))
    assert len(calls) == 1
    assert calls[0][3]["Input_0"] == {
        "act_no": "12345678901", "fc_sec_trd_nat_cd": "200", "iem_cd": "AAPL", "orr_qty": 2,
        "fc_orr_uit_pr": 230.25, "ahi_nmn_pr_tp_cd": "00", "wtm_cur_knd_cd": wire,
    }


def test_nh_sell_body_has_no_currency_field_in_either_mode(live_safety):
    live_safety.valuation = lambda request: Decimal(1000)
    for integrated in (False, True):
        broker, calls = nh_make(
            {"/gbstock/order/v1/sell": [reply({"orr_no": 8})]},
            market="US", venue="US", virtual=True, safety=live_safety, krw_settlement=integrated,
        )
        broker.place_order(OrderRequest(symbol="AAPL", market="US", side="sell", quantity=1,
                                        price=Decimal("230.25"), client_key=f"s-{integrated}"))
        assert "wtm_cur_knd_cd" not in calls[0][3]["Input_0"]


def test_nh_krw_limit_is_quantity_labelled_krw_never_a_usd_amount():
    broker, calls = nh_make(
        {US_POWER: [reply({"orr_pbl_amt": "700000", "wtm_cur_cd": "KRW"}),
                    reply({"orr_pbl_qty": 4, "orr_pbl_amt": "700000", "wtm_cur_cd": "KRW"})]},
        market="US", venue="US", krw_settlement=True)
    assert broker.buyable_limit("AAPL", price=Decimal("150")) == OrderLimit(Decimal(4), "KRW", "quantity")
    assert [c[3]["Input_0"]["wtm_cur_knd_cd"] for c in calls] == ["2", "2"]
    assert [c[3]["Input_0"]["pcs_dit"] for c in calls] == ["1", "2"]


def test_nh_cash_limit_is_usd_amount_and_krw_row_is_refused():
    broker, _ = nh_make({US_POWER: [reply({"orr_pbl_amt": "500.25", "wtm_cur_cd": "USD"})]},
                        market="US", venue="US")
    assert broker.buyable_limit("AAPL", price=Decimal("150")) == OrderLimit(Decimal("500.25"), "USD")
    broker, _ = nh_make({US_POWER: [reply({"orr_pbl_amt": "5", "wtm_cur_cd": "KRW"})]}, market="US", venue="US")
    with pytest.raises(InvalidResponseError):
        broker.buyable_limit("AAPL", price=Decimal("150"))


def test_nh_krw_row_in_usd_mode_wrong_currency_in_krw_mode_refused():
    broker, _ = nh_make({US_POWER: [reply({"orr_pbl_amt": "5", "wtm_cur_cd": "USD"})]},
                        market="US", venue="US", krw_settlement=True)
    with pytest.raises(InvalidResponseError):
        broker.buyable_limit("AAPL", price=Decimal("150"))


@pytest.mark.parametrize("policy_only", [True])
def test_nh_integrated_margin_policy_alone_never_sends_won_settlement(policy_only, live_safety):
    """P2-2: the account/strategy policy (is_integrated) keeps its old meaning — a lookup of integrated
    margin (wtm 1). The order body stays 1 (same currency); only krw_settlement sends 2."""
    live_safety.valuation = lambda request: Decimal(1000)
    broker, calls = nh_make({"/gbstock/order/v1/buy": [reply({"orr_no": 7})]}, market="US", venue="US",
                            virtual=True, safety=live_safety, is_integrated=policy_only)
    broker.place_order(OrderRequest(symbol="AAPL", market="US", side="buy", quantity=2,
                                    price=Decimal("230.25"), client_key="k"))
    assert calls[0][3]["Input_0"]["wtm_cur_knd_cd"] == "1"
    broker, calls = nh_make({US_POWER: [reply({"orr_pbl_amt": "9", "wtm_cur_cd": "USD"})]},
                            market="US", venue="US", is_integrated=policy_only)
    assert broker.buyable_limit("AAPL", price=Decimal("150")) == OrderLimit(Decimal("9"), "USD")
    assert calls[0][3]["Input_0"]["wtm_cur_knd_cd"] == "1"


# Kiwoom -----------------------------------------------------------------------------
def kiwoom(response, **kwargs):
    from .test_kiwoom_read_contract import us_read
    return us_read({"ust31490": response}, **kwargs)


KIWOOM_ROW = {"return_code": 0, "crnc_code": "USD", "krw_ord_rqst_yn": "Y", "min_ord_alowq": "3",
              "min_krw_ord_alowq": "7", "min_krw_ord_alowa": "1500000", "min_ord_alowa": "1000"}


def test_kiwoom_krw_limit_amount_is_krw_label_and_quantity_is_the_comparable_column():
    broker = kiwoom(KIWOOM_ROW, is_integrated=True)
    assert broker.krw_order_limit("NVDA", Decimal("200")) == OrderLimit(Decimal("1500000"), "KRW")
    assert broker.orderable_quantity("NVDA", Decimal("200")) == 7


@pytest.mark.parametrize("flag", ["N", "", None])
def test_kiwoom_not_applied_account_gets_dedicated_exception_for_both_reads(flag):
    row = {**KIWOOM_ROW, "krw_ord_rqst_yn": flag}
    with pytest.raises(IntegratedNotSubscribedError):
        kiwoom(row, is_integrated=True).krw_order_limit("NVDA", Decimal("200"))
    with pytest.raises(IntegratedNotSubscribedError):
        kiwoom(row, is_integrated=True).orderable_quantity("NVDA", Decimal("200"))


def test_kiwoom_cash_mode_quantity_column_is_unchanged():
    assert kiwoom(KIWOOM_ROW).orderable_quantity("NVDA", Decimal("200")) == 3


# KB ---------------------------------------------------------------------------------
def kb(body, integrated=True):
    from pykbroker.brokers.kb import KB, KB_API_BASE_URL
    client = Mock(base_url=KB_API_BASE_URL)
    client.post.return_value = {"dataHeader": {"resultCode": "200", "processFlag": "A"}, "dataBody": body}
    broker = KB(account="fixture", bound_account="fixture", market="US", venue="NASDAQ",
                app_key="fake-app", token="fake-token", is_integrated=integrated, client=client)
    broker._limiter = SimpleNamespace(wait=lambda _: None)
    return broker, client


def test_kb_krw_limit_reads_converted_unified_column_labelled_krw():
    broker, client = kb({"krw_exch_unty_ordr_psbl_amt": "900000", "fcrncy_unty_ordr_psbl_amt_p2": "123.45"})
    assert broker.krw_order_limit("TSLA") == OrderLimit(Decimal("900000"), "KRW")
    assert client.post.call_args.kwargs["body"]["dataBody"] == {"stnd_is_cd": "TSLA"}
    assert client.post.call_count == 1


@pytest.mark.parametrize("value", ["0", "0.00"])
def test_kb_zero_limit_means_global_one_market_not_applied(value):
    broker, _ = kb({"krw_exch_unty_ordr_psbl_amt": value})
    with pytest.raises(IntegratedNotSubscribedError):
        broker.krw_order_limit("TSLA")


def test_kb_foreign_unified_column_is_untouched_for_the_existing_path():
    broker, _ = kb({"krw_exch_unty_ordr_psbl_amt": "9", "fcrncy_unty_ordr_psbl_amt_p2": "123.45"})
    assert broker.orderable_cash("TSLA") == Decimal("123.45")


# LS ---------------------------------------------------------------------------------
def test_ls_pre_exchange_limit_reads_prexch_column_for_usd_row_only():
    rows = [{"CrcyCode": "USD", "FcurrOrdAbleAmt": "10", "PrexchOrdAbleAmt": "321.5"},
            {"CrcyCode": "JPY", "FcurrOrdAbleAmt": "1", "PrexchOrdAbleAmt": "9999"}]
    broker, client, _ = ls_us_make({"COSOQ02701": [{"rsp_cd": "00000", "COSOQ02701OutBlock3": rows}]},
                                   is_integrated=True)
    assert broker.krw_order_limit() == OrderLimit(Decimal("321.5"), PREXCH_UNIT_UNVERIFIED)  # unit unconfirmed (len 21.4)
    assert len(client.calls) == 1


def test_ls_pre_exchange_limit_without_a_single_usd_row_is_unknown():
    broker, _, _ = ls_us_make({"COSOQ02701": [{"rsp_cd": "00000", "COSOQ02701OutBlock3": []}]},
                              is_integrated=True)
    with pytest.raises(InvalidResponseError):
        broker.krw_order_limit()


# Meritz -----------------------------------------------------------------------------
@pytest.mark.parametrize("integrated,flag", [(False, "N"), (True, "Y")])
def test_meritz_estimate_integrated_flag_follows_krw_setting(integrated, flag, live_safety, monkeypatch):
    from datetime import date

    from pykbroker.brokers.meritz import Meritz
    from pykbroker.safety.rate_limit import RateLimiter

    from .meritz_contract_server import ContractServer
    monkeypatch.setattr(RateLimiter, "wait", lambda *args: None)
    server = ContractServer()
    auth = Meritz.issue_token(app_key="fixture-key", secret_key="fixture-secret", client_factory=lambda: server)
    broker = Meritz(account="fixture-account", bound_account="fixture-account", app_key="fixture-key",
                    token=auth["access_token"], mac_address="001122AABBCC", client=server, market="US",
                    venue="NYSE", order_date=date(2026, 10, 25), safety=live_safety, is_integrated=integrated)
    seen = []
    original = broker._response

    def spy(method, path, **kwargs):
        seen.append(kwargs.get("params"))
        return original(method, path, **kwargs)

    monkeypatch.setattr(broker, "_response", spy)
    assert isinstance(broker.available_cash(), Decimal)
    assert seen == [{"intg_marg_iqry_yn": flag}]


def test_meritz_integrated_flag_must_be_boolean():
    from pykbroker.brokers.meritz import Meritz
    with pytest.raises(ConfigurationError):
        Meritz(account="a", bound_account="a", app_key="k" * 8, token="t", mac_address="001122AABBCC",
               market="KR", is_integrated="Y")


# KIS / DB: locked, not changed ----------------------------------------------------------
def test_db_integrated_not_applied_has_dedicated_exception_and_unknown_flag_does_not():
    from pykbroker.brokers.dbfi import DBFI
    client = Mock()
    client.get_able_order_quantity.return_value = {"rsp_cd": "00000", "Out": {
        "AstkOrdAbleAmt": "5", "AstkOrdAbleAmt0": "300", "CsldtMgnUseYn": "N"}}
    broker = DBFI(account="fixture", market="US", venue="NASDAQ", client=client, is_integrated=True)
    with pytest.raises(IntegratedNotSubscribedError):
        broker.available_cash()
    client.get_able_order_quantity.return_value["Out"]["CsldtMgnUseYn"] = None
    with pytest.raises(InvalidResponseError) as info:
        broker.available_cash()
    assert not isinstance(info.value, NotSupportedError)
    client.get_able_order_quantity.return_value["Out"]["CsldtMgnUseYn"] = "Y"
    assert broker.available_cash() == Decimal("300")
    assert DBFI(account="fixture", market="US", venue="NASDAQ", client=client).available_cash() == Decimal("5")


@pytest.mark.parametrize("integrated,column", [(True, "usd_itgr_ord_psbl_amt"), (False, "usd_gnrl_ord_psbl_amt")])
def test_kis_us_limit_column_follows_krw_flag(kis, integrated, column):
    broker, _, _, scope = kis
    broker.market = "US"
    broker.is_integrated = integrated
    scope.account.build_body.side_effect = lambda params: params
    scope.client.request.return_value = {"rt_cd": "0", "output": {
        "usd_itgr_ord_psbl_amt": "300", "usd_gnrl_ord_psbl_amt": "100"}}
    assert broker._us_cash(scope) == Decimal("300" if integrated else "100")
    assert scope.client.request.call_args.kwargs["params"]["WCRC_FRCR_DVSN_CD"] == "01"
