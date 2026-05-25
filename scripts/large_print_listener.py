"""Listen for large trades across Binance + Coinbase ETH/USD spot.

Extends the multi_exchange_oracle pattern (just prices) to also capture
trade SIZE. Filters to "large prints" (default >= $50k USD notional) and
writes them to disk for cross-reference with bot signals.

Hypothesis: large prints in either direction precede short-horizon vol
spikes. If we detect them within 100s of milliseconds, we have an ex-ante
warning signal that touch_prob model is about to break.

Output: logs/large_prints_<TS>.jsonl
Each line: {"ts_ms", "venue", "side", "price", "qty", "usd"}
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
MIN_USD = float(sys.argv[2]) if len(sys.argv) > 2 else 50_000

ts_label = time.strftime("%Y%m%d_%H%M%S")
out_path = ROOT / "logs" / f"large_prints_{ts_label}.jsonl"
print(f"output: {out_path}  min_usd=${MIN_USD:,.0f}", flush=True)


def writer(out):
    counter = {"n": 0, "by_venue": {}}
    last_log = [time.time()]
    def write(rec):
        out.write(json.dumps(rec) + "\n"); out.flush()
        counter["n"] += 1
        counter["by_venue"][rec["venue"]] = counter["by_venue"].get(rec["venue"], 0) + 1
        if time.time() - last_log[0] > 60:
            print(f"  [{int(time.time()-time.time()+counter['n'])}] "
                  f"total={counter['n']} by_venue={counter['by_venue']} "
                  f"latest=${rec['usd']:,.0f} on {rec['venue']} {rec['side']}",
                  flush=True)
            last_log[0] = time.time()
    return write


async def binance_trades(write):
    url = "wss://stream.binance.com/ws/ethusdt@trade"
    bk = 1.0
    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ssl=SSL_CTX) as ws:
                bk = 1.0
                async for msg in ws:
                    d = json.loads(msg)
                    price = float(d.get("p", 0)); qty = float(d.get("q", 0))
                    usd = price * qty
                    if usd < MIN_USD: continue
                    side = "SELL" if d.get("m") else "BUY"  # m=true => buyer is maker => sell aggressor
                    write({
                        "ts_ms": int(d.get("T", time.time() * 1000)),
                        "venue": "binance", "side": side,
                        "price": price, "qty": qty, "usd": usd,
                    })
        except Exception as e:
            print(f"  binance err: {e}", flush=True)
            await asyncio.sleep(bk); bk = min(bk * 2, 60)


async def coinbase_matches(write):
    url = "wss://ws-feed.exchange.coinbase.com"
    sub = json.dumps({"type": "subscribe", "product_ids": ["ETH-USD"],
                       "channels": ["matches"]})
    bk = 1.0
    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ssl=SSL_CTX) as ws:
                await ws.send(sub); bk = 1.0
                async for msg in ws:
                    d = json.loads(msg)
                    if d.get("type") not in ("match", "last_match"): continue
                    price = float(d.get("price", 0)); qty = float(d.get("size", 0))
                    usd = price * qty
                    if usd < MIN_USD: continue
                    side = d.get("side", "?").upper()  # taker side
                    ts_iso = d.get("time", "")
                    try:
                        # ISO8601 → ms epoch
                        from datetime import datetime
                        ts_ms = int(datetime.fromisoformat(ts_iso.replace("Z", "+00:00")).timestamp() * 1000)
                    except:
                        ts_ms = int(time.time() * 1000)
                    write({
                        "ts_ms": ts_ms,
                        "venue": "coinbase", "side": side,
                        "price": price, "qty": qty, "usd": usd,
                    })
        except Exception as e:
            print(f"  coinbase err: {e}", flush=True)
            await asyncio.sleep(bk); bk = min(bk * 2, 60)


async def main():
    out = open(out_path, "w")
    write = writer(out)
    start = time.time()
    tasks = [asyncio.create_task(binance_trades(write)),
             asyncio.create_task(coinbase_matches(write))]
    try:
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=RUNTIME_SEC)
    except asyncio.TimeoutError:
        pass
    finally:
        for t in tasks: t.cancel()
        out.close()
    print(f"DONE: wrote {out_path}", flush=True)


asyncio.run(main())
