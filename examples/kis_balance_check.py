"""한국투자증권 잔고 조회 / KIS balance check (authenticated, read-only).

Install from the checkout with ``pip install -e '.[kis]'`` and supply the
compatible Pykis source plus PYKBROKER_ACCOUNT / PYKBROKER_CLIENT_OPTIONS
as described in docs/getting-started.md. Never put credentials in this file.
Run: python examples/kis_balance_check.py
This makes authenticated balance requests; it does not place or cancel orders.
"""

import sys

from pykbroker import create_broker
from pykbroker.exceptions import BrokerError


def main() -> int:
    try:
        with create_broker("kis") as broker:
            balance = broker.get_balance()
            print(balance.model_dump_json())
    except BrokerError:
        # Do not copy vendor exceptions, credentials, or account IDs to output.
        print("잔고 조회 실패: docs/getting-started.md의 인증·벤더 설정을 확인하세요.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
