"""Measure end-to-end latencies relevant to bot performance.

Polls every 5 seconds and records:
  - euphoria_api_rtt_ms: HTTP HEAD to mainnet.euphoria.finance
  - megaeth_rpc_rtt_ms: eth_blockNumber call to MegaETH RPC
  - redstone_age_ms: age of latest RedStone publish (current_time - startedAt_us/1000)
  - proxy_state_rtt_ms: GET /state on local proxy
  - chart_frame_age_ms: how stale is the EU price tape
  - cb_age_ms: how stale is the latest Coinbase tick

Output: logs/latency_<TS>.jsonl
"""
import json
import ssl
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path
import certifi

ROOT = Path(__file__).resolve().parent.parent
SSL_CTX = ssl.create_default_context(cafile=certifi.where())
POLL_INTERVAL_SEC = 5
RUNTIME_SEC = int(sys.argv[1]) if len(sys.argv) > 1 else 36000

ts_label = time.strftime("%Y%m%d_%H%M%S")
out_path = ROOT / "logs" / f"latency_{ts_label}.jsonl"
print(f"output: {out_path}", flush=True)


def rtt_get(url, timeout=4.0, method="GET"):
    """Returns (rtt_ms, response_body_or_None)."""
    t0 = time.time()
    try:
        req = urllib.request.Request(url, method=method,
                                      headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
            body = r.read()
        return int((time.time() - t0) * 1000), body
    except Exception:
        return -1, None


def rpc_call(method, params=[]):
    """JSON-RPC call to MegaETH. Returns (rtt_ms, result_or_None)."""
    t0 = time.time()
    body = json.dumps({"jsonrpc": "2.0", "id": 1,
                        "method": method, "params": params}).encode()
    try:
        req = urllib.request.Request(
            "https://mainnet.megaeth.com/rpc", data=body,
            headers={"Content-Type": "application/json",
                     "User-Agent": "Mozilla/5.0"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=4.0, context=SSL_CTX) as r:
            d = json.loads(r.read().decode())
        return int((time.time() - t0) * 1000), d.get("result")
    except Exception:
        return -1, None


# RedStone Bolt adapter
ADAPTER = "0xc555c100DB24dF36D406243642C169CC5A937f09"
LATEST_ROUND_DATA = "0xfeaf968c"


start = time.time()
n = 0
with open(out_path, "w") as out:
    while time.time() - start < RUNTIME_SEC:
        t_now = time.time()
        rec = {"ts_ms": int(t_now * 1000)}

        # Euphoria API HEAD
        rec["eu_api_rtt_ms"], _ = rtt_get("https://mainnet.euphoria.finance/",
                                           method="HEAD")
        # MegaETH RPC
        rec["megaeth_rpc_rtt_ms"], _ = rpc_call("eth_blockNumber")
        # RedStone publish age via eth_call
        t0 = time.time()
        rpc_rtt, result = rpc_call("eth_call",
                                    [{"to": ADAPTER, "data": LATEST_ROUND_DATA},
                                     "latest"])
        rec["redstone_rpc_rtt_ms"] = rpc_rtt
        if result and result.startswith("0x") and len(result) >= 2 + 5 * 64:
            h = result[2:]
            started_at_us = int(h[128:192], 16)  # microseconds
            rec["redstone_age_ms"] = int(time.time() * 1000) - started_at_us // 1000
        else:
            rec["redstone_age_ms"] = -1

        # Local proxy
        proxy_rtt, proxy_body = rtt_get("http://127.0.0.1:8787/state", timeout=2.0)
        rec["proxy_state_rtt_ms"] = proxy_rtt
        if proxy_body:
            try:
                d = json.loads(proxy_body)
                if d.get("priceTs"):
                    rec["chart_frame_age_ms"] = int(time.time() * 1000) - d["priceTs"]
                else:
                    rec["chart_frame_age_ms"] = -1
            except Exception:
                rec["chart_frame_age_ms"] = -1

        out.write(json.dumps(rec) + "\n")
        out.flush()
        n += 1
        if n % 12 == 0:  # every 60s
            print(f"  [{int(time.time()-start)}s] writes={n}  "
                  f"last: eu={rec.get('eu_api_rtt_ms')}ms "
                  f"rpc={rec.get('megaeth_rpc_rtt_ms')}ms "
                  f"rs_age={rec.get('redstone_age_ms')}ms "
                  f"chart_age={rec.get('chart_frame_age_ms')}ms",
                  flush=True)

        # Sleep to interval
        elapsed = time.time() - t_now
        time.sleep(max(0, POLL_INTERVAL_SEC - elapsed))
print(f"DONE: writes={n}", flush=True)
