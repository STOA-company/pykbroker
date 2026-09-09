"""Authenticated read-only example. Configure environment per getting-started."""

from pykbroker import create_broker

with create_broker("kis") as broker:
    quote = broker.get_quote("005930")
    print(quote.model_dump_json())
