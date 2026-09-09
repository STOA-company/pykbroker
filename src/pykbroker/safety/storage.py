"""Optional durable local ledgers. Policy amounts still come from the caller."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from decimal import Decimal
from pathlib import Path

from pykbroker.exceptions import SafetyError

from .risk_caps import RiskLimits, check_caps


class SQLiteLedger:
    """Single-host, multi-process atomic reservations and key claims.

    The caller owns the path and permissions. Never delete this file to retry
    uncertain orders. OrderSafety supplies hashed scope and client identifiers.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = None
        try:
            connection = sqlite3.connect(self.path)
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("CREATE TABLE IF NOT EXISTS claims (scope TEXT, key TEXT, PRIMARY KEY(scope,key))")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS usage (scope TEXT, day TEXT, amount TEXT, PRIMARY KEY(scope,day))"
            )
            yield connection
            connection.commit()
        except sqlite3.Error:
            if connection is not None:
                connection.rollback()
            raise SafetyError("Safety ledger unavailable; operation blocked") from None
        finally:
            if connection is not None:
                connection.close()

    def claim(self, scope: str, key: str) -> bool:
        with self._transaction() as connection:
            cursor = connection.execute("INSERT OR IGNORE INTO claims VALUES (?, ?)", (scope, key))
            return cursor.rowcount == 1

    def reserve(self, scope: str, day: date, amount: Decimal, limits: RiskLimits) -> None:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT amount FROM usage WHERE scope=? AND day=?", (scope, day.isoformat())
            ).fetchone()
            spent = Decimal(row[0]) if row else Decimal(0)
            check_caps(amount, spent, limits)
            connection.execute(
                "INSERT OR REPLACE INTO usage VALUES (?, ?, ?)", (scope, day.isoformat(), str(spent + amount))
            )
