"""
Coinbase Exchange ETH-USD ticker WebSocket subscriber.

Coinbase Pro ETH-USD is one of RedStone oracle's primary aggregator sources, so
this feed is the closest external reference to what Euphoria settles against
(unlike Binance ETH/USDT which carries a USDT-USD basis).

Same threading/locking model as BinanceFeed.

Channel: "ticker" — one message per trade, fields include time, price.
URL: wss://ws-feed.exchange.coinbase.com
"""
import asyncio
import json
import ssl
import statistics
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone

import certifi
import websockets


_SSL_CTX = ssl.create_default_context(cafile=certifi.where())


@dataclass
class Trade:
    ts_ms: int
    price: float


def _parse_iso_ms(s: str) -> int:
    # "2026-05-21T10:14:32.123456Z" — always UTC; treat as such (naive .timestamp()
    # assumes local time, which is wrong if local != UTC).
    s2 = s.rstrip("Z")
    if "." in s2:
        head, frac = s2.split(".")
        frac = (frac + "000000")[:6]
        s2 = f"{head}.{frac}"
        dt = datetime.strptime(s2, "%Y-%m-%dT%H:%M:%S.%f")
    else:
        dt = datetime.strptime(s2, "%Y-%m-%dT%H:%M:%S")
    dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


class CoinbaseFeed:
    def __init__(self, product_id: str = "ETH-USD", maxlen: int = 20000):
        self.product_id = product_id
        self._tape: deque = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self.connected = False
        self.last_error: str | None = None
        self.msg_count = 0

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True, name="coinbase-ws")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        try:
            asyncio.run(self._async_main())
        except Exception as e:
            self.last_error = f"loop_exit: {e}"

    async def _async_main(self) -> None:
        url = "wss://ws-feed.exchange.coinbase.com"
        sub = json.dumps({
            "type": "subscribe",
            "product_ids": [self.product_id],
            "channels": ["ticker"],
        })
        backoff = 1.0
        while not self._stop.is_set():
            try:
                async with websockets.connect(
                    url, ping_interval=10, ping_timeout=5, ssl=_SSL_CTX,
                ) as ws:
                    await ws.send(sub)
                    self.connected = True
                    self.last_error = None
                    backoff = 1.0
                    async for msg in ws:
                        if self._stop.is_set():
                            break
                        data = json.loads(msg)
                        if data.get("type") != "ticker":
                            continue
                        if data.get("product_id") != self.product_id:
                            continue
                        ts_str = data.get("time")
                        price_str = data.get("price")
                        if not ts_str or not price_str:
                            continue
                        try:
                            ts = _parse_iso_ms(ts_str)
                            price = float(price_str)
                        except (ValueError, KeyError):
                            continue
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
        trades = self.window(window_ms)
        if len(trades) < 5:
            return None
        buckets: dict[int, float] = {}
        for t in trades:
            buckets[t.ts_ms // 1000] = t.price
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
    f = CoinbaseFeed()
    f.start()
    time.sleep(8)
    print(f"connected={f.connected} msgs={f.msg_count} last={f.latest()} err={f.last_error}")
    print(f"vol_30s={f.realized_vol_per_sec(30_000)}")
    f.stop()
