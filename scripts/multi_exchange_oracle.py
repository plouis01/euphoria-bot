"""Build a custom multi-exchange ETH/USD price oracle.

Subscribes to WS feeds from multiple exchanges, computes median price every
100ms, and logs both the median and individual venue prices. Used to:
  1. Compare median latency vs RedStone publish
  2. Detect cross-venue divergence
  3. Replay later as a signal source

Output: logs/multi_oracle_<TS>.jsonl

Each line:
  {"ts_ms": int, "median": float, "venues": {"binance": 2095.30, "coinbase": 2095.42, ...}}
"""
import asyncio
import json
import ssl
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path
import certifi
import websockets

ROOT = Path(__file__).resolve().parent.parent
SSL_CTX = ssl.create_default_context(cafile=certifi.where())
RUNTIME_SEC = int(sys.argv[1]) if len(sys.argv) > 1 else 36000  # 10h default
LOG_INTERVAL_MS = 100

ts_label = time.strftime("%Y%m%d_%H%M%S")
out_path = ROOT / "logs" / f"multi_oracle_{ts_label}.jsonl"
print(f"output: {out_path}", flush=True)
print(f"runtime: {RUNTIME_SEC}s", flush=True)

# Shared state
prices: dict[str, tuple[float, float]] = {}  # venue -> (price, ts)
lock = asyncio.Lock()


async def binance_feed():
    url = "wss://stream.binance.com/ws/ethusdt@trade"
    backoff = 1.0
    while True:
        try:
            async with websockets.connect(url, ping_interval=10, ssl=SSL_CTX) as ws:
                backoff = 1.0
                async for msg in ws:
                    d = json.loads(msg)
                    p = float(d.get("p", 0))
                    if p > 0:
                        async with lock:
                            prices["binance"] = (p, time.time())
        except Exception as e:
            print(f"binance err: {e}", flush=True)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)


async def coinbase_feed():
    url = "wss://ws-feed.exchange.coinbase.com"
    sub = json.dumps({"type": "subscribe", "product_ids": ["ETH-USD"],
                       "channels": ["ticker"]})
    backoff = 1.0
    while True:
        try:
            async with websockets.connect(url, ping_interval=10, ssl=SSL_CTX) as ws:
                await ws.send(sub)
                backoff = 1.0
                async for msg in ws:
                    d = json.loads(msg)
                    if d.get("type") != "ticker":
                        continue
                    p = float(d.get("price", 0))
                    if p > 0:
                        async with lock:
                            prices["coinbase"] = (p, time.time())
        except Exception as e:
            print(f"coinbase err: {e}", flush=True)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)


async def okx_feed():
    url = "wss://ws.okx.com:8443/ws/v5/public"
    sub = json.dumps({"op": "subscribe",
                       "args": [{"channel": "tickers", "instId": "ETH-USDT"}]})
    backoff = 1.0
    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ssl=SSL_CTX) as ws:
                await ws.send(sub)
                backoff = 1.0
                async for msg in ws:
                    d = json.loads(msg)
                    data = d.get("data") or []
                    for tick in data:
                        p = float(tick.get("last", 0))
                        if p > 0:
                            async with lock:
                                prices["okx"] = (p, time.time())
        except Exception as e:
            print(f"okx err: {e}", flush=True)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)


async def kraken_feed():
    url = "wss://ws.kraken.com"
    sub = json.dumps({"event": "subscribe", "pair": ["ETH/USD"],
                       "subscription": {"name": "ticker"}})
    backoff = 1.0
    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ssl=SSL_CTX) as ws:
                await ws.send(sub)
                backoff = 1.0
                async for msg in ws:
                    d = json.loads(msg)
                    if isinstance(d, list) and len(d) >= 4 and d[2] == "ticker":
                        # d[1] is ticker data, "c" is close price: ["price", "volume"]
                        try:
                            p = float(d[1]["c"][0])
                            if p > 0:
                                async with lock:
                                    prices["kraken"] = (p, time.time())
                        except (KeyError, IndexError, TypeError):
                            pass
        except Exception as e:
            print(f"kraken err: {e}", flush=True)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)


async def bybit_feed():
    url = "wss://stream.bybit.com/v5/public/spot"
    sub = json.dumps({"op": "subscribe", "args": ["tickers.ETHUSDT"]})
    backoff = 1.0
    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ssl=SSL_CTX) as ws:
                await ws.send(sub)
                backoff = 1.0
                async for msg in ws:
                    d = json.loads(msg)
                    data = d.get("data")
                    if isinstance(data, dict):
                        p = float(data.get("lastPrice", 0))
                        if p > 0:
                            async with lock:
                                prices["bybit"] = (p, time.time())
        except Exception as e:
            print(f"bybit err: {e}", flush=True)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)


async def logger():
    out = open(out_path, "w")
    start = time.time()
    n_writes = 0
    last_status = start
    while time.time() - start < RUNTIME_SEC:
        async with lock:
            now = time.time()
            # Only use prices that are fresh (< 5s old)
            fresh = {v: p for v, (p, ts) in prices.items() if now - ts < 5}
            if len(fresh) >= 2:
                med = statistics.median(fresh.values())
                rec = {"ts_ms": int(now * 1000), "median": med, "venues": fresh}
                out.write(json.dumps(rec) + "\n")
                n_writes += 1
        if now - last_status > 60:
            last_status = now
            out.flush()
            elapsed = int(now - start)
            print(f"  [{elapsed}s] writes={n_writes} venues={list(prices.keys())}", flush=True)
        await asyncio.sleep(LOG_INTERVAL_MS / 1000.0)
    out.close()
    print(f"DONE: writes={n_writes}", flush=True)


async def main():
    await asyncio.gather(
        binance_feed(), coinbase_feed(), okx_feed(), kraken_feed(), bybit_feed(),
        logger(),
    )


if __name__ == "__main__":
    asyncio.run(main())
