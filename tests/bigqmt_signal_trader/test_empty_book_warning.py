# coding: utf-8
"""get_ticks warns once a minute per code when a quote carries no price on either side."""

import sys
import time as real_time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from bigqmt_signal_trader.adapters import market_bigqmt  # noqa: E402
from bigqmt_signal_trader.adapters.market_bigqmt import BigQmtMarketDataProvider  # noqa: E402


class _Context(object):
    def __init__(self, ticks):
        self.ticks = ticks

    def get_full_tick(self, codes):
        return dict((c, self.ticks[c]) for c in codes if c in self.ticks)


class _Clock(object):
    def __init__(self):
        self.now = 1000.0

    def __getattr__(self, name):
        return getattr(real_time, name)

    def monotonic(self):
        return self.now


class _Log(object):
    def __init__(self):
        self.warnings = []

    def warning(self, message, *args):
        self.warnings.append(message % args if args else message)

    def __getattr__(self, name):
        return lambda *a, **k: None


@pytest.fixture
def env(monkeypatch):
    clock, log = _Clock(), _Log()
    monkeypatch.setattr(market_bigqmt, "time", clock)
    monkeypatch.setattr(market_bigqmt, "log", log)
    return clock, log


def _provider(ticks):
    return BigQmtMarketDataProvider(_Context(ticks))


def test_no_price_either_side_warns_with_the_fields(env):
    _, log = env
    bad = {"timetag": "20261104 09:31:00", "lastPrice": 0.0,
           "askPrice": [0, 0, 0, 0, 0], "bidPrice": [0, 0, 0, 0, 0]}
    ticks = _provider({"600000.SH": bad}).get_ticks(["600000"])

    assert "600000.SH" in ticks
    assert len(log.warnings) == 1
    line = log.warnings[0]
    for expected in ("600000.SH", "20261104 09:31:00", "lastPrice=0.0",
                     "askPrice=[0, 0, 0, 0, 0]", "bidPrice=[0, 0, 0, 0, 0]"):
        assert expected in line


@pytest.mark.parametrize("quote", [
    {"lastPrice": 10.0, "askPrice": [0, 0, 0, 0, 0], "bidPrice": [10.0, 9.9, 0, 0, 0]},
    {"lastPrice": 11.0, "askPrice": [11.0, 0, 0, 0, 0], "bidPrice": [0, 0, 0, 0, 0]},
    {"lastPrice": 10.0, "askPrice": [10.1, 10.2], "bidPrice": [10.0, 9.9]},
])
def test_a_quote_with_a_price_on_either_side_is_silent(env, quote):
    _, log = env
    _provider({"600000.SH": quote}).get_ticks(["600000.SH"])
    assert log.warnings == []


def test_rate_limited_to_one_line_per_code_per_minute(env):
    clock, log = env
    bad = {"lastPrice": 0, "askPrice": [0], "bidPrice": [0]}
    provider = _provider({"600000.SH": bad, "000001.SZ": bad})

    provider.get_ticks(["600000.SH", "000001.SZ"])
    assert len(log.warnings) == 2            # one per code
    clock.now += 30
    provider.get_ticks(["600000.SH", "000001.SZ"])
    assert len(log.warnings) == 2            # still inside the minute
    clock.now += 31
    provider.get_ticks(["600000.SH"])
    assert len(log.warnings) == 3            # minute passed, only the asked code


def test_a_broken_quote_shape_never_breaks_get_ticks(env):
    _, log = env
    ticks = _provider({"600000.SH": "not a dict"}).get_ticks(["600000.SH"])
    assert ticks == {"600000.SH": "not a dict"}
