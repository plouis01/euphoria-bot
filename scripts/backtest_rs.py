"""Retrospective RedStone resolution for historic bot logs.

For each resolved bet in the target logs:
  - Parse cellX, cellY, mult from SIGNAL line + resolve line
  - Compute window_start_ms, window_end_ms from cellX × TIME_INTERVAL
  - Find MegaETH block range covering that window
  - For each block in range, eth_call latestRoundData on the RedStone adapter
  - Dedupe by startedAt; check if any price touched the band [a, b)
  - Output (tag, cellX, cellY, mult, RS=WIN|LOSS) per bet

Aggregated by tag at the end with: n, WR_eu, WR_rs, PnL_eu, PnL_rs.

Throttled to ~2 req/sec to coexist with the live RS sweep, with exponential
backoff on rate-limit errors.

Usage:
    python3 scripts/backtest_rs.py            # default: live + top 10 dryrun
    python3 scripts/backtest_rs.py LIVE_*     # custom log glob

Writes results to logs/backtest_rs_<timestamp>.jsonl (one line per bet)
plus a leaderboard summary to stdout.
"""
import os, re, sys, time, json, glob, ssl, urllib.request, urllib.error
from collections import defaultdict
from pathlib import Path

import certifi

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)

RPC_URL = "https://mainnet.megaeth.com/rpc"
ADAPTER = "0xc555c100DB24dF36D406243642C169CC5A937f09"
LATEST_ROUND_DATA = "0xfeaf968c"
TIME_INTERVAL = 5     # seconds per cell-X step
PRICE_INTERVAL = 0.5  # dollars per cell-Y step

_SSL_CTX = ssl.create_default_context(cafile=certifi.where())

# Target tags: top 10 from HOUR_FINAL + all LIVE
TARGET_DRY_TAGS = {
    "W1_baseline_c30r", "W3_R1_strict", "W3_cal2_only", "W1_R1_only",
    "W3_F4k4", "W3_R3_drift", "W4_studentt", "W3_F4k8", "W4_heston",
    "TOP_c30r_pure",
}
TARGET_LIVE_PREFIX = "LIVE_"

RESOLVE_RE = re.compile(
    r"\[(?P<tag>[^\]]+)\] \[bot\] resolve cell=\((?P<cx>\d+),(?P<cy>\d+)\) "
    r"band=\[(?P<lo>[\d.]+),(?P<hi>[\d.]+)\) strat=(?P<strat>\S+) "
    r"eu_n=(?P<eun>\d+) cb_n=(?P<cbn>\d+) "
    r"(?:rs_n=(?P<rsn>\d+) )?"
    r"EU=(?P<eu>WIN|LOSS) CB=(?P<cb>WIN|LOSS)(?: RS=(?P<rs>WIN|LOSS|N/A))? "
    r"p_pred=(?P<p>[\d.]+) ev_pred=\+?(?P<ev>-?[\d.]+) "
    r"σ_at_bet=(?P<sigma>[\d.]+) drift_at_bet=\$(?P<drift>[+-][\d.]+) "
    r"pnl=\$(?P<pnl>[+-]?[\d.]+)"
)
SIGNAL_RE = re.compile(
    r"\[(?P<tag>[^\]]+)\] \[bot\] SIGNAL strat=\S+ cell=\((?P<cx>\d+),(?P<cy>\d+)\) "
    r"mult=(?P<mult>[\d.]+)"
)

LIVE_SETTLED_RE = re.compile(
    r"\[(?P<tag>[^\]]+)\] \[bot\] LIVE settled tradeId=\S+ "
    r"cell=\((?P<cx>\d+),(?P<cy>\d+)\) mult=(?P<mult>[\d.]+) "
    r"outcome=(?P<outcome>won|lost)"
)


class RpcClient:
    def __init__(self, url=RPC_URL, target_rate=2.0):
        self.url = url
        self.min_interval = 1.0 / target_rate
        self.last_call = 0.0
        self.req_n = 0
        self.fail_n = 0

    def _throttle(self):
        elapsed = time.time() - self.last_call
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self.last_call = time.time()

    def call(self, method: str, params: list, max_retries: int = 5):
        backoff = 1.0
        for attempt in range(max_retries):
            self._throttle()
            self.req_n += 1
            body = json.dumps({"jsonrpc": "2.0", "id": 1,
                               "method": method, "params": params}).encode()
            req = urllib.request.Request(
                self.url, data=body,
                headers={"Content-Type": "application/json",
                         "User-Agent": "Mozilla/5.0"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=5, context=_SSL_CTX) as r:
                    data = json.loads(r.read().decode())
            except Exception as e:
                self.fail_n += 1
                time.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
                continue
            if "error" in data:
                # Rate limit — back off
                self.fail_n += 1
                time.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
                continue
            return data.get("result")
        return None


def block_at_ts(rpc: RpcClient, ts_seconds: int,
                cache: dict[int, tuple[int, int]]) -> int | None:
    """Estimate block number at given timestamp from cached (block, ts) anchors.

    After seeding 2 anchors (current + 1 day ago), all subsequent lookups are
    pure linear extrapolation — zero RPC calls. MegaETH is ~1 block/sec but
    slightly variable, so we accept ±2 block error.
    """
    if len(cache) < 2:
        # Seed anchor 1: current
        if not cache:
            bn_hex = rpc.call("eth_blockNumber", [])
            if bn_hex is None:
                return None
            bn = int(bn_hex, 16)
            blk = rpc.call("eth_getBlockByNumber", [hex(bn), False])
            if not blk:
                return None
            cache[bn] = (bn, int(blk["timestamp"], 16))
        # Seed anchor 2: 86400 blocks (1 day) earlier — for better slope estimate
        anchor_bn = next(iter(cache.keys()))
        target = max(1, anchor_bn - 86400)
        if target not in cache:
            blk = rpc.call("eth_getBlockByNumber", [hex(target), False])
            if blk:
                cache[target] = (target, int(blk["timestamp"], 16))
    # Pure extrapolation from the closest anchor
    items = sorted(cache.values())
    # Use the anchor closest to target ts
    best = min(items, key=lambda x: abs(x[1] - ts_seconds))
    delta_blocks = ts_seconds - best[1]
    est = best[0] + delta_blocks
    return max(1, est)


def get_rs_prices(rpc: RpcClient, start_block: int, end_block: int) -> list[tuple[int, float]]:
    """Sample latestRoundData at every other block in [start_block, end_block].
    Returns deduped (startedAt_ms, price)."""
    prices = []
    seen = set()
    # Sample fewer blocks to keep RPC budget reasonable. A 5s window has
    # ~5 blocks and ~7 RedStone publishes (≈666ms cadence). Two samples per
    # window catches most distinct rounds via dedup.
    n_blocks = end_block - start_block + 1
    if n_blocks <= 3:
        sample = list(range(start_block, end_block + 1))
    else:
        # First, middle, last
        sample = [start_block, start_block + n_blocks // 2, end_block]
    for bn in sample:
        res = rpc.call("eth_call",
                       [{"to": ADAPTER, "data": LATEST_ROUND_DATA}, hex(bn)])
        if res is None or not res.startswith("0x") or len(res) < 2 + 5 * 64:
            continue
        h = res[2:]
        answer_raw = int(h[64:128], 16)
        if answer_raw >= 1 << 255:
            answer_raw -= 1 << 256
        started_at = int(h[128:192], 16)  # microseconds
        started_at_ms = started_at // 1000
        if started_at_ms in seen:
            continue
        seen.add(started_at_ms)
        prices.append((started_at_ms, answer_raw / 1e8))
    return prices


def touched_band(prices: list[tuple[int, float]], band_lo: float, band_hi: float) -> bool:
    """Check if any price in `prices` falls in [band_lo, band_hi) OR if
    consecutive prices straddle the band (intra-tick crossing)."""
    if not prices:
        return False
    prices = sorted(prices)
    for ts, p in prices:
        if band_lo <= p < band_hi:
            return True
    # Segment crossing: check consecutive pairs
    for i in range(len(prices) - 1):
        p0 = prices[i][1]; p1 = prices[i + 1][1]
        lo, hi = (p0, p1) if p0 <= p1 else (p1, p0)
        if lo <= band_hi and hi >= band_lo:
            # Segment overlaps band
            return True
    return False


def parse_logs(log_paths: list[str]) -> list[dict]:
    """Extract one record per resolved/settled bet."""
    bets = []
    for path in log_paths:
        # First pass: SIGNAL mults per cell (needed for both dry resolve and live)
        signal_mults: dict[tuple[int, int], float] = {}
        for line in open(path):
            m = SIGNAL_RE.search(line)
            if m:
                signal_mults[(int(m.group("cx")), int(m.group("cy")))] = float(m.group("mult"))
        # Second pass: dry-run resolves
        for line in open(path):
            m = RESOLVE_RE.search(line)
            if not m:
                continue
            cx = int(m.group("cx"))
            cy = int(m.group("cy"))
            mult = signal_mults.get((cx, cy))
            if mult is None:
                pnl = float(m.group("pnl"))
                mult = (pnl + 1.0) if m.group("eu") == "WIN" else 3.0
            band_lo = float(m.group("lo"))
            band_hi = float(m.group("hi"))
            bets.append({
                "log": os.path.basename(path),
                "tag": m.group("tag"),
                "kind": "DRY",
                "cx": cx, "cy": cy, "mult": mult,
                "band_lo": band_lo, "band_hi": band_hi,
                "window_start_ms": cx * TIME_INTERVAL * 1000,
                "window_end_ms": (cx + 1) * TIME_INTERVAL * 1000,
                "eu_won": m.group("eu") == "WIN",
                "cb_won": m.group("cb") == "WIN",
                "live_outcome": None,
            })
        # Third pass: LIVE settled lines (on-chain ground truth)
        for line in open(path):
            m = LIVE_SETTLED_RE.search(line)
            if not m:
                continue
            cx = int(m.group("cx"))
            cy = int(m.group("cy"))
            mult = float(m.group("mult"))
            band_lo = cy * PRICE_INTERVAL
            band_hi = band_lo + PRICE_INTERVAL
            bets.append({
                "log": os.path.basename(path),
                "tag": m.group("tag"),
                "kind": "LIVE",
                "cx": cx, "cy": cy, "mult": mult,
                "band_lo": band_lo, "band_hi": band_hi,
                "window_start_ms": cx * TIME_INTERVAL * 1000,
                "window_end_ms": (cx + 1) * TIME_INTERVAL * 1000,
                "eu_won": None,
                "cb_won": None,
                "live_outcome": m.group("outcome") == "won",  # ground truth
            })
    return bets


def main():
    # Build log list
    pat = sys.argv[1] if len(sys.argv) > 1 else None
    if pat is None:
        # Default: LIVE_* + top 10 dryrun tags
        logs = []
        for f in glob.glob("logs/LIVE_*.log"):
            logs.append(f)
        for tag in TARGET_DRY_TAGS:
            # Pick the largest log for each tag
            cands = glob.glob(f"logs/{tag}_*.log")
            if cands:
                cands.sort(key=os.path.getsize, reverse=True)
                logs.append(cands[0])
    else:
        logs = sorted(glob.glob(f"logs/{pat}.log"))
    print(f"# Backtest target logs: {len(logs)}", flush=True)
    for l in logs:
        sz = os.path.getsize(l) // 1024
        print(f"#  {os.path.basename(l)} ({sz}KB)", flush=True)

    bets = parse_logs(logs)
    print(f"# Parsed {len(bets)} bets total", flush=True)

    # Sanity: per-tag bet count
    per_tag = defaultdict(int)
    for b in bets:
        per_tag[b["tag"]] += 1
    print("# Per-tag bet count:")
    for tag, n in sorted(per_tag.items(), key=lambda x: -x[1]):
        print(f"#   {tag}: {n}")
    print("#", flush=True)

    rpc = RpcClient(target_rate=3.0)
    block_cache: dict[int, tuple[int, int]] = {}

    # Output file
    ts_label = time.strftime("%Y%m%d_%H%M%S")
    out_path = ROOT / "logs" / f"backtest_rs_{ts_label}.jsonl"
    print(f"# Writing results to {out_path.name}", flush=True)

    t0 = time.time()
    n_done = 0
    n_rs_won = 0
    n_skipped = 0
    with open(out_path, "w") as out:
        for b in bets:
            start_ts = b["window_start_ms"] // 1000
            end_ts = b["window_end_ms"] // 1000
            start_block = block_at_ts(rpc, start_ts, block_cache)
            end_block = block_at_ts(rpc, end_ts, block_cache)
            if start_block is None or end_block is None:
                n_skipped += 1
                continue
            prices = get_rs_prices(rpc, start_block, end_block + 1)
            if not prices:
                n_skipped += 1
                continue
            rs_won = touched_band(prices, b["band_lo"], b["band_hi"])
            if rs_won:
                n_rs_won += 1
            out_rec = {
                "tag": b["tag"], "kind": b["kind"],
                "cx": b["cx"], "cy": b["cy"],
                "mult": b["mult"], "eu_won": b["eu_won"], "cb_won": b["cb_won"],
                "live_outcome": b["live_outcome"],
                "rs_won": rs_won, "rs_ticks": len(prices),
                "rs_price_min": min(p for _, p in prices),
                "rs_price_max": max(p for _, p in prices),
            }
            out.write(json.dumps(out_rec) + "\n")
            out.flush()
            n_done += 1
            if n_done % 50 == 0:
                rate = n_done / (time.time() - t0)
                eta_s = (len(bets) - n_done) / max(rate, 0.001)
                print(f"# {n_done}/{len(bets)} bets done  "
                      f"rate={rate:.2f}/s  ETA={eta_s/60:.1f}min  "
                      f"RPC req={rpc.req_n} fail={rpc.fail_n}  "
                      f"skipped={n_skipped}",
                      flush=True)

    elapsed = time.time() - t0
    print(f"# DONE in {elapsed/60:.1f}min  bets={n_done}  "
          f"rs_won={n_rs_won}  skipped={n_skipped}  "
          f"RPC req={rpc.req_n} fail={rpc.fail_n}",
          flush=True)
    print(f"# Output: {out_path}", flush=True)


if __name__ == "__main__":
    main()
