"""
RedStone Bolt ETH/USD on-chain feed via MegaETH RPC.

Polls latestRoundData() on the MegaETH ETH price-feed adapter
(0xc555c100DB24dF36D406243642C169CC5A937f09) every ~300 ms. Per
REDSTONE_FEED_RESEARCH.md the adapter publishes at ~1.5 Hz (every ~666 ms)
with microsecond-precision `startedAt` timestamps. This is the SAME price
source Euphoria settles against (RedStone primary-prod, 5-signer threshold-1).

Why this matters: Coinbase WS leads/lags the global RedStone median by tens
of cents in trending regimes. Dryrun bets resolved against Coinbase
systematically disagree with LIVE settlements. This feed gives us
ground-truth-equivalent prices for dry-run resolution.

Shape matches CoinbaseFeed / BinanceFeed: start(), stop(), latest(),
window(ms), realized_vol_per_sec(window_ms).
"""
import asyncio
import json
import os
import ssl
import statistics
import tempfile
import threading
import time
import urllib.request
import urllib.error
from collections import deque
from dataclasses import dataclass

import certifi


_SSL_CTX = ssl.create_default_context(cafile=certifi.where())


# AggregatorV3Interface.latestRoundData() selector
LATEST_ROUND_DATA_SELECTOR = "0xfeaf968c"
# RedStone Bolt MegaETH ETH adapter (Chainlink-compatible, decimals=8)
ADAPTER_ADDRESS = "0xc555c100DB24dF36D406243642C169CC5A937f09"
DEFAULT_RPC_URL = "https://mainnet.megaeth.com/rpc"

# ---- Shared cross-process cache (file-based) ----
# Multiple bots running concurrently all query the same adapter every ~666ms.
# Without sharing, N bots ⇒ N× RPC pressure → rate limits.
# With this cache, only the bot whose poll arrives first after TTL expires
# does the real RPC; everyone else reads the file. N bots ⇒ ~1× RPC pressure.
CACHE_PATH = os.path.join(tempfile.gettempdir(), "redstone_eth_cache.json")
CACHE_TTL_MS = 500  # serve cached values up to this age; matches RS's ~666ms publish cadence

def _read_cache() -> dict | None:
    try:
        with open(CACHE_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return None

def _write_cache(round_id: int, answer: int, started_at_us: int) -> None:
    tmp = CACHE_PATH + f".tmp.{os.getpid()}"
    try:
        with open(tmp, "w") as f:
            json.dump({
                "fetched_ms": int(time.time() * 1000),
                "round_id": round_id,
                "answer": answer,
                "started_at_us": started_at_us,
            }, f)
        os.replace(tmp, CACHE_PATH)  # atomic on POSIX
    except OSError:
        pass  # cache write failure is non-fatal


@dataclass
class RsTick:
    """One on-chain RedStone update."""
    ts_ms: int       # local time the poll completed (for compatibility)
    started_at_ms: int  # on-chain `startedAt` timestamp in ms (true publish time)
    round_id: int
    price: float     # USD


def _eth_call_uncached(rpc_url: str, timeout: float = 0.8) -> tuple[int, int, int] | None:
    """Direct RPC call (no cache). Returns (round_id, answer, started_at_us)."""
    body = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_call",
        "params": [
            {"to": ADAPTER_ADDRESS, "data": LATEST_ROUND_DATA_SELECTOR},
            "latest",
        ],
    }).encode()
    req = urllib.request.Request(
        rpc_url, data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0",  # required — MegaETH RPC 403s default urllib
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
            payload = json.loads(resp.read().decode())
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
            json.JSONDecodeError, OSError):
        return None
    if "result" not in payload:
        return None
    hex_data = payload["result"]
    if not hex_data.startswith("0x") or len(hex_data) < 2 + 5 * 64:
        return None
    h = hex_data[2:]
    round_id = int(h[0:64], 16)
    answer_raw = int(h[64:128], 16)
    if answer_raw >= 1 << 255:
        answer_raw -= 1 << 256
    started_at = int(h[128:192], 16)
    return round_id, answer_raw, started_at


def _eth_call_latest(rpc_url: str, timeout: float = 0.8) -> tuple[int, int, int] | None:
    """Cached RPC call. Returns cached value if fresh; else fetches and updates cache."""
    cached = _read_cache()
    if cached:
        age_ms = int(time.time() * 1000) - cached.get("fetched_ms", 0)
        if 0 <= age_ms < CACHE_TTL_MS:
            return cached["round_id"], cached["answer"], cached["started_at_us"]
    result = _eth_call_uncached(rpc_url, timeout)
    if result is not None:
        _write_cache(*result)
    return result


class RedStoneChainFeed:
    """Polls RedStone Bolt ETH adapter on MegaETH ~3x/sec."""

    def __init__(self, rpc_url: str = DEFAULT_RPC_URL,
                 poll_interval_ms: int = 500,
                 maxlen: int = 20000):
        self.rpc_url = rpc_url
        self.poll_interval_ms = poll_interval_ms
        self._tape: deque = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.connected = False
        self.last_error: str | None = None
        self.msg_count = 0
        self._last_round_id = -1

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="redstone-rpc")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        backoff = 0.5
        while not self._stop.is_set():
            poll_start = time.time()
            res = _eth_call_latest(self.rpc_url)
            if res is None:
                self.connected = False
                self.last_error = "eth_call failed"
                time.sleep(min(backoff, 5.0))
                backoff = min(backoff * 1.5, 5.0)
                continue
            backoff = 0.5
            self.connected = True
            self.last_error = None
            round_id, answer, started_at = res
            # `startedAt` is in MICROseconds (RedStone Bolt uses µs precision
            # in the timestamp field, not seconds as Chainlink-classic does).
            started_at_ms = started_at // 1000
            # Skip duplicates — adapter publishes every ~666ms.
            if started_at_ms == self._last_round_id:
                elapsed_ms = (time.time() - poll_start) * 1000
                rem = max(50, self.poll_interval_ms - elapsed_ms)
                time.sleep(rem / 1000.0)
                continue
            self._last_round_id = started_at_ms  # dedupe by startedAt, not roundId
            price = answer / 1e8
            tick = RsTick(
                ts_ms=int(time.time() * 1000),
                started_at_ms=started_at_ms,
                round_id=round_id,
                price=price,
            )
            with self._lock:
                self._tape.append(tick)
                self.msg_count += 1
            elapsed_ms = (time.time() - poll_start) * 1000
            rem = max(50, self.poll_interval_ms - elapsed_ms)
            time.sleep(rem / 1000.0)

    def latest(self) -> RsTick | None:
        with self._lock:
            return self._tape[-1] if self._tape else None

    def window(self, window_ms: int) -> list[RsTick]:
        """Returns ticks whose started_at_ms falls within [now-window, now]."""
        cutoff = int(time.time() * 1000) - window_ms
        with self._lock:
            return [t for t in self._tape if t.started_at_ms >= cutoff]

    def window_at(self, start_ms: int, end_ms: int) -> list[RsTick]:
        """Returns ticks whose started_at_ms falls within [start_ms, end_ms]."""
        with self._lock:
            return [t for t in self._tape
                    if start_ms <= t.started_at_ms <= end_ms]

    def realized_vol_per_sec(self, window_ms: int = 60_000) -> float | None:
        ticks = self.window(window_ms)
        if len(ticks) < 5:
            return None
        # 1-second bucketed (last tick per second)
        buckets: dict[int, float] = {}
        for t in ticks:
            buckets[t.started_at_ms // 1000] = t.price
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


if __name__ == "__main__":
    # Smoke test: poll for 20s, print stats
    import sys
    feed = RedStoneChainFeed()
    feed.start()
    print("polling redstone for 20s...", flush=True)
    time.sleep(20)
    feed.stop()
    print(f"connected={feed.connected} last_error={feed.last_error}")
    print(f"msg_count={feed.msg_count}")
    latest = feed.latest()
    if latest:
        age = int(time.time() * 1000) - latest.started_at_ms
        print(f"latest: round={latest.round_id} price=${latest.price:.4f} "
              f"startedAt={latest.started_at_ms}ms age={age}ms")
    win = feed.window(20_000)
    if len(win) >= 2:
        deltas = [win[i+1].started_at_ms - win[i].started_at_ms
                  for i in range(len(win)-1)]
        deltas_sorted = sorted(deltas)
        med = deltas_sorted[len(deltas)//2]
        print(f"ticks in 20s: {len(win)}  median Δ between rounds: {med}ms")
        print(f"vol_per_sec: {feed.realized_vol_per_sec(20_000)}")
