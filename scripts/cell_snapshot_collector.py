"""Capture every cell quote update + EU price for 10 hours.

Polls the proxy every 50ms. Uses delta encoding (only writes cells whose mult
changed since last snapshot) to keep disk usage manageable.

Output: logs/cell_snapshots_<TS>.jsonl
  Each line is one of:
    {"t": "p", "ts_ms": int, "price": float}                   # EU price snapshot
    {"t": "c", "ts_ms": int, "diffs": [[cx, cy, mult], ...]}   # cell deltas
    {"t": "f", "ts_ms": int, "full": {cell_key: mult, ...}}    # full snapshot every 30s for resync

After collection, run scripts/cell_snapshot_resolve.py to compute win/loss for
each (cx, cy) by replaying EU price tape against band-touches.
"""
import json
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROXY = "http://127.0.0.1:8787"
POLL_INTERVAL_MS = 50
FULL_SNAPSHOT_EVERY_SEC = 30
RUNTIME_SEC = int(sys.argv[1]) if len(sys.argv) > 1 else 36000  # 10h default

ts_label = time.strftime("%Y%m%d_%H%M%S")
out_path = ROOT / "logs" / f"cell_snapshots_{ts_label}.jsonl"
print(f"output: {out_path}", flush=True)
print(f"runtime: {RUNTIME_SEC}s ({RUNTIME_SEC/3600:.1f}h)", flush=True)
print(f"poll interval: {POLL_INTERVAL_MS}ms", flush=True)


def http_get(path, timeout=2.0):
    try:
        with urllib.request.urlopen(PROXY + path, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return None


prev_cells: dict[tuple[int, int], float] = {}
prev_price = None
start = time.time()
last_full_dump = 0
poll_count = 0
write_count = 0
error_count = 0

with open(out_path, "w") as out:
    while time.time() - start < RUNTIME_SEC:
        t_poll = time.time()
        poll_count += 1
        ts_ms = int(t_poll * 1000)

        # Price snapshot
        st = http_get("/state")
        if st and st.get("price") is not None:
            price = float(st["price"])
            if price != prev_price:
                out.write(json.dumps({"t": "p", "ts_ms": ts_ms, "price": price}) + "\n")
                prev_price = price
                write_count += 1

        # Cell snapshot
        mults = http_get("/multipliers")
        if mults and "cells" in mults:
            new_cells = {(c["cellX"], c["cellY"]): c["mult"] for c in mults["cells"]}
            # Periodic full snapshot for resync
            if t_poll - last_full_dump > FULL_SNAPSHOT_EVERY_SEC:
                full = {f"{k[0]}_{k[1]}": v for k, v in new_cells.items()}
                out.write(json.dumps({"t": "f", "ts_ms": ts_ms, "full": full}) + "\n")
                last_full_dump = t_poll
                prev_cells = new_cells
                write_count += 1
            else:
                diffs = []
                for k, v in new_cells.items():
                    if k not in prev_cells or prev_cells[k] != v:
                        diffs.append([k[0], k[1], v])
                # Also record cells that DISAPPEARED (window expired)
                for k in prev_cells:
                    if k not in new_cells:
                        diffs.append([k[0], k[1], None])
                if diffs:
                    out.write(json.dumps({"t": "c", "ts_ms": ts_ms, "diffs": diffs}) + "\n")
                    write_count += 1
                prev_cells = new_cells
        else:
            error_count += 1

        # Throttle
        elapsed_ms = (time.time() - t_poll) * 1000
        sleep_ms = max(0, POLL_INTERVAL_MS - elapsed_ms)
        time.sleep(sleep_ms / 1000.0)

        # Status every 60s
        if poll_count % 1200 == 0:  # ~60s at 50ms
            elapsed_s = time.time() - start
            size_mb = out_path.stat().st_size / (1024 * 1024)
            print(f"  [{int(elapsed_s)}s] polls={poll_count} writes={write_count} "
                  f"errors={error_count} file_size={size_mb:.1f}MB", flush=True)
            out.flush()

print(f"DONE: polls={poll_count} writes={write_count} errors={error_count}", flush=True)
print(f"output: {out_path} ({out_path.stat().st_size // 1024} KB)", flush=True)
