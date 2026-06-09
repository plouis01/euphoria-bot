"""Reconstruct the historical RedStone Bolt ETH/USD tape from MegaETH archive state.

The adapter (0xc555...f09) keeps NO round history (latestRoundData always
returns roundId=1, getRoundData reverts) and emits NO logs. But the public
RPC serves archive state, blocks are exactly 1.0s, so we sample
latestRoundData() via eth_call at every historical block in the window and
dedupe by startedAt (µs). RedStone publishes ~1.5Hz → 1Hz block sampling
captures ~2/3 of prints; fidelity is quantified downstream by re-resolving
DRY-log bets that carry live-recorded RS outcomes (see fullboard_backtest.py).

Output: one JSON line per unique print {"started_at_ms", "price", "block"}.
Resumable: on restart, skips blocks <= the last block already in the file.

Usage:
  python3 scripts/fetch_redstone_tape.py \
      --start-utc "2026-05-24 22:30:00" --end-utc "2026-05-27 21:00:00" \
      --out logs/redstone_tape_20260524_20260527.jsonl
"""
import argparse
import calendar
import json
import ssl
import sys
import time
import urllib.request
from pathlib import Path

import certifi

CTX = ssl.create_default_context(cafile=certifi.where())
RPC = "https://mainnet.megaeth.com/rpc"
ADAPTER = "0xc555c100DB24dF36D406243642C169CC5A937f09"
LRD = "0xfeaf968c"  # latestRoundData()


def rpc_post(payload, timeout=20):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        RPC, data=body,
        headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
        method="POST")
    with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
        return json.loads(r.read())


def head_block_and_ts():
    h = int(rpc_post({"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber",
                      "params": []})["result"], 16)
    b = rpc_post({"jsonrpc": "2.0", "id": 1, "method": "eth_getBlockByNumber",
                  "params": [hex(h), False]})["result"]
    return h, int(b["timestamp"], 16)


def block_ts(n):
    b = rpc_post({"jsonrpc": "2.0", "id": 1, "method": "eth_getBlockByNumber",
                  "params": [hex(n), False]})["result"]
    return int(b["timestamp"], 16)


def fetch_batch(blocks):
    """One JSON-RPC batch of eth_call latestRoundData at the given blocks.
    Returns {block: (price, started_at_us)} for successful entries."""
    payload = [{"jsonrpc": "2.0", "id": i,
                "method": "eth_call",
                "params": [{"to": ADAPTER, "data": LRD}, hex(blk)]}
               for i, blk in enumerate(blocks)]
    res = rpc_post(payload)
    out = {}
    if isinstance(res, dict):  # batch rejected → error dict
        raise RuntimeError(f"batch rejected: {res.get('error')}")
    for entry in res:
        if "result" not in entry or not entry["result"] or entry["result"] == "0x":
            continue
        h = entry["result"][2:]
        if len(h) < 5 * 64:
            continue
        answer = int(h[64:128], 16)
        if answer >= 1 << 255:
            answer -= 1 << 256
        started_at_us = int(h[128:192], 16)
        out[blocks[entry["id"]]] = (answer / 1e8, started_at_us)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start-utc", required=True, help='"YYYY-MM-DD HH:MM:SS" UTC')
    ap.add_argument("--end-utc", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--batch-size", type=int, default=100,
                    help="RPC batch limit is 100 (server enforced)")
    ap.add_argument("--sleep", type=float, default=8.0,
                    help="initial pause between batches; adapts on 429s (AIMD)")
    ap.add_argument("--min-sleep", type=float, default=2.0)
    ap.add_argument("--rpc", type=str, default=RPC)
    args = ap.parse_args()
    globals()["RPC"] = args.rpc

    t_start = calendar.timegm(time.strptime(args.start_utc, "%Y-%m-%d %H:%M:%S"))
    t_end = calendar.timegm(time.strptime(args.end_utc, "%Y-%m-%d %H:%M:%S"))

    head, t_head = head_block_and_ts()
    # blocks are 1.0s; verify the linear mapping at both window edges
    b_start = head - (t_head - t_start)
    b_end = head - (t_head - t_end)
    for name, blk, want in (("start", b_start, t_start), ("end", b_end, t_end)):
        got = block_ts(blk)
        if abs(got - want) > 2:
            # linear mapping drifted — correct by the observed offset
            blk += want - got
            got2 = block_ts(blk)
            print(f"[tape] {name} edge corrected by {want - got}s "
                  f"(now off by {got2 - want}s)", flush=True)
            if name == "start":
                b_start = blk
            else:
                b_end = blk
        else:
            print(f"[tape] {name} edge ok: block {blk:,} ts off by {got - want}s",
                  flush=True)

    out_path = Path(args.out)
    done_until = b_start - 1
    seen_started_at = set()
    if out_path.exists():
        for line in open(out_path):
            try:
                d = json.loads(line)
                done_until = max(done_until, d["block"])
                seen_started_at.add(d["started_at_ms"])
            except (json.JSONDecodeError, KeyError):
                continue
        print(f"[tape] resuming after block {done_until:,} "
              f"({len(seen_started_at):,} prints already on disk)", flush=True)

    total = b_end - b_start + 1
    t0 = time.time()
    written = 0
    fails = 0
    sleep_s = args.sleep  # AIMD: grow on 429, shrink slowly on success
    with open(out_path, "a") as f:
        blk = max(b_start, done_until + 1)
        while blk <= b_end:
            batch = list(range(blk, min(blk + args.batch_size, b_end + 1)))
            try:
                res = fetch_batch(batch)
                fails = 0
                sleep_s = max(args.min_sleep, sleep_s * 0.93)
            except Exception as e:
                fails += 1
                sleep_s = min(sleep_s * 1.6, 45.0)
                print(f"[tape] batch @{blk:,} failed ({e}); sleep→{sleep_s:.1f}s",
                      flush=True)
                time.sleep(sleep_s)
                if fails > 20:
                    print("[tape] too many consecutive failures — exiting "
                          "(rerun to resume)", flush=True)
                    sys.exit(1)
                continue
            # write in block order, dedupe by startedAt
            for b in batch:
                if b not in res:
                    continue
                price, st_us = res[b]
                st_ms = st_us // 1000
                if st_ms in seen_started_at:
                    continue
                seen_started_at.add(st_ms)
                f.write(json.dumps({"started_at_ms": st_ms, "price": price,
                                    "block": b}) + "\n")
                written += 1
            f.flush()
            blk = batch[-1] + 1
            done_frac = (blk - b_start) / total
            if (blk - b_start) % (args.batch_size * 20) < args.batch_size:
                elapsed = time.time() - t0
                rate = (blk - max(b_start, done_until + 1)) / max(elapsed, 1e-9)
                remaining = (b_end - blk) / max(rate, 1e-9)
                print(f"[tape] {done_frac:6.1%}  block {blk:,}/{b_end:,}  "
                      f"prints+{written:,}  {rate:.0f} blk/s  sleep={sleep_s:.1f}s  "
                      f"eta {remaining/60:.0f}m", flush=True)
            time.sleep(sleep_s)
    print(f"[tape] DONE — wrote {written:,} new prints to {out_path}", flush=True)


if __name__ == "__main__":
    main()
