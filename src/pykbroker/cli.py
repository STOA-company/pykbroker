"""Read-only command line; all broker construction uses the shared factory."""

import argparse
import json
import os

from . import __version__
from .exceptions import BrokerError
from .factory import BROKERS, create_broker


def main() -> None:
    parser = argparse.ArgumentParser(prog="pykbroker", description="Query Korean stock brokers")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--broker", choices=sorted(BROKERS), default=os.environ.get("PYKBROKER_BROKER", "kis"))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("brokers")
    sub.add_parser("balance")
    sub.add_parser("positions")
    for command in ("quote", "rules"):
        sub.add_parser(command).add_argument("symbol")
    sub.add_parser("orders").add_argument("--symbol")
    args = parser.parse_args()
    if args.command == "brokers":
        print(json.dumps(sorted(BROKERS)))
        return
    try:
        with create_broker(args.broker) as broker:
            if args.command == "quote":
                result = broker.get_quote(args.symbol)
            elif args.command == "rules":
                result = broker.get_rules(args.symbol)
            elif args.command == "balance":
                result = broker.get_balance()
            elif args.command == "positions":
                result = broker.get_positions()
            else:
                result = broker.list_orders(args.symbol)
            print(
                json.dumps([item.model_dump(mode="json") for item in result], ensure_ascii=False)
                if isinstance(result, list)
                else result.model_dump_json()
            )
    except BrokerError as exc:
        parser.exit(2, f"pykbroker: {exc}\n")


if __name__ == "__main__":
    main()
