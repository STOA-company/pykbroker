"""No keys or network. Synthetic market data demonstrates client injection."""

from types import SimpleNamespace

from pykbroker import KIS


class FixtureClient:
    def account(self, account):
        return SimpleNamespace()


class FixtureAPI:
    def get_price_domestic(self, **kwargs):
        return {"json_data": {"rt_cd": "0", "output": {"stck_prpr": "70000"}}}

    def get_quote_domestic(self, **kwargs):
        return {"json_data": {"rt_cd": "0", "output1": {"bidp1": "69900", "askp1": "70100"}}}


with KIS(account="fixture", client=FixtureClient(), api=FixtureAPI()) as broker:
    print(broker.get_quote("005930").model_dump_json())
