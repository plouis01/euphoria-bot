"""Find listener API + WS construction path on quotesFeed."""
import asyncio
import json
from playwright.async_api import async_playwright

CDP_URL = "http://127.0.0.1:9222"
EUPHORIA_HOST = "euphoria.finance"

PROBE_JS = r"""
() => {
  const out = {};
  const qf = window.__chart && window.__chart.quotesFeed;
  if (!qf) return { error: 'no_quotesFeed' };

  // Walk prototype chain
  let p = qf;
  const protos = [];
  let depth = 0;
  while (p && depth < 5) {
    const props = Object.getOwnPropertyNames(p);
    protos.push({ depth, ctor: p.constructor && p.constructor.name, props });
    p = Object.getPrototypeOf(p);
    depth++;
  }
  out.prototypes = protos;

  // Look at the source of methods that look like subscription/listener registration
  const subscriberMethods = [];
  let p2 = Object.getPrototypeOf(qf);
  if (p2) {
    for (const k of Object.getOwnPropertyNames(p2)) {
      const fn = p2[k];
      if (typeof fn === 'function') {
        const src = fn.toString().slice(0, 400);
        if (/listener|subscribe|on[A-Z]|addEvent/.test(k) || /listener/i.test(src)) {
          subscriberMethods.push({ name: k, snippet: src });
        }
      }
    }
  }
  out.subscriberMethods = subscriberMethods;

  // Same for priceFeed
  const pf = window.__chart && window.__chart.priceFeed;
  if (pf) {
    let p3 = Object.getPrototypeOf(pf);
    const pfMethods = [];
    if (p3) {
      for (const k of Object.getOwnPropertyNames(p3)) {
        const fn = p3[k];
        if (typeof fn === 'function') {
          if (/listener|subscribe|on[A-Z]|addEvent/i.test(k)) {
            pfMethods.push({ name: k, snippet: fn.toString().slice(0, 250) });
          }
        }
      }
    }
    out.priceFeedSubscriberMethods = pfMethods;
  }

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
    if page is None: print("No tab"); return
    result = await page.evaluate(PROBE_JS)
    print(json.dumps(result, indent=2, default=str)[:8000])

if __name__ == "__main__":
    asyncio.run(main())
