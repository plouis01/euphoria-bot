"""Deeper introspection of __chart.quotesFeed / priceFeed / tradeFeed."""
import asyncio
import json
from playwright.async_api import async_playwright

CDP_URL = "http://127.0.0.1:9222"
EUPHORIA_HOST = "euphoria.finance"

PROBE_JS = r"""
() => {
  const out = {};
  const g = window.__chart;
  if (!g) return { error: 'no_chart' };

  // Walk each feed
  for (const feedName of ['quotesFeed', 'priceFeed', 'tradeFeed']) {
    const f = g[feedName];
    if (!f) continue;
    const info = { keys: [], internals: {} };
    try {
      info.keys = Object.keys(f);
      info.ctor = f.constructor && f.constructor.name;
    } catch {}
    // Walk each property briefly
    for (const k of info.keys) {
      try {
        const v = f[k];
        const t = typeof v;
        if (t === 'function') {
          info.internals[k] = { type: 'function', name: v.name, len: v.length };
        } else if (t === 'object' && v !== null) {
          const ctor = v.constructor && v.constructor.name;
          const innerKeys = Object.keys(v).slice(0, 12);
          info.internals[k] = { type: 'object', ctor, keys: innerKeys, url: v.url };
        } else {
          info.internals[k] = { type: t, value: String(v).slice(0, 100) };
        }
      } catch (e) {
        info.internals[k] = { error: String(e) };
      }
    }
    out[feedName] = info;
  }

  // Specifically find any WebSocket inside the feeds (BFS up to depth 4)
  out.wsFound = [];
  const seen = new Set();
  const search = (obj, path, depth) => {
    if (depth > 5) return;
    if (!obj || typeof obj !== 'object' || seen.has(obj)) return;
    seen.add(obj);
    const ctor = obj.constructor && obj.constructor.name;
    if (ctor === 'WebSocket' || (obj.url && typeof obj.url === 'string' && obj.url.startsWith('ws'))) {
      out.wsFound.push({ path, ctor, url: obj.url, readyState: obj.readyState });
      return;
    }
    try {
      for (const k of Object.keys(obj)) {
        if (k.startsWith('_') && depth > 1) continue;
        search(obj[k], `${path}.${k}`, depth + 1);
      }
    } catch {}
  };
  search(g, '__chart', 0);

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
        if page: break
    if page is None:
        print("No Euphoria tab found"); return

    result = await page.evaluate(PROBE_JS)
    print(json.dumps(result, indent=2, default=str)[:8000])

if __name__ == "__main__":
    asyncio.run(main())
