#!/usr/bin/env python3
"""A′-6: prove the A′ tests are guards, not decoration.

A green suite proves nothing on its own. Each entry below reverts one decision the
A′ conditions made; the suite must go red for that entry. An entry that stays green
means the behaviour it names is unprotected — treat that as a failing build.

    python scripts/mutation_check.py            # every mutation
    python scripts/mutation_check.py A'-1       # one condition

Every mutation is applied to a copy of the file and reverted in a finally block, so
an interrupted run leaves the tree exactly as it found it.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "pykbroker"
EXPERIMENTAL = SRC / "experimental"


@dataclass(frozen=True)
class Mutation:
    condition: str
    name: str
    path: Path
    before: str
    after: str


MUTATIONS = (
    Mutation(
        "A'-1",
        "the mock-account flag never reaches the vendor factory",
        SRC / "brokers/kis.py",
        '"virtual_account": self.virtual,',
        '"virtual_account": False,',
    ),
    Mutation(
        "A'-1",
        "an injected client's account mode is taken on trust",
        SRC / "brokers/kis.py",
        "        if mode is not self.virtual:",
        "        if False:",
    ),
    Mutation(
        "A'-2",
        "the session judgement is inverted",
        SRC / "rules/kr.py",
        "    return REGULAR_OPEN <= local.time() < REGULAR_CLOSE",
        "    return not (REGULAR_OPEN <= local.time() < REGULAR_CLOSE)",
    ),
    Mutation(
        "A'-2",
        "the daily upper limit rounds outward, past what the venue accepts",
        SRC / "rules/kr.py",
        "rounding=ROUND_FLOOR) * up_tick",
        "rounding=ROUND_CEILING) * up_tick",
    ),
    Mutation(
        "A'-2",
        "a silent venue is read as 'not halted' instead of 'unknown'",
        SRC / "rules/kr.py",
        "halted=halted if isinstance(halted, bool) else None,",
        "halted=bool(halted),",
    ),
    Mutation(
        "A'-3",
        "available quantity answers the holding instead of the settled part",
        SRC / "brokers/_common.py",
        "                return position.sellable",
        "                return position.quantity",
    ),
    Mutation(
        "A'-3",
        "a position claiming more sellable than held is believed",
        SRC / "models/position.py",
        "        if self.sellable > self.quantity:",
        "        if False:",
    ),
    Mutation(
        "A'-4",
        "KIS claims a market order the US venue does not offer",
        SRC / "brokers/kis.py",
        '            supports_market_order=self.market == "KR",',
        "            supports_market_order=True,",
    ),
    Mutation(
        "A'-4",
        "the declared venue list gains a venue the broker cannot route to",
        SRC / "brokers/kis.py",
        '            venues=tuple(VENUES) if self.market == "US" else ("KRX",),',
        '            venues=(*VENUES, "LSE") if self.market == "US" else ("KRX",),',
    ),
    Mutation(
        "A'-5",
        "a broker with no mock domain accepts virtual=True anyway",
        SRC / "brokers/_common.py",
        "        if virtual and not self.supports_virtual:",
        "        if False:",
    ),
    # --- second round: the four bypasses the first-round review actually walked through
    Mutation(
        "A'2-1",
        "a US mock account claims an open-order route the vendor does not have",
        SRC / "brokers/kis.py",
        '            supports_order_history=not (self.market == "US" and self.virtual),',
        "            supports_order_history=True,",
    ),
    Mutation(
        "A'2-1",
        "a KR mock account is sent down the revisable-order path the vendor refuses",
        SRC / "brokers/kis.py",
        '        if self.market == "KR" and self.virtual:',
        "        if False:",
    ),
    Mutation(
        "A'2-2",
        "a halt notice is overwritten by a price row that says 'trading'",
        SRC / "brokers/kis.py",
        "        if venue is True or notice is True:",
        "        if venue is True:",
    ),
    Mutation(
        "A'2-2",
        "a status code we have not verified is read as 'trading' instead of 'unknown'",
        SRC / "brokers/kis.py",
        "    return False if text in KR_TRADING_CODES else None",
        "    return False",
    ),
    Mutation(
        "A'2-3",
        "the venue's own daily band is thrown away and recomputed as plus or minus 30 percent",
        SRC / "rules/kr.py",
        '        return upper, lower, "venue"',
        "        pass",
    ),
    Mutation(
        "A'2-4",
        "a market that requires a venue accepts an order that names none",
        SRC / "brokers/_common.py",
        "        if capabilities.requires_venue and request.venue is None:",
        "        if False:",
    ),
    Mutation(
        "A'2-5",
        "the locked REST mapping grows a mock-account argument again",
        EXPERIMENTAL / "_rest.py",
        '        market: str = "KR",\n    ) -> None:',
        '        market: str = "KR",\n        virtual: bool = False,\n    ) -> None:',
    ),
    # --- B′2: the second-round conditions on the Toss venue regression
    Mutation(
        "B'2-1",
        "the contradictory pair (requires a venue, declares none) is constructible again",
        SRC / "models/capabilities.py",
        "        if self.requires_venue and not self.venues:",
        "        if False:",
    ),
    Mutation(
        "B'2-1",
        "Toss extends its capabilities through model_copy again, skipping validation",
        SRC / "brokers/toss.py",
        "        return Capabilities(**{**base.model_dump(), **update})",
        "        return base.model_copy(update=update)",
    ),
    Mutation(
        "B'2-1",
        "the base default hands US the empty venue list instead of locking",
        SRC / "brokers/_common.py",
        '        if self.market == "US" and not self.us_venues:',
        "        if False:",
    ),
    Mutation(
        "B'2-2",
        "Toss opens the unverified overseas market again",
        SRC / "brokers/toss.py",
        '        if self.market != "KR":',
        "        if False:",
    ),
    Mutation(
        "B'2-2",
        "Toss claims the close-order type its locked overseas session would need",
        SRC / "brokers/toss.py",
        '        if request.order_type == "close":',
        "        if False:",
    ),
    Mutation(
        "A'2-5",
        "the dev extra drops the package the suite imports",
        ROOT / "pyproject.toml",
        '"build>=1", "mcp[cli]>=1.0,<2"]',
        '"build>=1"]',
    ),
)


def _suite_is_red() -> bool:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-x", "--no-header", "-p", "no:cacheprovider"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    return result.returncode != 0


def _apply(mutation: Mutation) -> bool:
    original = mutation.path.read_text()
    if mutation.before not in original:
        raise SystemExit(f"anchor missing, mutation is stale: {mutation.condition} {mutation.name}")
    try:
        mutation.path.write_text(original.replace(mutation.before, mutation.after, 1))
        return _suite_is_red()
    finally:
        mutation.path.write_text(original)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("condition", nargs="?", help="run only this condition, e.g. A'-2")
    selected = [m for m in MUTATIONS if parser.parse_args().condition in (None, m.condition)]
    if not selected:
        raise SystemExit("no mutation matches that condition")
    survivors = []
    for mutation in selected:
        red = _apply(mutation)
        print(f"{'RED  ' if red else 'ALIVE'} {mutation.condition}  {mutation.name}")
        if not red:
            survivors.append(mutation)
    print(f"\n{len(selected) - len(survivors)}/{len(selected)} mutations killed")
    for mutation in survivors:
        print(f"unguarded: {mutation.condition} {mutation.name}")
    return 1 if survivors else 0


if __name__ == "__main__":
    raise SystemExit(main())
