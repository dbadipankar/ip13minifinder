#!/usr/bin/env python3
"""
Diagnostic. Run this once via the 'Inspect Cashify page' workflow.
It prints Cashify's ACTUAL field names so monitor.py can be corrected.
Sends nothing to Telegram. Changes nothing.
"""

import json
import re
from pathlib import Path

import monitor  # reuse the same fetching code

MARKERS = ("superb", "good", "fair", "excellent", "like new", "refurb")


def truncate(v, n=90):
    s = repr(v)
    return s if len(s) <= n else s[:n] + "..."


def main() -> int:
    print("=" * 70)
    print("FETCHING", monitor.PRODUCT_URL)
    print("=" * 70)

    html = monitor.fetch_html(monitor.PRODUCT_URL)
    Path("page.html").write_text(html)
    print(f"page length: {len(html):,} bytes")

    # --- does the word 'Superb' even appear in the raw page? -------------
    print("\n--- CONDITION WORDS IN RAW HTML ---")
    for w in MARKERS:
        n = len(re.findall(w, html, re.I))
        print(f"  {w:<12} appears {n} times")

    data = monitor.extract_embedded_json(html)
    if data is None:
        print("\nNo embedded JSON at all. Page is fully client-rendered.")
        return 1

    Path("data.json").write_text(json.dumps(data, indent=1)[:4_000_000])
    print("\nEmbedded JSON captured -> data.json")

    dicts = list(monitor.walk(data))
    print(f"total nested objects: {len(dicts):,}")

    # --- objects that mention a condition word --------------------------
    print("\n--- OBJECTS MENTIONING A CONDITION WORD (first 12) ---")
    shown = 0
    for d in dicts:
        if shown >= 12:
            break
        flat = json.dumps(d, default=str).lower()
        if not any(m in flat for m in MARKERS):
            continue
        if len(d) > 30:          # skip giant container objects
            continue
        print(f"\n  OBJECT #{shown + 1}  keys={list(d.keys())}")
        for k, v in list(d.items())[:18]:
            if not isinstance(v, (dict, list)):
                print(f"      {k:<26} = {truncate(v)}")
        shown += 1
    if shown == 0:
        print("  none found - condition data may load separately via API")

    # --- every key whose value looks like a rupee price -----------------
    print("\n--- KEYS HOLDING PRICE-LIKE NUMBERS (1000-500000) ---")
    price_keys = {}
    for d in dicts:
        for k, v in d.items():
            num = None
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                num = v
            elif isinstance(v, str) and re.fullmatch(r"[\d,]+(\.\d+)?", v.strip() or "x"):
                try:
                    num = float(v.replace(",", ""))
                except ValueError:
                    num = None
            if num is not None and 1000 < num < 500000:
                price_keys.setdefault(str(k), []).append(int(num))
    for k, vals in sorted(price_keys.items(), key=lambda x: -len(x[1]))[:20]:
        print(f"  {k:<28} {len(vals):>4} values, e.g. {sorted(set(vals))[:5]}")
    if not price_keys:
        print("  none - prices likely load via a separate API call")

    # --- keys that look like condition / storage / colour ---------------
    print("\n--- KEYS RESEMBLING condition / storage / colour ---")
    interesting = {}
    for d in dicts:
        for k, v in d.items():
            kl = str(k).lower()
            if any(t in kl for t in ("condition", "grade", "quality", "storage",
                                     "memory", "rom", "color", "colour",
                                     "variant", "stock", "avail")):
                if not isinstance(v, (dict, list)):
                    interesting.setdefault(str(k), set()).add(truncate(v, 40))
    for k, vals in sorted(interesting.items())[:25]:
        print(f"  {k:<28} -> {sorted(vals)[:6]}")
    if not interesting:
        print("  none found")

    print("\n" + "=" * 70)
    print("Copy everything above and send it to Claude.")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
