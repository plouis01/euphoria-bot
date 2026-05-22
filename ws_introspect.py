"""
Find existing WebSocket connections in the Euphoria page via JS introspection.
Uses several tactics:
  1. performance.getEntries() for resource URLs
  2. window-level objects that might hold WS refs (tRPC client, etc.)
  3. Monkey-patch WebSocket to track future opens

Read-only — won't disturb running proxy.
"""
import asyncio
import json
from playwright.async_api import async_playwright

CDP_URL = "http://127.0.0.1:9222"
EUPHORIA_HOST = "euphoria.finance"


PROBE_JS = r"""
() => {
  const out = { perfResources: [], windowKeys: [], inspections: {} };

  // 1) Performance entries
  try {
    const entries = performance.getEntries();
    for (const e of entries) {
      if (e.name && (e.name.includes('ws://') || e.name.includes('wss://') ||
                     e.initiatorType === 'websocket')) {
        out.perfResources.push({
          name: e.name,
          type: e.entryType,
          init: e.initiatorType,
          start: e.startTime,
        });
      }
    }
  } catch (e) { out.perfErr = String(e); }

  // 2) Look at common globals that often hold WS or tRPC clients
  const candidates = ['__chart', '__trpc', '__trpcClient', '__nats', 'natsConnection', '__wsClient'];
  for (const k of candidates) {
    if (window[k] !== undefined) {
      const v = window[k];
      const info = { type: typeof v, keys: [] };
      try { info.keys = Object.keys(v).slice(0, 30); } catch {}
      try { info.ctor = v.constructor && v.constructor.name; } catch {}
      out.inspections[k] = info;
    }
  }

  // 3) Look inside __chart for any 'feed' / 'ws' / 'connection' refs
  try {
    const g = window.__chart;
    if (g) {
      const findWs = (obj, path, depth) => {
        if (depth > 4 || !obj || typeof obj !== 'object') return;
        for (const k of Object.keys(obj)) {
          const v = obj[k];
          if (v && typeof v === 'object') {
            const ctor = v.constructor && v.constructor.name;
            if (ctor === 'WebSocket' ||
                (v.url && typeof v.url === 'string' && v.url.startsWith('ws'))) {
              out.inspections[`${path}.${k}`] = {
                ctor: ctor,
                url: v.url,
                readyState: v.readyState,
              };
            } else if (k.toLowerCase().includes('feed') ||
                       k.toLowerCase().includes('ws') ||
                       k.toLowerCase().includes('socket') ||
                       k.toLowerCase().includes('nats') ||
                       k.toLowerCase().includes('client')) {
              findWs(v, `${path}.${k}`, depth + 1);
            }
          }
        }
      };
      findWs(g, '__chart', 0);
    }
  } catch (e) { out.chartErr = String(e); }

  // 4) Look at React fiber memoizedState for any WS refs (cheap shallow scan)
  try {
    const canvas = document.querySelector('canvas');
    if (canvas) {
      const fk = Object.keys(canvas).find(k => k.startsWith('__reactFiber'));
      if (fk) {
        let f = canvas[fk];
        let hops = 0;
        while (f && hops < 20) {
          const ms = f.memoizedState;
          if (ms && typeof ms === 'object') {
            for (const sk of Object.keys(ms)) {
              const v = ms[sk];
              if (v && typeof v === 'object' && v.constructor && v.constructor.name === 'WebSocket') {
                out.inspections[`fiber.${hops}.${sk}`] = {
                  ctor: 'WebSocket', url: v.url, readyState: v.readyState,
                };
              }
            }
          }
          f = f.return;
          hops++;
        }
      }
    }
  } catch (e) { out.fiberErr = String(e); }

  // 5) Monkey-patch WebSocket constructor going forward + retain existing if possible
  try {
    if (!window.__ws_intercepts) {
      window.__ws_intercepts = [];
      const orig = window.WebSocket;
      window.WebSocket = function(url, protocols) {
        const ws = new orig(url, protocols);
        window.__ws_intercepts.push({ url, t: Date.now() });
        return ws;
      };
      // Inherit prototype
      window.WebSocket.prototype = orig.prototype;
      window.WebSocket.OPEN = orig.OPEN;
      out.monkeyPatched = true;
    } else {
      out.alreadyPatched = true;
      out.priorIntercepts = window.__ws_intercepts.slice(-20);
    }
  } catch (e) { out.patchErr = String(e); }

  return out;
}
"""


async def main():
    pw = await async_playwright().start()
    browser = await pw.chromium.connect_over_cdp(CDP_URL)
    page = None
    for ctx in browser.contexts:
        for p in ctx.pages:
            if EUPHORIA_HOST in (p.url or ""):
                page = p
                break
        if page:
            break
    if page is None:
        print("No Euphoria tab found")
        return

    result = await page.evaluate(PROBE_JS)
    print(json.dumps(result, indent=2, default=str)[:6000])


if __name__ == "__main__":
    asyncio.run(main())
