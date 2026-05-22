"""
ProxyFeed — drop-in replacement for BinanceFeed/CoinbaseFeed that reads
external market data from the signing_proxy's centralized feeds instead of
opening its own WebSocket. Solves:

  - Startup-race when many bots open WS simultaneously (we saw 4 bots fail
    to connect Coinbase in run 3).
  - Rate-limiting / connection limits on Coinbase / Binance.
  - Data-consistency: all bots see the exact same tape from one source.

Caches each tape locally with a short TTL so repeated calls in the same
bot iteration don't all hit the proxy.

Same public API as BinanceFeed / CoinbaseFeed:
  .start(), .stop(), .connected, .msg_count, .last_error
  .latest() -> Trade | None
  .window(window_ms) -> list[Trade]
  .realized_vol_per_sec(window_ms) -> float | None
"""
import json
import os
import statistics
import time
import urllib.request
from collections import deque
from dataclasses import dataclass


PROXY = os.environ.get("PROXY", "http://127.0.0.1:8787")
CACHE_TTL_MS = 500


@dataclass
class Trade:
    ts_ms: int
    price: float


class ProxyFeed:
    def __init__(self, endpoint: str, label: str = ""):
        self.endpoint = endpoint
        self.label = label or endpoint
        self._cache_ts: float = 0.0
        self._cache_window_ms: int = 0
        self._cache_ticks: list[tuple[int, float]] = []
        self._latest: Trade | None = None
        self._latest_age_ms: int | None = None
        self.connected: bool = False
        self.msg_count: int = 0
        self.last_error: str | None = None

    def start(self) -> None:
        # Warm cache.
        self._refresh(60_000)

    def stop(self) -> None:
        pass

    def _fetch(self, window_ms: int) -> dict | None:
        try:
            with urllib.request.urlopen(
                f"{PROXY}{self.endpoint}?window_ms={window_ms}", timeout=3
            ) as r:
                return json.loads(r.read())
        except Exception as e:
            self.last_error = f"fetch_err: {e}"
            return None

    def _refresh(self, window_ms: int) -> None:
        # Always fetch at least 120s so vol computations have headroom.
        fetch_window = max(window_ms, 120_000)
        d = self._fetch(fetch_window)
        if d is None:
            self.connected = False
            return
        self.connected = bool(d.get("connected", False))
        self.msg_count = int(d.get("msgCount", 0))
        self.last_error = d.get("lastError")
        ticks_raw = d.get("ticks") or []
        self._cache_ticks = ticks_raw
        latest = d.get("latest")
        if latest:
            self._latest = Trade(int(latest["ts_ms"]), float(latest["price"]))
            self._latest_age_ms = d.get("latestAgeMs")
        self._cache_ts = time.time()
        self._cache_window_ms = fetch_window

    def _ensure_fresh(self, window_ms: int) -> None:
        age_ms = (time.time() - self._cache_ts) * 1000
        if age_ms > CACHE_TTL_MS or self._cache_window_ms < window_ms:
            self._refresh(window_ms)

    def latest(self) -> Trade | None:
        self._ensure_fresh(2_000)
        return self._latest

    def window(self, window_ms: int) -> list[Trade]:
        self._ensure_fresh(window_ms)
        cutoff = int(time.time() * 1000) - window_ms
        return [Trade(ts, p) for (ts, p) in self._cache_ticks if ts >= cutoff]

    def realized_vol_per_sec(self, window_ms: int = 60_000) -> float | None:
        self._ensure_fresh(window_ms)
        trades = [(ts, p) for (ts, p) in self._cache_ticks if ts >= int(time.time() * 1000) - window_ms]
        if len(trades) < 5:
            return None
        buckets: dict[int, float] = {}
        for ts, p in trades:
            buckets[ts // 1000] = p
        secs = sorted(buckets.keys())
        if len(secs) < 5:
            return None
        prices = [buckets[s] for s in secs]
        diffs = []
        for i in range(len(secs) - 1):
            dt = secs[i + 1] - secs[i]
            if dt <= 0 or dt > 3:
                continue
            diffs.append((prices[i + 1] - prices[i]) / (dt ** 0.5))
        if len(diffs) < 4:
            return None
        return statistics.stdev(diffs)


def BinanceFeed(*_, **__) -> ProxyFeed:
    return ProxyFeed("/binance_tape", label="binance")


def CoinbaseFeed(*_, **__) -> ProxyFeed:
    return ProxyFeed("/coinbase_tape", label="coinbase")
