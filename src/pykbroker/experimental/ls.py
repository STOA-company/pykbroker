"""Compatibility alias; the formal LS module owns the same default-closed lock."""

import sys

from pykbroker.brokers import ls

sys.modules[__name__] = ls
