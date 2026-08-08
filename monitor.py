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
def parse_list(raw: str) -> list[str]:
    """'Superb, Good' -> ['superb', 'good']. Blank -> [] meaning 'any'."""
    return [x.strip().lower() for x in raw.split(",") if x.strip()]


# Each accepts a comma-separated list. Blank = accept anything.
WANT_CONDITION = parse_list(os.getenv("WANT_CONDITION", "superb,good"))
WANT_STORAGE = parse_list(os.getenv("WANT_STORAGE", "128,256"))
WANT_COLOR = parse_list(os.getenv("WANT_COLOR", ""))
MAX_PRICE = int(os.getenv("MAX_PRICE", "0"))          # rupees; 0 = no cap

# If a listing doesn't state its colour, alert anyway rather than stay silent.
ALERT_ON_UNKNOWN_COLOR = os.getenv("ALERT_ON_UNKNOWN_COLOR", "1") not in ("0", "false")

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


GRADE_WORDS = ("superb", "good", "fair", "excellent", "like new",
               "best value", "pristine", "flawless")


def _text_of(v):
    """schema.org fields are sometimes a string, sometimes a nested object."""
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        for k in ("name", "value", "@id", "url"):
            if isinstance(v.get(k), str):
                return v[k]
    return ""


def _grade_from_product(d: dict, blob: str) -> str:
    """Grade lives in additionalProperty, or is embedded in the name string."""
    props = d.get("additionalProperty")
    if isinstance(props, dict):
        props = [props]
    if isinstance(props, list):
        for p in props:
            if not isinstance(p, dict):
                continue
            ident = f"{p.get('propertyID', '')} {p.get('name', '')}".lower()
            if "grade" in ident or "condition" in ident:
                val = _text_of(p.get("value"))
                if val.strip():
                    return val.strip()

    # Fall back to scanning the product name / description text.
    low = blob.lower()
    for w in GRADE_WORDS:
        if re.search(r"\b" + re.escape(w) + r"\b", low):
            return w.title()
    return ""


def _storage_from_text(blob: str) -> str:
    """'4 GB / 128 GB' -> '128 GB'. RAM comes first, storage second."""
    caps = re.findall(r"(\d+)\s*(GB|TB)\b", blob, re.I)
    if not caps:
        return ""
    if len(caps) >= 2:
        num, unit = caps[1]          # second capacity = storage
    else:
        num, unit = caps[0]
    return f"{num} {unit.upper()}"


def _offer_details(d: dict):
    """Pull price + availability out of the nested offers object."""
    offers = d.get("offers")
    if isinstance(offers, dict):
        offers = [offers]
    if not isinstance(offers, list):
        return None, None

    for o in offers:
        if not isinstance(o, dict):
            continue
        # An AggregateOffer wraps real offers inside it.
        inner = o.get("offers")
        if isinstance(inner, (list, dict)):
            p, a = _offer_details({"offers": inner})
            if p is not None:
                return p, a

        raw = o.get("price", o.get("lowPrice"))
        price = None
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            price = int(raw)
        elif isinstance(raw, str):
            digits = re.sub(r"[^\d.]", "", raw)
            if digits:
                try:
                    price = int(float(digits))
                except ValueError:
                    price = None

        avail = _text_of(o.get("availability"))
        if price is not None and 1000 < price < 500000:
            return price, avail
    return None, None


def find_variants(data) -> list[dict]:
    """Read schema.org Product variants (Cashify's actual format)."""
    variants, seen = [], set()

    for d in walk(data):
        if not isinstance(d, dict):
            continue
        if "Product" not in str(d.get("@type", "")):
            continue

        price, avail = _offer_details(d)
        if price is None:
            continue

        name = d.get("name", "") if isinstance(d.get("name"), str) else ""
        desc = d.get("description", "") if isinstance(d.get("description"), str) else ""
        blob = f"{name} {desc}"

        grade = _grade_from_product(d, blob)
        storage = _storage_from_text(blob)
        color = _text_of(d.get("color")).strip()

        if avail:
            in_stock = "instock" in avail.lower().replace(" ", "")
            stock_known = True
        else:
            in_stock, stock_known = True, False

        key = (grade.lower(), price, storage, color.lower())
        if key in seen:
            continue
        seen.add(key)
        variants.append({
            "condition": grade,
            "price": price,
            "storage": storage,
            "color": color,
            "in_stock": in_stock,
            "stock_known": stock_known,
            "sku": d.get("sku", ""),
        })
    return variants


def any_match(wanted: list[str], field: str) -> bool:
    """True if the field contains any wanted term. Empty list = accept all."""
    if not wanted:
        return True
    haystack = field.lower().replace(" ", "")
    return any(w.replace(" ", "") in haystack for w in wanted)


def matches(v: dict) -> bool:
    if not v["in_stock"]:
        return False
    if MAX_PRICE and v["price"] > MAX_PRICE:
        return False

    # If Cashify didn't publish a grade for this listing, don't drop it
    # silently - surface it rather than risk missing the phone.
    if WANT_CONDITION and v["condition"]:
        if not any_match(WANT_CONDITION, v["condition"]):
            return False

    if not any_match(WANT_STORAGE, v["storage"]):
        return False

    if WANT_COLOR:
        if not v["color"]:
            return ALERT_ON_UNKNOWN_COLOR
        if not any_match(WANT_COLOR, v["color"]):
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
    print(f"Criteria: condition={WANT_CONDITION or 'any'} | "
          f"storage={WANT_STORAGE or 'any'} | color={WANT_COLOR or 'any'} | "
          f"cap={MAX_PRICE or 'none'}")
    grades = sorted({v["condition"] or "(none)" for v in variants})
    print(f"Grades published by Cashify: {grades}")
    print(f"Found {len(variants)} variants, {len(hits)} matching.")
    for v in variants:
        flag = "?" if not v["stock_known"] else ("in" if v["in_stock"] else "OUT")
        mark = "MATCH" if matches(v) else "     "
        print(f"  {mark} {(v['condition'] or '-'):<14} Rs.{v['price']:<8} "
              f"{v['storage']:<10} {(v['color'] or '-'):<12} stock={flag}")

    fingerprint = sorted(
        f"{v['condition']}|{v['storage']}|{v['color']}|{v['price']}"
        for v in hits)
    prev = state.get("last_hits", [])

    if hits and fingerprint != prev:
        lines = ["🔔 <b>iPhone 13 mini — match found on Cashify</b>", ""]
        for v in hits:
            bits = [v["condition"]]
            if v["storage"]:
                bits.append(v["storage"])
            bits.append(v["color"] if v["color"] else "colour not stated")
            lines.append(f"• {' · '.join(bits)} — <b>₹{v['price']:,}</b>")
        lines += ["", PRODUCT_URL]
        notify("\n".join(lines))
    elif not hits and prev:
        notify("ℹ️ No iPhone 13 mini currently matches your criteria "
               "(condition/storage/colour/price).")

    state["last_hits"] = fingerprint
    state["last_ok"] = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
