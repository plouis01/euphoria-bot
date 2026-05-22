"""
Binance ETH/USDT @trade WebSocket subscriber.

Runs in a background thread with its own asyncio loop. Maintains a rolling
tape of trades; exposes thread-safe reads for the bot's main thread.

WS endpoint: wss://stream.binance.com/ws/ethusdt@trade
Free, no auth, ~1 message per real trade.
"""
import asyncio
import json
import ssl
import statistics
import threading
import time
from collections import deque
from dataclasses import dataclass

import certifi
import websockets


_SSL_CTX = ssl.create_default_context(cafile=certifi.where())


@dataclass
class Trade:
    ts_ms: int   # Binance server trade timestamp
    price: float


class BinanceFeed:
    def __init__(self, symbol: str = "ethusdt", maxlen: int = 20000):
        self.symbol = symbol
        self._tape: deque = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self.connected = False
        self.last_error: str | None = None
        self.msg_count = 0

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True, name="binance-ws")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        try:
            asyncio.run(self._async_main())
        except Exception as e:
            self.last_error = f"loop_exit: {e}"

    async def _async_main(self) -> None:
        url = f"wss://stream.binance.com/ws/{self.symbol}@trade"
        backoff = 1.0
        while not self._stop.is_set():
            try:
                async with websockets.connect(
                    url, ping_interval=10, ping_timeout=5, ssl=_SSL_CTX,
                ) as ws:
                    self.connected = True
                    self.last_error = None
                    backoff = 1.0
                    async for msg in ws:
                        if self._stop.is_set():
                            break
                        data = json.loads(msg)
                        ts = int(data["T"])
                        price = float(data["p"])
                        with self._lock:
                            self._tape.append(Trade(ts, price))
                            self.msg_count += 1
            except Exception as e:
                self.connected = False
                self.last_error = str(e)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    def latest(self) -> Trade | None:
        with self._lock:
            return self._tape[-1] if self._tape else None

    def window(self, window_ms: int) -> list[Trade]:
        cutoff = int(time.time() * 1000) - window_ms
        with self._lock:
            return [t for t in self._tape if t.ts_ms >= cutoff]

    def realized_vol_per_sec(self, window_ms: int = 60_000) -> float | None:
        """σ in $ / √sec from 1-second-bucketed arithmetic price differences.

        Returns None if fewer than 5 1-sec buckets exist.
        """
        trades = self.window(window_ms)
        if len(trades) < 5:
            return None
        buckets: dict[int, float] = {}
        for t in trades:
            buckets[t.ts_ms // 1000] = t.price  # last price in each 1s bucket
        secs = sorted(buckets.keys())
        if len(secs) < 5:
            return None
        # Only use contiguous seconds; if there are gaps the diff is over >1s.
        prices = [buckets[s] for s in secs]
        diffs = []
        for i in range(len(secs) - 1):
            dt = secs[i + 1] - secs[i]
            if dt <= 0 or dt > 3:
                continue
            # Normalize to per-sqrt-second
            diffs.append((prices[i + 1] - prices[i]) / (dt ** 0.5))
        if len(diffs) < 4:
            return None
        return statistics.stdev(diffs)
