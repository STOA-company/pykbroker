---
description: "Basic KRX cash-equity tick and regular-session helpers, trading-day callbacks and unsupported product scope."
lang: en
---

# Korean cash-equity rules

`rules.kr` provides the basic KRX cash-equity tick table and regular trading hours only. Callers supply rules for ETFs, ETNs, other venues, and special opening days. Without product classification, `get_rules()` returns `tick=None`.

| Price (KRW) | Tick (KRW) |
| --- | --- |
| Below 2,000 | 1 |
| 2,000 to below 5,000 | 5 |
| 5,000 to below 20,000 | 10 |
| 20,000 to below 50,000 | 50 |
| 50,000 to below 200,000 | 100 |
| 200,000 to below 500,000 | 500 |
| 500,000 and above | 1,000 |

The source documentation records comparison with the [KRX tick-size guide](https://global.krx.co.kr/contents/GLB/06/0602/0602010201/GLB0602010201T3.jsp) on 2026-09-09.

Default regular hours are 09:00–15:30 KST. The source documentation also references the [KRX trading-hours and holidays guide](https://global.krx.co.kr/contents/GLB/06/0602/0602010201/GLB0602010201T1.jsp). `regular_session_open()` does not infer holidays and returns False without a trading-day callback. A weekday check cannot replace a trading calendar.

`normalize_symbol()` handles six-character alphanumeric short codes and DB's A prefix. `round_price()` provides buy rounding down and sell rounding up, but order submission does not silently invoke it. Caller-selected prices are not truncated before transmission.

[한국어](../../api/rules.md)
