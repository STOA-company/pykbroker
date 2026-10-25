"""SEAM S-추가3 market vocabulary and denomination (no inferred exchange routing)."""
from typing import Literal

Market = Literal["KR", "US", "HK", "CN_SH", "CN_SZ", "JP_TSE", "JP_OSE", "JP_JASDAQ", "VN_HOSE", "VN_HNX"]
CURRENCIES = {"KR": "KRW", "US": "USD", "HK": "HKD", "CN_SH": "CNY", "CN_SZ": "CNY",
              "JP_TSE": "JPY", "JP_OSE": "JPY", "JP_JASDAQ": "JPY", "VN_HOSE": "VND", "VN_HNX": "VND"}
INTEGER_PRICE_CURRENCIES = frozenset({"KRW", "JPY", "VND"})
TIMEZONES = {"KR": "Asia/Seoul", "US": "America/New_York", "HK": "Asia/Hong_Kong",
             "CN_SH": "Asia/Shanghai", "CN_SZ": "Asia/Shanghai", "JP_TSE": "Asia/Tokyo",
             "JP_OSE": "Asia/Tokyo", "JP_JASDAQ": "Asia/Tokyo", "VN_HOSE": "Asia/Ho_Chi_Minh",
             "VN_HNX": "Asia/Ho_Chi_Minh"}
