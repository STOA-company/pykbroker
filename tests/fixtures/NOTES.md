# Fixture provenance

These JSON files are **synthetic contract snapshots**, built from response field
accesses in the referenced adapters and vendor source models. They are not
recordings of authenticated brokerage traffic and do not prove live API support.
No live response captures were supplied in the extraction checkout. Tests use
these snapshots and mocks; every test blocks outbound socket connection/DNS.

KIS snapshots follow the PyKis price/book envelopes and account response models.
`kis/daily_orders.json` follows `KisStockDailyOrders` (`_pykis/scope/account/api/
response/daily_order.py`) and `kis/open_orders_overseas.json` the overseas
`inquire-nccs` envelope. `tests/vendor_chain.py` replays the vendor's own control
flow over these snapshots -- including the two places it refuses a mock account --
so a mock account's refusals are present in tests instead of mocked away.
DBFI snapshots follow Pydb get_stock_balance, quote/book and transaction history.
Values, security names and order identifiers are synthetic. No account or
credential data is stored. Source revisions are listed in NOTICE.

TODO: supplement with sanitized vendor-recorded responses after the caller
obtains permission and access; retain this provenance distinction.
