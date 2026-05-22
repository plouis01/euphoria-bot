"""
Enumerate all WS connections on the Euphoria page and dump a sample of
their frames. Goal: find which one carries quote/multiplier updates.

Connects to live Chrome via CDP, attaches new listeners that record frames
for ~30s, then prints a per-WS summary with sample frame structures.
"""
import asyncio
import json
import time
from collections import defaultdict
from playwright.async_api import async_playwright


CDP_URL = "http://127.0.0.1:9222"
EUPHORIA_HOST = "euphoria.finance"
DURATION_S = 30


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

    print(f"Attached to {page.url}")

    ws_records: dict[int, dict] = {}
    next_ws_id = [0]

    def hook(ws):
        wid = next_ws_id[0]
        next_ws_id[0] += 1
        url = getattr(ws, "url", "?")
        rec = {
            "url": url,
            "n_recv": 0,
            "n_send": 0,
            "samples_recv": [],
            "samples_send": [],
            "type_counts_recv": defaultdict(int),
        }
        ws_records[wid] = rec
        print(f"WS#{wid} new: {url}")

        def on_recv(payload):
            rec["n_recv"] += 1
            try:
                obj = json.loads(payload)
            except Exception:
                obj = {"_raw_len": len(payload)}
            if rec["n_recv"] <= 12:
                rec["samples_recv"].append(obj)
            # Categorize
            cat = "other"
            if isinstance(obj, dict):
                result = obj.get("result")
                if isinstance(result, dict):
                    j = result.get("data", {}).get("json", {}) if isinstance(result.get("data"), dict) else {}
                    if isinstance(j, dict):
                        if "price" in j and "timestamp" in j and len(j) <= 2:
                            cat = "price-tick"
                        elif j.get("type") == "order":
                            cat = "order"
                        elif j.get("type") == "position":
                            cat = "position"
                        elif "cells" in j or "data" in j or "quoteData" in j:
                            cat = "quotes?"
                        else:
                            cat = f"data:{sorted(j.keys())[:4]}"
                elif obj.get("method"):
                    cat = f"method:{obj['method']}"
            rec["type_counts_recv"][cat] += 1

        def on_send(payload):
            rec["n_send"] += 1
            try:
                obj = json.loads(payload)
            except Exception:
                obj = {"_raw_len": len(payload)}
            if rec["n_send"] <= 8:
                rec["samples_send"].append(obj)

        ws.on("framereceived", on_recv)
        ws.on("framesent", on_send)

    page.on("websocket", hook)
    print(f"Listening {DURATION_S}s...")
    await asyncio.sleep(DURATION_S)

    print("\n=== Summary ===")
    for wid, r in ws_records.items():
        print(f"\nWS#{wid} {r['url']}")
        print(f"  recv: {r['n_recv']}  send: {r['n_send']}")
        print(f"  type_counts_recv: {dict(r['type_counts_recv'])}")
        if r["samples_send"]:
            print(f"  first send sample:")
            print(f"    {json.dumps(r['samples_send'][0])[:600]}")
        if r["samples_recv"]:
            non_price = [s for s in r["samples_recv"]
                          if not (isinstance(s, dict)
                                  and isinstance(s.get("result", {}), dict)
                                  and isinstance(s["result"].get("data", {}), dict)
                                  and "price" in (s["result"]["data"].get("json") or {})
                                  and "timestamp" in (s["result"]["data"].get("json") or {}))]
            if non_price:
                print(f"  first NON-price recv sample:")
                print(f"    {json.dumps(non_price[0])[:600]}")
            else:
                print(f"  recv samples all price-ticks; first sample:")
                print(f"    {json.dumps(r['samples_recv'][0])[:300]}")


if __name__ == "__main__":
    asyncio.run(main())
