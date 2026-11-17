"""Physical bullion reference marks (not a dealer's guaranteed buyback price).

Gold reference: NBP 1g of fine gold in PLN -> PLN / troy oz.
Silver reference: gold-api.com live silver spot USD/oz. Historical silver
falls back only to clearly labelled COMEX futures *proxy*, not spot.
"""
from __future__ import annotations

from datetime import date, timedelta
import requests

TROY_OUNCE_GRAMS = 31.1034768


def fetch_nbp_gold(start: date, end: date) -> list[tuple[str, float]]:
    values = []
    day = start
    session = requests.Session()
    while day <= end:
        last = min(day + timedelta(days=89), end)
        response = session.get(f"https://api.nbp.pl/api/cenyzlota/{day}/{last}/?format=json",
                               timeout=20, headers={"Accept": "application/json"})
        if response.status_code == 200:
            for item in response.json():
                gram = float(item["cena"])
                if gram > 0:
                    values.append((item["data"], gram * TROY_OUNCE_GRAMS))
        elif response.status_code != 404:
            response.raise_for_status()
        day = last + timedelta(days=1)
    return values


def fetch_silver_spot() -> tuple[str, float]:
    response = requests.get("https://api.gold-api.com/price/XAG", timeout=15)
    response.raise_for_status()
    item = response.json()
    price = float(item["price"])
    if price <= 0 or item.get("currency") != "USD":
        raise ValueError("Nieprawidłowe notowanie spot srebra.")
    publication = str(item.get("updatedAt") or "")[:10]
    day = date.fromisoformat(publication) if publication else date.today()
    if day > date.today():
        day = date.today()
    return day.isoformat(), price


def latest_metal_price(conn, asset_id: int, asset_symbol: str, as_of: str) -> dict | None:
    """Select NBP gold or silver spot ahead of the older futures proxy.

    An NBP observation can precede the as-of date by up to 10 days (holidays).
    Otherwise a historical fallback may be used with its actual source label.
    """
    period = as_of[:10]
    preferred = "nbp_gold" if asset_symbol.upper() == "GOLD" else "silver_spot"
    prefer = conn.execute("""SELECT price,price_date,source,currency FROM price_history
       WHERE asset_id=? AND price_date<=? AND source=?
       ORDER BY price_date DESC LIMIT 1""", (asset_id, period, preferred)).fetchone()
    official = (dict(prefer) if prefer and
        (date.fromisoformat(period) - date.fromisoformat(prefer['price_date'])).days <= 10 else None)
    row = conn.execute("""SELECT price,price_date,source,currency FROM price_history
        WHERE asset_id=? AND price_date<=? ORDER BY price_date DESC,
        CASE source WHEN 'manual' THEN 0 WHEN 'nbp_gold' THEN 1
           WHEN 'silver_spot' THEN 1 ELSE 2 END, updated_at DESC LIMIT 1""", (asset_id, period)).fetchone()
    stored = official or (dict(row) if row else None)
    # Use the reference stated on a metal trade only if newer than an external
    # market observation. No invented dealer premiums are added to valuation.
    tx = conn.execute("""SELECT substr(occurred_at,1,10) day,metal_spot_pln_per_oz price
        FROM transactions WHERE asset_id=? AND type IN ('BUY','SELL')
        AND substr(occurred_at,1,10)<=? AND metal_spot_pln_per_oz>0
        ORDER BY occurred_at DESC,id DESC LIMIT 1""", (asset_id, period)).fetchone()
    if tx and (stored is None or tx['day'] > stored['price_date']):
        return {"price": float(tx['price']), "price_date": tx['day'],
                "source": "transaction_reference", "currency": "PLN"}
    return stored


def ensure_metal_reference(conn, asset_id: int, asset_symbol: str, as_of: str) -> dict | None:
    """Try a small on-demand historical window for a new transaction.

    Network errors are intentionally nonfatal: the form will ask for a manual
    reference spot price, rather than silently substituting an unrelated day.
    """
    target = date.fromisoformat(as_of[:10])
    quote = latest_metal_price(conn, asset_id, asset_symbol, as_of)
    preferred = 'nbp_gold' if asset_symbol.upper() == 'GOLD' else 'silver_spot'
    if quote and quote['source'] == preferred and (target - date.fromisoformat(quote['price_date'])).days <= 10:
        return quote
    # Do not mistake a recent COMEX futures proxy for an available spot/NBP
    # reference. Try the preferred source first when entering a new trade.
    start = target - timedelta(days=15)
    try:
        if asset_symbol.upper() == 'GOLD':
            history = fetch_nbp_gold(start, target)
            source, currency = 'nbp_gold', 'PLN'
        elif target >= date.today() - timedelta(days=1):
            history = [fetch_silver_spot()]
            source, currency = 'silver_spot', 'USD'
        else:
            from .market import fetch_yahoo_history
            history = fetch_yahoo_history('SI=F', start, target, auto_adjust=False)
            source, currency = 'yahoo_futures_proxy', 'USD'
        conn.executemany("""INSERT INTO price_history(asset_id,price_date,price,currency,source)
           VALUES(?,?,?,?,?) ON CONFLICT(asset_id,price_date,source)
           DO UPDATE SET price=excluded.price,currency=excluded.currency,updated_at=CURRENT_TIMESTAMP""",
           [(asset_id, day, price, currency, source) for day, price in history if day <= as_of[:10]])
        conn.commit()
    except Exception:
        pass
    return latest_metal_price(conn, asset_id, asset_symbol, as_of)


def metal_reference_pln(conn, asset_id: int, asset_symbol: str, as_of: str, *, fetch: bool = False) -> dict | None:
    quote = (ensure_metal_reference(conn, asset_id, asset_symbol, as_of) if fetch else
             latest_metal_price(conn, asset_id, asset_symbol, as_of))
    if not quote:
        return None
    from .fx_quotes import fx_quote
    currency = quote['currency']
    fx = fx_quote(conn, currency, quote['price_date'], fetch_missing=fetch)
    if not fx or fx['stale']:
        return None
    return {"price_pln": float(quote['price']) * fx['rate'],
            "price_date": quote['price_date'], "source": quote['source'],
            "fx_date": fx['date']}
