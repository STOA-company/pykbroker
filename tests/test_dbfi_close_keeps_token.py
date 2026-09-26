"""Closing a DBFI broker must not revoke the account's access token.

Pydb's `DBFI.close()` calls `OAuth.revoke_token()` (Pydb/pydbfi/api.py:31-32, oauth.py:177) on the
domestic, overseas, and futures APIs. The token lives on the account row and is reused by the
legacy DbfiOrder and TriggerTrader, so revoking it at close cuts them off.
"""

from types import SimpleNamespace

import pytest

from pykbroker.brokers.dbfi import DBFI
from pykbroker.exceptions import ConfigurationError


class VendorDbfi:
    """`Pydb.pydbfi.DBFI`: every `close()` revokes the shared token, once per API object."""

    def __init__(self, **options):
        self.options, self.revocations = options, 0
        self.auth = SimpleNamespace(revoke_token=self._revoke)

    def _revoke(self):
        self.revocations += 1

    def close(self):
        for _ in ("domestic", "overseas", "domestic_futures"):
            self.auth.revoke_token()


def test_close_never_reaches_the_vendors_token_revocation():
    vendor = VendorDbfi()
    broker = DBFI(account="fixture-account", client=vendor, api=object())
    broker.close()
    broker.close()
    assert vendor.revocations == 0


def test_an_owned_client_is_not_closed_through_the_vendor_either():
    built = []

    def factory(**options):
        built.append(VendorDbfi(**options))
        return built[-1]

    broker = DBFI(account="fixture-account", client_factory=factory, client_options={"app_key": "k"})
    broker._connect()
    broker.close()
    assert len(built) == 1 and built[0].revocations == 0


def test_a_context_manager_exit_revokes_nothing():
    vendor = VendorDbfi()
    with DBFI(account="fixture-account", client=vendor, api=object()):
        pass
    assert vendor.revocations == 0


def test_close_still_ends_the_broker_session(dbfi):
    broker, client, _ = dbfi
    broker.close()
    client.close.assert_not_called()
    with pytest.raises(ConfigurationError):
        broker.get_balance()
    with pytest.raises(ConfigurationError):
        broker.__enter__()
