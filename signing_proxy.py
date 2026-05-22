"""
Signing proxy for the Euphoria trading bot.

Attaches to a running Chrome instance over CDP, finds the
euphoria.finance tab, observes its tRPC websocket frames, and
exposes a tiny HTTP API so an external bot loop can:

  - read live state (price, quote-ref-time, balance, nonce, grid)
  - place taps (drives the DOM in the live, logged-in session)

The Privy embedded wallet signs the EIP-712 in-page; the ECDSA P-256
trade key in IndexedDB (non-extractable) signs `botSignature`. We never
touch keys directly — we just drive the page that already has them.

Setup:
  1. Quit Chrome completely (Cmd+Q).
  2. Start Chrome with the debug port:
       open -na "Google Chrome" --args --remote-debugging-port=9222
  3. Open https://euphoria.finance/trade and confirm you're logged in
     and showing the correct USDm balance on 0x928D...2c22.
  4. python -m pip install playwright aiohttp
     python -m playwright install   # (no-op if Chrome is target; harmless)
  5. python signing_proxy.py

API:
  GET  /state        -> {price, priceTs, balance, nonce, lastTrade}
  GET  /chart        -> live chart snapshot (balance, bet size, slippage,
                        ready flag, grid window, mult-quote stats)
  GET  /multipliers  -> dump every (cellX, cellY) -> multiplier currently quoted
  POST /tap          -> {cellX, cellY, amount, slippage[, timeoutS]}
                        Returns once an `executed` frame matches the tap.

Architecture deferred (see memory):
  - 1b: dedicated copied profile (no Chrome lock)
  - 2a: page.evaluate() into the page's executeTrade directly (skip DOM)
  - 2c: import the page's tRPC client (still needs BE/FE → no win)
  - Option 1 (pure-Python signer): dead, extractable=false on the key
"""

import asyncio
import json
import os
import time
from collections import deque
from typing import Any, Optional

from aiohttp import web
from playwright.async_api import async_playwright, Page, WebSocket

from binance_feed import BinanceFeed
from coinbase_feed import CoinbaseFeed


CDP_URL = "http://127.0.0.1:9222"
EUPHORIA_HOST = "euphoria.finance"
PROXY_PORT = 8787

# Window of recent WS frames kept for diagnostics. Trades themselves
# resolve via the pending_taps map, not this buffer. Sized for a full tap
# lifecycle in a busy session — 200 was too small to span sent→executed→settled
# when manual UI taps interleave with bot taps.
FRAME_BUFFER_SIZE = 1000

# Dense price tape: every price tick observed on the WS, keyed by source-side ts.
# Sized for ~10 min at 5 Hz; bot reads via /price_tape.
PRICE_TAPE_SIZE = 6000

# Append-only settlement log: every position-settled frame seen on the WS
# is written here as JSON-lines for permanent audit.
SETTLEMENT_LOG_PATH = os.environ.get(
    "SETTLEMENT_LOG_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "settlements.jsonl"),
)


class ProxyState:
    def __init__(self) -> None:
        self.price: Optional[float] = None
        self.price_ts: Optional[int] = None
        self.price_tape: deque = deque(maxlen=PRICE_TAPE_SIZE)  # (ts_ms, price)
        self.balance: Optional[str] = None
        self.nonce: Optional[int] = None
        self.last_trade: Optional[dict] = None
        # Live in-memory mirror of __chart.quotesFeed.quoteData, refreshed via
        # the page-side subscribe() listener (see install_quote_listener_js).
        # When fresh, http_multipliers serves from this instead of doing a
        # Playwright JS eval — sub-millisecond reads.
        self.live_multipliers: Optional[dict] = None
        self.live_multipliers_ts: float = 0.0
        # In-flight tap initiated by the proxy. None when idle.
        # Shape: {"started_at": float, "future": Future, "sub_id": int|None,
        #         "trade_id": str|None, "submitted_payload": dict|None,
        #         "lifecycle": [{"status": str, "ts": float}], ...}
        self.current_tap: Optional[dict] = None
        # Last-seen tradeId per status for diagnostics (manual taps too).
        self.last_executed: Optional[dict] = None
        self.frames: deque = deque(maxlen=FRAME_BUFFER_SIZE)
        # External feeds, attached at startup so settle-time tick snapshots can
        # be enriched from inside the WS-frame handler.
        self.binance_feed = None
        self.coinbase_feed = None

    def push_frame(self, direction: str, payload: Any) -> None:
        self.frames.append({"dir": direction, "ts": time.time(), "msg": payload})


STATE = ProxyState()


def _append_settlement_log(entry: dict) -> None:
    """Append a settled-position record to SETTLEMENT_LOG_PATH as JSON-lines.
    Best-effort; never raises (logs failure to stderr at worst)."""
    try:
        os.makedirs(os.path.dirname(SETTLEMENT_LOG_PATH), exist_ok=True)
        with open(SETTLEMENT_LOG_PATH, "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception as e:
        print(f"[proxy] settlement log write failed: {e}", flush=True)


def _parse_frame(raw: str) -> Optional[Any]:
    try:
        return json.loads(raw)
    except Exception:
        return None


def _record_lifecycle(status: str) -> None:
    tap = STATE.current_tap
    if tap is not None:
        tap.setdefault("lifecycle", []).append({"status": status, "ts": time.time()})


def _handle_received(msg: Any) -> None:
    """tRPC downstream frame. Extract price ticks and order/position events."""
    if not isinstance(msg, dict):
        return
    result = msg.get("result")
    if not isinstance(result, dict):
        return
    if result.get("type") != "data":
        return
    msg_sub_id = msg.get("id")
    data = ((result.get("data") or {}).get("json")) or {}

    # Price tick: {price, timestamp}
    if "price" in data and "timestamp" in data and len(data) <= 2:
        STATE.price = data["price"]
        STATE.price_ts = data["timestamp"]
        STATE.price_tape.append((data["timestamp"], data["price"]))
        return

    t = data.get("type")
    status = data.get("status")
    trade_id = data.get("tradeId")
    tap = STATE.current_tap

    if t == "order":
        STATE.last_trade = data

        # Bind tradeId to current_tap on the first order frame matching our sub_id.
        if (tap is not None and tap.get("sub_id") is not None
                and msg_sub_id == tap["sub_id"]
                and tap.get("trade_id") is None and trade_id):
            tap["trade_id"] = trade_id

        if status == "requested" and tap and trade_id == tap.get("trade_id"):
            _record_lifecycle("requested")
        elif status == "matched" and tap and trade_id == tap.get("trade_id"):
            _record_lifecycle("matched")
        elif status == "executed":
            executed = data.get("executed") or {}
            new_balance = executed.get("updatedBalance")
            if new_balance is not None:
                STATE.balance = new_balance
            STATE.last_executed = data
            if tap and trade_id == tap.get("trade_id"):
                _record_lifecycle("executed")
                fut: asyncio.Future = tap["future"]
                if not fut.done():
                    fut.set_result(data)
        elif status == "rejected" and tap and trade_id == tap.get("trade_id"):
            _record_lifecycle("rejected")
            fut = tap["future"]
            if not fut.done():
                fut.set_exception(RuntimeError(f"order rejected: {data}"))

    elif t == "position":
        if status == "opened" and tap and trade_id == tap.get("trade_id"):
            _record_lifecycle("opened")
        elif status == "settled":
            settled = data.get("settled") or {}
            new_balance = settled.get("updatedBalance")
            if new_balance is not None:
                STATE.balance = new_balance
            # Persist every settled position to disk for audit + later analysis.
            matched = bool(tap and trade_id == tap.get("trade_id"))
            cell_x = cell_y = None
            if matched:
                tap_args = (tap or {}).get("tap_args") or {}
                cell_x = tap_args.get("cellX")
                cell_y = tap_args.get("cellY")
            # Attach a dense tick snapshot so scripts/rescore_logs.py can
            # re-decide touch with the segment-crossing resolver. 70s covers a
            # 5s settlement window plus the typical 60s pre-bet lookback.
            now_ms = int(time.time() * 1000)
            window_ms = 70_000
            eu_ticks = [(ts, p) for (ts, p) in STATE.price_tape
                        if ts >= now_ms - window_ms]
            cb_ticks = []
            if STATE.coinbase_feed is not None:
                cb_ticks = [(t.ts_ms, t.price)
                             for t in STATE.coinbase_feed.window(window_ms)]
            bn_ticks = []
            if STATE.binance_feed is not None:
                bn_ticks = [(t.ts_ms, t.price)
                             for t in STATE.binance_feed.window(window_ms)]
            _append_settlement_log({
                "ts": time.time(),
                "tradeId": trade_id,
                "matched_current_tap": matched,
                "cellX": cell_x,
                "cellY": cell_y,
                "settled": settled,
                "data": data,
                "priceTicks": eu_ticks,
                "coinbaseTicks": cb_ticks,
                "binanceTicks": bn_ticks,
            })
            # If this settles the current tap, complete its settled_future.
            if matched:
                _record_lifecycle("settled")
                sfut = tap.get("settled_future")
                if sfut is not None and not sfut.done():
                    sfut.set_result(data)


def _handle_sent(msg: Any) -> None:
    """tRPC upstream frame. Bind sub_id of the next executeTrade.subscribe
    frame to current_tap so we can chain to tradeId via downstream order events."""
    if not isinstance(msg, dict):
        return
    if msg.get("method") != "subscription":
        return
    params = msg.get("params") or {}
    if params.get("path") != "trades.executeTrade":
        return
    sub_id = msg.get("id")
    payload = (params.get("input") or {}).get("json") or {}
    nonce = payload.get("nonce")
    if isinstance(nonce, int):
        STATE.nonce = nonce
    tap = STATE.current_tap
    if tap is not None and tap.get("sub_id") is None:
        tap["sub_id"] = sub_id
        tap["submitted_payload"] = payload
        _record_lifecycle("sent")


def _hook_websocket(ws: WebSocket) -> None:
    def on_recv(payload: str) -> None:
        msg = _parse_frame(payload)
        if msg is None:
            return
        STATE.push_frame("recv", msg)
        _handle_received(msg)

    def on_send(payload: str) -> None:
        msg = _parse_frame(payload)
        if msg is None:
            return
        STATE.push_frame("send", msg)
        _handle_sent(msg)

    ws.on("framereceived", on_recv)
    ws.on("framesent", on_send)


QUOTES_LISTENER_JS = r"""
(() => {
  const g = window.__chart;
  if (!g || !g.quotesFeed) return { ok: false, reason: 'no_quotesFeed' };
  if (window.__quoteListenerInstalled) return { ok: true, already: true };
  window.__quoteListenerInstalled = true;

  // Serialize the live quoteData and push to the proxy via expose_binding.
  // Called on every quotesFeed.notifyListeners() → after every MM grid update.
  const flush = () => {
    try {
      const qd = g.quotesFeed.quoteData || {};
      const { data, timeSteps, priceSteps, startingIndex } = qd;
      if (!data || !startingIndex || !timeSteps || !priceSteps) return;
      const cells = [];
      for (let dx = 0; dx < timeSteps; dx++) {
        for (let dy = 0; dy < priceSteps; dy++) {
          const v = data[dx * priceSteps + dy];
          if (v == null) continue;
          cells.push({
            cellX: startingIndex.timeIndex + dx,
            cellY: startingIndex.priceIndex + dy,
            mult: v / 100,
          });
        }
      }
      const payload = {
        lastGridTimestamp: g.quotesFeed.lastGridTimestamp,
        isStale: g.quotesFeed.isStale,
        startingIndex, timeSteps, priceSteps, cells,
      };
      window.__pushQuotes(payload);
    } catch (e) { /* swallow */ }
  };

  // Initial push so we have data immediately on attach.
  flush();
  const unsub = g.quotesFeed.subscribe(flush);
  window.__quoteListenerUnsub = unsub;
  return { ok: true, installed: true };
})()
"""


def _push_quotes_handler(_source, payload):
    """Receive a quote-grid update from the page. Stores in STATE for fast
    in-memory reads from /multipliers without a Playwright eval."""
    if not isinstance(payload, dict):
        return
    STATE.live_multipliers = payload
    STATE.live_multipliers_ts = time.time()


async def install_quote_listener(page: Page) -> dict:
    """Install the in-page subscribe() listener (after chart hook is ready)."""
    return await page.evaluate(QUOTES_LISTENER_JS)


async def _attach():
    pw = await async_playwright().start()
    browser = await pw.chromium.connect_over_cdp(CDP_URL)
    target_page: Optional[Page] = None
    for ctx in browser.contexts:
        for page in ctx.pages:
            if EUPHORIA_HOST in (page.url or ""):
                target_page = page
                break
        if target_page:
            break
    if target_page is None:
        raise RuntimeError(
            f"No tab found matching {EUPHORIA_HOST}. Open it in Chrome first."
        )
    target_page.on("websocket", _hook_websocket)
    # expose_binding allows in-page JS to call back into Python directly.
    try:
        await target_page.expose_binding("__pushQuotes", _push_quotes_handler)
    except Exception as e:
        # Idempotent on re-attach: page may already have the binding from a prior session.
        print(f"[proxy] expose_binding warning: {e}")
    return pw, browser, target_page


# ---------------------------------------------------------------------------
# Tap driver — calls __chart.game.placeBet directly (V1, jumped to 2a).
# ---------------------------------------------------------------------------
#
# We rely on the page exposing the chart instance at window.__chart. The bot
# UI lazily attaches it (a separate one-shot JS does that during startup, see
# install_chart_hook below). Once attached, placeBet enters an XState machine
# which signs (Privy embedded wallet), invokes BE() for botSignature, attaches
# the device fingerprint, and opens the executeTrade tRPC subscription.
#
# Signature observed in the bundle:
#   placeBet(cellX, cellY, multiplier, gridState, asset='ETH', quotedGridRefTime)
# State setters used:
#   setBetSize(usdAmount)
#   setSlippageTolerance(percent)  // 60 = 60%
INSTALL_CHART_HOOK_JS = r"""
() => {
  // Walks the React fiber under the first <canvas> to find the chart
  // instance and exposes it at window.__chart. Idempotent.
  if (window.__chart && window.__chart.game) return { ok: true, alreadyHooked: true };
  const canvas = document.querySelector('canvas');
  if (!canvas) return { ok: false, reason: 'no_canvas' };
  const fiberKey = Object.keys(canvas).find(k => k.startsWith('__reactFiber'));
  if (!fiberKey) return { ok: false, reason: 'no_react_fiber' };
  let f = canvas[fiberKey];
  while (f && !(f.memoizedProps && f.memoizedProps.chart)) {
    f = f.return;
    if (!f) break;
  }
  if (!f) return { ok: false, reason: 'no_chart_prop_in_tree' };
  window.__chart = f.memoizedProps.chart;
  return { ok: true, alreadyHooked: false };
}
"""

TAP_JS = r"""
async (args) => {
  const g = window.__chart;
  if (!g || !g.game) return { ok: false, reason: 'chart_not_hooked' };

  // Configure bet (idempotent state-machine sends; cheap).
  if (typeof args.amount === 'number') g.game.setBetSize(args.amount);
  if (typeof args.slippage === 'number') g.game.setSlippageTolerance(args.slippage);

  // Read current quote and ref time from local state.
  const mult = g.quotesFeed.getMultiplierForCell(args.cellX, args.cellY);
  if (mult == null) {
    return { ok: false, reason: 'no_quote_for_cell',
             cellX: args.cellX, cellY: args.cellY };
  }
  const refTime = g.quotesFeed.lastGridTimestamp;

  // Verify bot protection is ready by attempting the call. placeBet itself
  // returns a sentinel on insufficient balance / security key not ready.
  let res;
  try {
    res = g.game.placeBet(args.cellX, args.cellY, mult, g.gridState,
                          args.asset || 'ETH', refTime);
  } catch (e) {
    return { ok: false, reason: 'placeBet_threw', err: String(e) };
  }

  // placeBet is fire-and-forget (returns undefined on success, a sentinel
  // string/symbol otherwise). Treat falsy/undefined as success.
  const failed = (res !== undefined && res !== null);
  let sentinel = null;
  if (failed) {
    try {
      if (typeof res === 'string') sentinel = res;
      else if (typeof res === 'object') {
        sentinel = {};
        for (const k of Object.keys(res)) sentinel[k] = res[k];
        sentinel.__str = String(res);
        sentinel.__ctor = res.constructor && res.constructor.name;
      } else sentinel = String(res);
    } catch (e) { sentinel = 'serialize_failed: ' + String(e); }
  }
  // Snapshot machine state for diagnostics on failure.
  let machineState = null;
  try {
    const snap = g.game.machine && g.game.machine.getSnapshot && g.game.machine.getSnapshot();
    if (snap) machineState = { value: snap.value,
                               balance: snap.context && snap.context.balance,
                               betSize: snap.context && snap.context.betSize,
                               nonce: snap.context && snap.context.nonce };
  } catch (e) {}
  return {
    ok: !failed,
    placeBetReturn: sentinel,
    multiplier: mult,
    quotedGridRefTime: refTime,
    machineState,
  };
}
"""


async def install_chart_hook(page: Page):
    return await page.evaluate(INSTALL_CHART_HOOK_JS)


async def tap_cell(page: Page, cell_x: int, cell_y: int, amount: float, slippage: int):
    return await page.evaluate(TAP_JS, {"cellX": cell_x, "cellY": cell_y,
                                        "amount": amount, "slippage": slippage})


CHART_STATE_JS = r"""
() => {
  const g = window.__chart;
  if (!g || !g.game) return { ok: false, reason: 'chart_not_hooked' };
  const ctx = g.game.machine.getSnapshot().context;
  const qd = g.quotesFeed.quoteData || {};
  return {
    ok: true,
    balance: g.game.getBalance(),
    pendingBalance: g.game.getPendingBalance(),
    betSize: g.game.getBetSize(),
    slippageTolerance: g.game.getSlippageTolerance(),
    asset: g.game.getAsset(),
    activeTimeframe: g.game.getActiveTimeframe(),
    activeGameType: g.game.getActiveGameType(),
    isReady: g.game.getIsReady(),
    machineState: ctx,
    grid: {
      lastGridTimestamp: g.quotesFeed.lastGridTimestamp,
      isStale: g.quotesFeed.isStale,
      startingIndex: qd.startingIndex || null,
      timeSteps: qd.timeSteps || null,
      priceSteps: qd.priceSteps || null,
    },
    chartConfig: {
      squareDuration: g.chartConfig.squareDuration,
      dollarsPerLine: g.chartConfig.dollarsPerLine,
      gridSize: g.chartConfig.gridSize,
      slippageTolerance: g.chartConfig.slippageTolerance,
      fatFingerProtection: g.chartConfig.fatFingerProtection,
    },
  };
}
"""

MULTIPLIERS_JS = r"""
() => {
  const g = window.__chart;
  if (!g || !g.quotesFeed) return { ok: false, reason: 'chart_not_hooked' };
  const qd = g.quotesFeed.quoteData || {};
  const { data, startingIndex, timeSteps, priceSteps } = qd;
  if (!data || !startingIndex) return { ok: false, reason: 'no_quote_data' };
  const cells = [];
  for (let dx = 0; dx < timeSteps; dx++) {
    for (let dy = 0; dy < priceSteps; dy++) {
      const v = data[dx * priceSteps + dy];
      if (v == null) continue;
      cells.push({
        cellX: startingIndex.timeIndex + dx,
        cellY: startingIndex.priceIndex + dy,
        mult: v / 100,
      });
    }
  }
  return {
    ok: true,
    lastGridTimestamp: g.quotesFeed.lastGridTimestamp,
    isStale: g.quotesFeed.isStale,
    startingIndex,
    timeSteps,
    priceSteps,
    cells,
  };
}
"""


# ---------------------------------------------------------------------------
# HTTP API
# ---------------------------------------------------------------------------


async def http_state(_req: web.Request) -> web.Response:
    return web.json_response({
        "price": STATE.price,
        "priceTs": STATE.price_ts,
        "balance": STATE.balance,
        "nonce": STATE.nonce,
        "lastTrade": STATE.last_trade,
        "frameBuf": len(STATE.frames),
    })


async def http_frames(_req: web.Request) -> web.Response:
    # Last N frames for debugging. Trim large payloads. deque doesn't slice,
    # so materialize via list() first.
    return web.json_response(list(STATE.frames)[-50:])


_chart_cache = {"ts": 0.0, "value": None}
_chart_lock = asyncio.Lock()
CHART_CACHE_TTL_MS = 1000  # balance/state shouldn't change within a second


async def http_chart(req: web.Request) -> web.Response:
    """Cached + coalesced /chart. Balance/state rarely changes within 1s,
    so many bots can share one Playwright eval per second."""
    page: Page = req.app["page"]
    now_ms = int(time.time() * 1000)
    if (_chart_cache["value"] is not None
            and now_ms - _chart_cache["ts"] * 1000 < CHART_CACHE_TTL_MS):
        return web.json_response(_chart_cache["value"])
    async with _chart_lock:
        now_ms = int(time.time() * 1000)
        if (_chart_cache["value"] is not None
                and now_ms - _chart_cache["ts"] * 1000 < CHART_CACHE_TTL_MS):
            return web.json_response(_chart_cache["value"])
        await install_chart_hook(page)
        try:
            result = await page.evaluate(CHART_STATE_JS)
        except Exception as e:
            return web.json_response({"error": repr(e)}, status=500)
        if isinstance(result, dict):
            _chart_cache["value"] = result
            _chart_cache["ts"] = time.time()
        return web.json_response(result)


_mults_cache = {"ts": 0.0, "value": None}
_mults_lock = asyncio.Lock()
MULTIPLIERS_CACHE_TTL_MS = 500
LIVE_QUOTES_FRESH_MS = 8000  # how recently the WS-direct listener must have fired


def _stamp_freshness(result: dict) -> dict:
    """Attach serverTs and quoteAgeMs based on the in-payload lastGridTimestamp."""
    out = dict(result)
    now_ms = int(time.time() * 1000)
    lgt = out.get("lastGridTimestamp")
    if isinstance(lgt, (int, float)):
        out["serverTs"] = now_ms
        out["quoteAgeMs"] = now_ms - int(lgt)
    out["ok"] = True
    return out


async def http_multipliers(req: web.Request) -> web.Response:
    """Serves /multipliers from (in priority order):
    1. The WS-direct in-page subscriber's live mirror (STATE.live_multipliers)
       — sub-millisecond, no Playwright eval.
    2. A short-TTL cache that coalesces concurrent callers.
    3. A fresh Playwright eval as fallback.
    """
    page: Page = req.app["page"]
    now_s = time.time()

    # 1) WS-direct path: in-memory, push-driven by quotesFeed.subscribe().
    if (STATE.live_multipliers is not None
            and (now_s - STATE.live_multipliers_ts) * 1000 < LIVE_QUOTES_FRESH_MS):
        return web.json_response(_stamp_freshness(STATE.live_multipliers))

    # 2) Coalesced cache path (covers the period between attach and the first
    # listener fire, and any case where the in-page listener is gone).
    now_ms = int(now_s * 1000)
    if (_mults_cache["value"] is not None
            and now_ms - _mults_cache["ts"] * 1000 < MULTIPLIERS_CACHE_TTL_MS):
        return web.json_response(_stamp_freshness(_mults_cache["value"]))
    async with _mults_lock:
        now_ms = int(time.time() * 1000)
        if (_mults_cache["value"] is not None
                and now_ms - _mults_cache["ts"] * 1000 < MULTIPLIERS_CACHE_TTL_MS):
            return web.json_response(_stamp_freshness(_mults_cache["value"]))
        # 3) Fallback: do the Playwright eval.
        await install_chart_hook(page)
        try:
            result = await page.evaluate(MULTIPLIERS_JS)
        except Exception as e:
            return web.json_response({"error": repr(e)}, status=500)
        if isinstance(result, dict):
            _mults_cache["value"] = result
            _mults_cache["ts"] = time.time()
            # Opportunistically re-install the listener in case it's missing.
            try:
                await install_quote_listener(page)
            except Exception:
                pass
            return web.json_response(_stamp_freshness(result))
        return web.json_response(result, status=500)


def _serve_feed_tape(feed, req: web.Request) -> web.Response:
    try:
        window_ms = int(req.query.get("window_ms", "60000"))
    except ValueError:
        window_ms = 60_000
    now_ms = int(time.time() * 1000)
    trades = feed.window(window_ms)
    ticks = [(t.ts_ms, t.price) for t in trades]
    latest = feed.latest()
    return web.json_response({
        "serverTs": now_ms,
        "windowMs": window_ms,
        "count": len(ticks),
        "ticks": ticks,
        "latest": {"ts_ms": latest.ts_ms, "price": latest.price} if latest else None,
        "latestAgeMs": (now_ms - latest.ts_ms) if latest else None,
        "connected": feed.connected,
        "msgCount": feed.msg_count,
        "lastError": feed.last_error,
    })


async def http_settlements(req: web.Request) -> web.Response:
    """Read last N entries from the persistent settlement log."""
    try:
        n = int(req.query.get("n", "100"))
    except ValueError:
        n = 100
    entries: list[dict] = []
    try:
        if os.path.exists(SETTLEMENT_LOG_PATH):
            with open(SETTLEMENT_LOG_PATH) as f:
                lines = f.readlines()[-n:]
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    except Exception as e:
        return web.json_response({"error": repr(e)}, status=500)
    return web.json_response({
        "path": SETTLEMENT_LOG_PATH,
        "count": len(entries),
        "entries": entries,
    })


async def http_binance_tape(req: web.Request) -> web.Response:
    return _serve_feed_tape(req.app["binance"], req)


async def http_coinbase_tape(req: web.Request) -> web.Response:
    return _serve_feed_tape(req.app["coinbase"], req)


async def http_price_tape(req: web.Request) -> web.Response:
    """Return all WS price ticks within the last `window_ms` (default 60_000)."""
    try:
        window_ms = int(req.query.get("window_ms", "60000"))
    except ValueError:
        window_ms = 60_000
    now_ms = int(time.time() * 1000)
    cutoff = now_ms - window_ms
    ticks = [(ts, p) for (ts, p) in STATE.price_tape if ts >= cutoff]
    return web.json_response({
        "serverTs": now_ms,
        "windowMs": window_ms,
        "count": len(ticks),
        "ticks": ticks,
        "tapeSize": len(STATE.price_tape),
    })


async def http_tap(req: web.Request) -> web.Response:
    """Place a tap and wait for its on-chain lifecycle.

    Key semantics (fixed from earlier race-y version):
      - placeBet's return value is NOT used to declare failure. The XState
        machine processes async, and placeBet often returns a truthy
        sentinel even when the trade goes through. Truth is established by
        the lifecycle events (requested → matched → executed → settled).
      - Real failure conditions are detected from TAP_JS reasons:
        chart_not_hooked / no_quote_for_cell / placeBet_threw → 422.
      - Otherwise we wait for `executed` (always returned). If body asks
        ?wait=settled (or timeoutS large enough), we ALSO wait for `settled`
        so callers get the win/loss outcome in one round-trip.
    """
    page: Page = req.app["page"]
    if STATE.current_tap is not None:
        return web.json_response({"status": "busy", "tradeId": None}, status=409)

    body = await req.json()
    cell_x = int(body["cellX"])
    cell_y = int(body["cellY"])
    amount = float(body.get("amount", 1))
    slippage = int(body.get("slippage", 60))
    # Default timeout now generous enough to span a typical 30s-60s position window.
    timeout_s = float(body.get("timeoutS", 60))
    wait_settled = bool(body.get("waitSettled", True))

    # Make sure the chart is exposed at window.__chart before tapping.
    hook = await install_chart_hook(page)
    if not (isinstance(hook, dict) and hook.get("ok")):
        return web.json_response({"status": "chart_hook_failed",
                                   "tradeId": None, "hook": hook}, status=422)

    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    settled_fut: asyncio.Future = asyncio.get_event_loop().create_future()
    STATE.current_tap = {
        "started_at": time.time(),
        "future": fut,
        "settled_future": settled_fut,
        "sub_id": None,
        "trade_id": None,
        "submitted_payload": None,
        "lifecycle": [{"status": "tap_requested", "ts": time.time()}],
        "tap_args": {"cellX": cell_x, "cellY": cell_y,
                     "amount": amount, "slippage": slippage},
    }
    try:
        t0 = time.time()
        js_result = await tap_cell(page, cell_x, cell_y, amount, slippage)
        # Detect HARD failures (chart broken, no quote, exception) — return 422.
        # IMPORTANT: A non-ok js_result with a sentinel "placeBetReturn" is NOT
        # a hard failure — placeBet just returned info; the trade still flows
        # through XState. Only the listed reasons mean "definitely didn't
        # submit". Anything else: wait for the lifecycle.
        hard_fail_reasons = {"chart_not_hooked", "no_quote_for_cell",
                              "placeBet_threw"}
        if (isinstance(js_result, dict) and not js_result.get("ok")
                and js_result.get("reason") in hard_fail_reasons):
            return web.json_response({"status": "placeBet_hard_failed",
                                       "tradeId": STATE.current_tap.get("trade_id"),
                                       "jsResult": js_result}, status=422)

        try:
            executed = await asyncio.wait_for(fut, timeout=timeout_s)
        except asyncio.TimeoutError:
            return web.json_response({
                "status": "executed_timeout",
                "tradeId": STATE.current_tap.get("trade_id"),
                "tap": _serialize_tap(STATE.current_tap),
                "tapJsResult": js_result,
            }, status=504)

        # Optionally wait for the position to settle so caller gets win/loss in one shot.
        settled = None
        settled_status = "not_waited"
        if wait_settled:
            # Position windows can be up to ~60s. Use remaining timeout.
            remaining = max(5.0, timeout_s - (time.time() - t0))
            try:
                settled = await asyncio.wait_for(settled_fut, timeout=remaining)
                settled_status = "settled"
            except asyncio.TimeoutError:
                settled_status = "settled_timeout"

        rtt_ms = int((time.time() - t0) * 1000)
        return web.json_response({
            "status": "executed",
            "settledStatus": settled_status,
            "rttMs": rtt_ms,
            "tradeId": STATE.current_tap["trade_id"],
            "lifecycle": STATE.current_tap["lifecycle"],
            "submittedPayload": STATE.current_tap["submitted_payload"],
            "trade": executed,
            "settled": settled,
            "tapJsResult": js_result,
        })
    finally:
        STATE.current_tap = None


def _serialize_tap(tap: Optional[dict]) -> Optional[dict]:
    if tap is None:
        return None
    # Future objects don't JSON-serialize; strip both.
    return {k: v for k, v in tap.items() if k not in ("future", "settled_future")}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> None:
    pw, browser, page = await _attach()
    print(f"[proxy] attached to {page.url}")

    # Centralized external feeds (shared across all bots, replaces per-bot WS).
    binance = BinanceFeed()
    binance.start()
    coinbase = CoinbaseFeed()
    coinbase.start()
    # Expose feeds to the WS-frame handler so settle-time tick snapshots can
    # enrich settlements.jsonl entries.
    STATE.binance_feed = binance
    STATE.coinbase_feed = coinbase
    print(f"[proxy] started Binance + Coinbase feeds")

    # Install chart hook and the WS-direct quotesFeed listener.
    try:
        hook = await install_chart_hook(page)
        print(f"[proxy] chart hook: {hook}")
        if isinstance(hook, dict) and hook.get("ok"):
            ql = await install_quote_listener(page)
            print(f"[proxy] quote listener: {ql}")
    except Exception as e:
        print(f"[proxy] startup hook/listener warning: {e}")

    app = web.Application()
    app["page"] = page
    app["binance"] = binance
    app["coinbase"] = coinbase
    app.router.add_get("/state", http_state)
    app.router.add_get("/frames", http_frames)
    app.router.add_get("/chart", http_chart)
    app.router.add_get("/multipliers", http_multipliers)
    app.router.add_get("/price_tape", http_price_tape)
    app.router.add_get("/binance_tape", http_binance_tape)
    app.router.add_get("/coinbase_tape", http_coinbase_tape)
    app.router.add_get("/settlements", http_settlements)
    app.router.add_post("/tap", http_tap)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", PROXY_PORT)
    await site.start()
    print(f"[proxy] HTTP listening on http://127.0.0.1:{PROXY_PORT}")
    print("[proxy] endpoints: GET /state  GET /frames  GET /chart"
          "  GET /multipliers  GET /price_tape  GET /binance_tape  GET /coinbase_tape"
          "  GET /settlements  POST /tap")
    print(f"[proxy] settlement log: {SETTLEMENT_LOG_PATH}")

    try:
        await asyncio.Event().wait()
    finally:
        binance.stop()
        coinbase.stop()
        await runner.cleanup()
        await browser.close()
        await pw.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
