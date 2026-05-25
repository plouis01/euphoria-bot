"""Listen to Binance futures liquidation stream for ETH and write to disk.

Binance exposes a free WS stream of all futures liquidations:
  wss://fstream.binance.com/ws/!forceOrder@arr

Each message is a single liquidation across any symbol. We filter for ETH-USDT
perp and log timestamp + side + size_usd. This gives a free, real-time
liquidation tape we can cross-reference with our DRY bot signals.

Hypothesis (per STALEQUANT_REVIEW.md task 2): when liquidations cluster, ETH
realized vol spikes and our touch_prob model breaks. A clustering detector
could be an ex-ante regime gate.

NOTE: historical CoinGlass-style aggregated heatmap data isn't freely
available. This listener only captures going-forward. Backtest against past
bet logs is not feasible without paid CoinGlass / Coinalyze / TheBlock data.

Output: logs/liquidations_<TS>.jsonl
Each line: {"ts_ms": int, "symbol": "ETHUSDT", "side": "BUY"|"SELL",
            "qty": float, "price": float, "usd": float}
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


async def main():
    url = "wss://fstream.binance.com/ws/!forceOrder@arr"
    out = open(out_path, "w")
    start = time.time()
    n_total = 0
    n_eth = 0
    last_log = 0
    backoff = 1.0
    while time.time() - start < RUNTIME_SEC:
        try:
            async with websockets.connect(url, ping_interval=20, ssl=SSL_CTX) as ws:
                backoff = 1.0
                async for msg in ws:
                    if time.time() - start >= RUNTIME_SEC: break
                    try:
                        d = json.loads(msg)
                    except: continue
                    o = d.get("o", {})
                    sym = o.get("s")
                    if not sym: continue
                    n_total += 1
                    if sym != "ETHUSDT": continue
                    qty = float(o.get("q", 0))
                    price = float(o.get("ap", 0) or o.get("p", 0))
                    side = o.get("S")
                    rec = {
                        "ts_ms": int(o.get("T", time.time() * 1000)),
                        "symbol": sym, "side": side,
                        "qty": qty, "price": price, "usd": qty * price,
                    }
                    out.write(json.dumps(rec) + "\n")
                    out.flush()
                    n_eth += 1
                    if time.time() - last_log > 60:
                        print(f"  [{int(time.time()-start)}s] total_evts={n_total} "
                              f"eth_liq={n_eth} latest_usd=${rec['usd']:,.0f} "
                              f"side={rec['side']}", flush=True)
                        last_log = time.time()
        except Exception as e:
            print(f"  err: {e} (retry in {backoff}s)", flush=True)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
    out.close()
    print(f"DONE: eth_liquidations={n_eth} total_msgs={n_total}", flush=True)


asyncio.run(main())
