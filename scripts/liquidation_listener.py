"""Listen to ETH perpetual liquidation streams across multiple CEX.

Free real-time liquidation tape for cross-referencing with bot signals.
Hypothesis: when liquidations cluster, ETH realized vol spikes and our
touch_prob model breaks. A clustering detector could be an ex-ante regime
gate.

Venues:
  - Binance USDT-M futures: ETHUSDT  (wss://fstream.binance.com)
  - Bybit linear perp:      ETHUSDT  (wss://stream.bybit.com/v5/public/linear)
  - OKX swap:               ETH-USDT-SWAP (wss://ws.okx.com:8443/ws/v5/public)

Venues NOT covered here (and why):
  - Hyperliquid: no SYSTEM-WIDE liquidation WS — only per-user via
    userEvents/userFills subscriptions. Large HL prints are captured
    in large_print_listener.py instead; post-hoc address matching against
    HL liquidator addresses would be required for liquidation classification.
  - Coinbase: spot only on main exchange (no perps, no liquidations)
  - Coinbase INTX: requires paid auth for clean liquidation tape

NOTE: historical aggregated heatmap data (CoinGlass-style) requires paid
access. This listener only captures going-forward. Backtest against past
bet logs is not feasible without paid CoinGlass / Coinalyze data.

Output: logs/liquidations_<TS>.jsonl
Each line: {"ts_ms", "venue", "symbol", "side", "qty", "price", "usd"}
"""
import asyncio
import json
import ssl
import sys
import time
from pathlib import Path
import certifi
import websockets

ROOT = Path(__file__).resolve().parent.parent
SSL_CTX = ssl.create_default_context(cafile=certifi.where())
RUNTIME_SEC = int(sys.argv[1]) if len(sys.argv) > 1 else 86400

ts_label = time.strftime("%Y%m%d_%H%M%S")
out_path = ROOT / "logs" / f"liquidations_{ts_label}.jsonl"
print(f"output: {out_path}", flush=True)

# Shared writer
out_file = open(out_path, "w")
counts = {"binance": 0, "bybit": 0, "okx": 0}
last_status = [time.time()]
write_lock = asyncio.Lock()


async def write_event(rec):
    async with write_lock:
        out_file.write(json.dumps(rec) + "\n"); out_file.flush()
        counts[rec["venue"]] = counts.get(rec["venue"], 0) + 1
        if time.time() - last_status[0] > 60:
            print(f"  [{int(time.time())}] counts={counts} latest: "
                  f"{rec['venue']} {rec['side']} ${rec['usd']:,.0f}", flush=True)
            last_status[0] = time.time()


async def binance_feed():
    """Binance USDT-M futures forceOrder stream (ALL symbols, we filter ETH)."""
    url = "wss://fstream.binance.com/ws/!forceOrder@arr"
    bk = 1.0
    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ssl=SSL_CTX) as ws:
                bk = 1.0
                async for msg in ws:
                    try: d = json.loads(msg)
                    except: continue
                    o = d.get("o", {})
                    if o.get("s") != "ETHUSDT": continue
                    qty = float(o.get("q", 0))
                    price = float(o.get("ap", 0) or o.get("p", 0))
                    await write_event({
                        "ts_ms": int(o.get("T", time.time() * 1000)),
                        "venue": "binance", "symbol": "ETHUSDT",
                        "side": o.get("S"), "qty": qty, "price": price,
                        "usd": qty * price,
                    })
        except Exception as e:
            print(f"  binance err: {e}", flush=True)
            await asyncio.sleep(bk); bk = min(bk * 2, 60)


async def bybit_feed():
    """Bybit V5 linear perps liquidation topic for ETHUSDT.
    https://bybit-exchange.github.io/docs/v5/websocket/public/all-liquidation"""
    url = "wss://stream.bybit.com/v5/public/linear"
    sub = json.dumps({"op": "subscribe", "args": ["allLiquidation.ETHUSDT"]})
    bk = 1.0
    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ssl=SSL_CTX) as ws:
                await ws.send(sub); bk = 1.0
                async for msg in ws:
                    try: d = json.loads(msg)
                    except: continue
                    if not d.get("topic", "").startswith("allLiquidation"): continue
                    for evt in d.get("data", []):
                        qty = float(evt.get("v", 0) or evt.get("size", 0))
                        price = float(evt.get("p", 0) or evt.get("price", 0))
                        side = evt.get("S") or evt.get("side")
                        ts_ms = int(evt.get("T") or evt.get("updatedTime") or time.time() * 1000)
                        await write_event({
                            "ts_ms": ts_ms,
                            "venue": "bybit", "symbol": "ETHUSDT",
                            "side": side, "qty": qty, "price": price,
                            "usd": qty * price,
                        })
        except Exception as e:
            print(f"  bybit err: {e}", flush=True)
            await asyncio.sleep(bk); bk = min(bk * 2, 60)


async def okx_feed():
    """OKX V5 public liquidation-orders for SWAP instType.
    https://www.okx.com/docs-v5/en/#public-data-websocket-liquidation-orders-channel"""
    url = "wss://ws.okx.com:8443/ws/v5/public"
    sub = json.dumps({"op": "subscribe", "args": [
        {"channel": "liquidation-orders", "instType": "SWAP"}
    ]})
    bk = 1.0
    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ssl=SSL_CTX) as ws:
                await ws.send(sub); bk = 1.0
                async for msg in ws:
                    try: d = json.loads(msg)
                    except: continue
                    if d.get("arg", {}).get("channel") != "liquidation-orders": continue
                    for inst in d.get("data", []):
                        if not inst.get("instId", "").startswith("ETH-USDT"): continue
                        for detail in inst.get("details", []):
                            qty = float(detail.get("sz", 0))
                            price = float(detail.get("bkPx", 0))
                            side = detail.get("side", "").upper()
                            ts_ms = int(detail.get("ts", time.time() * 1000))
                            # OKX 'sz' is in contracts, multiplier varies. For
                            # ETH-USDT-SWAP, 1 contract = 0.1 ETH typically.
                            # Compute USD as qty * price * 0.1 (best-effort)
                            usd = qty * price * 0.1
                            await write_event({
                                "ts_ms": ts_ms, "venue": "okx",
                                "symbol": inst.get("instId"),
                                "side": side, "qty": qty, "price": price,
                                "usd": usd,
                            })
        except Exception as e:
            print(f"  okx err: {e}", flush=True)
            await asyncio.sleep(bk); bk = min(bk * 2, 60)


async def main():
    start = time.time()
    print(f"runtime: {RUNTIME_SEC}s   venues: binance, bybit, okx", flush=True)
    tasks = [
        asyncio.create_task(binance_feed()),
        asyncio.create_task(bybit_feed()),
        asyncio.create_task(okx_feed()),
    ]
    try:
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=RUNTIME_SEC)
    except asyncio.TimeoutError:
        pass
    finally:
        for t in tasks: t.cancel()
        out_file.close()
    print(f"DONE: counts={counts}", flush=True)


asyncio.run(main())
