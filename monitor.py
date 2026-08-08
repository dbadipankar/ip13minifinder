#!/usr/bin/env python3
"""
Cashify watcher - alerts on Telegram when a refurbished iPhone 13 mini in
SUPERB condition is in stock (optionally under a price cap).

Loads the page in real headless Chrome via Playwright, so it works whether
Cashify renders server-side or client-side.

Setup: see README.md
"""

import json
import os
import re
import sys
import time
from pathlib import Path

import requests

# ---------------------------------------------------------------- config ----
PRODUCT_URL = os.getenv(
    "PRODUCT_URL",
    "https://www.cashify.in/buy-refurbished-mobile-phones/renewed-apple-iphone-13-mini",
)
WANT_CONDITION = os.getenv("WANT_CONDITION", "superb").lower()
MAX_PRICE = int(os.getenv("MAX_PRICE", "0"))          # rupees; 0 = no cap
WANT_STORAGE = os.getenv("WANT_STORAGE", "").strip()  # e.g. "128"; blank = any

TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID")

STATE_FILE = Path(os.getenv("STATE_FILE", "state.json"))
DEBUG_DUMP = os.getenv("DEBUG_DUMP", "").lower() in ("1", "true", "yes")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# How many consecutive failed runs before we warn you the watcher is broken.
BROKEN_AFTER = 3


# ------------------------------------------------------------- fetching ----
def fetch_html(url: str, tries: int = 3) -> str:
    """Real browser render. Falls back to plain HTTP if Playwright is absent."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[warn] playwright not installed, falling back to requests")
        r = requests.get(url, headers={"User-Agent": UA,
                                       "Accept-Language": "en-IN,en;q=0.9"},
                         timeout=30)
        r.raise_for_status()
        return r.text

    last = None
    for attempt in range(tries):
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    args=["--disable-blink-features=AutomationControlled"]
                )
                ctx = browser.new_context(
                    user_agent=UA,
                    locale="en-IN",
                    viewport={"width": 1366, "height": 900},
                )
                page = ctx.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                # give client-side price widgets time to populate
                try:
                    page.wait_for_load_state("networkidle", timeout=20000)
                except Exception:
                    pass
                page.wait_for_timeout(2500)
                html = page.content()
                browser.close()
                if len(html) > 5000:
                    return html
                last = RuntimeError(f"suspiciously short page ({len(html)} bytes)")
        except Exception as e:  # noqa: BLE001
            last = e
        time.sleep(4 * (attempt + 1))
    raise RuntimeError(f"fetch failed after {tries} tries: {last}")


# -------------------------------------------------------------- parsing ----
def extract_embedded_json(html: str):
    m = re.search(
        r'<script id="__NEXT_DATA__" type="application/json"[^>]*>(.*?)</script>',
        html, re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    blocks = re.findall(
        r'<script type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S)
    out = []
    for b in blocks:
        try:
            out.append(json.loads(b))
        except json.JSONDecodeError:
            continue
    return out or None


def walk(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk(v)


def find_variants(data) -> list[dict]:
    """Any dict carrying a condition/grade label plus a plausible price."""
    variants, seen = [], set()

    cond_keys = ("condition", "conditionname", "grade", "gradename",
                 "variantname", "quality")
    price_keys = ("sellingprice", "saleprice", "price", "offerprice",
                  "finalprice", "discountedprice", "mrp")
    stock_keys = ("instock", "isinstock", "available", "isavailable",
                  "availability", "stock", "quantity", "soldout")

    for d in walk(data):
        lower = {str(k).lower(): v for k, v in d.items()}

        cond = None
        for k in cond_keys:
            v = lower.get(k)
            if isinstance(v, str) and v.strip():
                cond = v.strip()
                break
        if not cond:
            continue

        price = None
        for k in price_keys:
            v = lower.get(k)
            if isinstance(v, (int, float)) and 1000 < v < 500000:
                price = int(v)
                break
            if isinstance(v, str) and re.fullmatch(r"[\d,]+(\.\d+)?", v.strip()):
                n = int(float(v.replace(",", "")))
                if 1000 < n < 500000:
                    price = n
                    break
        if price is None:
            continue

        stock = None
        for k in stock_keys:
            if k in lower:
                v = lower[k]
                if isinstance(v, bool):
                    stock = (not v) if k == "soldout" else v
                elif isinstance(v, (int, float)):
                    stock = v > 0
                elif isinstance(v, str):
                    s = v.strip().lower()
                    stock = not any(x in s for x in
                                    ("false", "outofstock", "out of stock",
                                     "sold out", "unavailable"))
                break

        storage = ""
        for k in ("storage", "internalmemory", "memory", "rom", "variant"):
            v = lower.get(k)
            if isinstance(v, (str, int)) and str(v).strip():
                storage = str(v).strip()
                break

        key = (cond.lower(), price, storage)
        if key in seen:
            continue
        seen.add(key)
        variants.append({
            "condition": cond,
            "price": price,
            "storage": storage,
            "in_stock": True if stock is None else stock,
            "stock_known": stock is not None,
        })
    return variants


def matches(v: dict) -> bool:
    if WANT_CONDITION not in v["condition"].lower():
        return False
    if not v["in_stock"]:
        return False
    if MAX_PRICE and v["price"] > MAX_PRICE:
        return False
    if WANT_STORAGE and WANT_STORAGE not in v["storage"].replace(" ", ""):
        return False
    return True


# --------------------------------------------------------------- alerts ----
def notify(text: str) -> None:
    print(text)
    if not (TG_TOKEN and TG_CHAT):
        print("[warn] Telegram not configured; printed only.")
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML"},
            timeout=20)
        r.raise_for_status()
    except Exception as e:  # noqa: BLE001
        print(f"[error] telegram send failed: {e}", file=sys.stderr)


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def record_failure(state: dict, reason: str) -> None:
    """Only nags you once per broken streak, not every 15 minutes."""
    n = state.get("fail_streak", 0) + 1
    state["fail_streak"] = n
    print(f"[fail {n}] {reason}")
    if n == BROKEN_AFTER:
        notify("⚠️ <b>Cashify watcher is broken</b>\n\n"
               f"{BROKEN_AFTER} runs in a row failed: {reason}\n\n"
               "The page structure likely changed. Run locally with "
               "DEBUG_DUMP=1 and inspect page.html.")
    save_state(state)


# ----------------------------------------------------------------- main ----
def main() -> int:
    state = load_state()

    try:
        html = fetch_html(PRODUCT_URL)
    except Exception as e:  # noqa: BLE001
        record_failure(state, f"fetch error: {e}")
        return 1

    if DEBUG_DUMP:
        Path("page.html").write_text(html)
        print("[debug] wrote page.html")

    data = extract_embedded_json(html)
    if data is None:
        record_failure(state, "no embedded JSON found on page")
        return 1

    variants = find_variants(data)
    if not variants:
        record_failure(state, "embedded JSON found but no variants parsed")
        return 1

    # Healthy run.
    if state.get("fail_streak", 0) >= BROKEN_AFTER:
        notify("✅ Cashify watcher recovered and is running normally again.")
    state["fail_streak"] = 0

    hits = [v for v in variants if matches(v)]
    print(f"Found {len(variants)} variants, {len(hits)} matching "
          f"(condition~'{WANT_CONDITION}', cap={MAX_PRICE or 'none'}).")
    for v in variants:
        flag = "?" if not v["stock_known"] else ("in" if v["in_stock"] else "OUT")
        print(f"  - {v['condition']:<14} Rs.{v['price']:<8} "
              f"{v['storage']:<10} stock={flag}")

    fingerprint = sorted(f"{v['condition']}|{v['storage']}|{v['price']}"
                         for v in hits)
    prev = state.get("last_hits", [])

    if hits and fingerprint != prev:
        lines = [f"🔔 <b>iPhone 13 mini — {WANT_CONDITION.title()} available</b>", ""]
        for v in hits:
            store = f" · {v['storage']}" if v["storage"] else ""
            lines.append(f"• {v['condition']}{store} — <b>₹{v['price']:,}</b>")
        lines += ["", PRODUCT_URL]
        notify("\n".join(lines))
    elif not hits and prev:
        notify(f"ℹ️ {WANT_CONDITION.title()} iPhone 13 mini is no longer "
               f"listed at your criteria.")

    state["last_hits"] = fingerprint
    state["last_ok"] = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
