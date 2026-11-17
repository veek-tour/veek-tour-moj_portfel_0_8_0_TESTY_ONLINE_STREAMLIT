from __future__ import annotations

from datetime import date, timedelta, datetime, timezone
from typing import Any
import logging
import time
from math import isfinite

import requests

from .db import rows, set_setting, get_setting

log = logging.getLogger(__name__)
NBP_CURRENCIES = {"USD", "EUR", "GBP", "CHF", "CZK", "NOK", "SEK", "DKK", "JPY"}


def fetch_nbp_rates(currency: str, start: date, end: date) -> list[tuple[str, float]]:
    currency = currency.upper()
    if currency not in NBP_CURRENCIES:
        return []
    result: list[tuple[str, float]] = []
    cursor = start
    session = requests.Session()
    while cursor <= end:
        chunk_end = min(end, cursor + timedelta(days=89))
        url = f"https://api.nbp.pl/api/exchangerates/rates/A/{currency}/{cursor.isoformat()}/{chunk_end.isoformat()}/?format=json"
        response = session.get(url, timeout=20, headers={"Accept": "application/json"})
        if response.status_code == 200:
            data = response.json()
            result.extend((item["effectiveDate"], float(item["mid"])) for item in data.get("rates", []))
        elif response.status_code not in (404,):
            response.raise_for_status()
        cursor = chunk_end + timedelta(days=1)
    return result


def _extract_close_series(data):
    if data is None or data.empty:
        return []
    close = data["Close"]
    if hasattr(close, "columns"):
        close = close.iloc[:, 0]
    results = []
    for idx, value in close.dropna().items():
        try:
            number = float(value)
            if isfinite(number) and number > 0:
                stamp = idx.date().isoformat() if hasattr(idx, "date") else str(idx)[:10]
                results.append((stamp, number))
        except (TypeError, ValueError):
            continue
    return results


def fetch_yahoo_history(symbol: str, start: date, end: date, auto_adjust: bool = False) -> list[tuple[str, float]]:
    """Download daily closes, with a Ticker.history fallback for transient download API failures."""
    try:
        import yfinance as yf
    except Exception as exc:
        raise RuntimeError("Brak biblioteki yfinance. Zainstaluj zależności z requirements.txt.") from exc
    last_error = None
    for attempt in range(2):
        try:
            data = yf.download(symbol, start=start.isoformat(), end=(end + timedelta(days=1)).isoformat(),
                               auto_adjust=auto_adjust, progress=False, threads=False, timeout=20)
            values = _extract_close_series(data)
            if values:
                return values
        except Exception as exc:
            last_error = exc
        try:
            ticker = yf.Ticker(symbol)
            data = ticker.history(start=start.isoformat(), end=(end + timedelta(days=1)).isoformat(),
                                  auto_adjust=auto_adjust, raise_errors=True)
            values = _extract_close_series(data)
            if values:
                return values
        except Exception as exc:
            last_error = exc
        if attempt == 0:
            time.sleep(1.0)
    if last_error:
        raise RuntimeError(f"Yahoo Finance nie zwrócił danych: {last_error}") from last_error
    return []


def fetch_yahoo_intraday(symbol: str) -> tuple[str, float] | None:
    """Fetch the latest available hourly crypto quote; stored as today's refreshed mark."""
    try:
        import yfinance as yf
        data = yf.download(symbol, period="1d", interval="1h", auto_adjust=False, progress=False,
                           threads=False, prepost=True, timeout=20)
        values = _extract_close_series(data)
        if values:
            return date.today().isoformat(), values[-1][1]
        data = yf.Ticker(symbol).history(period="1d", interval="1h", auto_adjust=False, prepost=True)
        values = _extract_close_series(data)
        if values:
            return date.today().isoformat(), values[-1][1]
    except Exception as exc:
        log.warning("Intraday Yahoo Finance %s: %s", symbol, exc)
    return None



BINANCE_PREFIX = "BINANCE:"


def _yahoo_crypto_symbol(binance_pair: str) -> str | None:
    """A different provider for a confirmed USD-quoted crypto pair.

    A fallback must retain the same base asset and USD quote currency; no
    automatic crypto conversions are attempted.
    """
    pair = str(binance_pair).strip().upper()
    if pair.endswith('USDT') and pair[:-4].isalnum():
        return pair[:-4] + '-USD'
    if pair.endswith('USD') and pair[:-3].isalnum():
        return pair[:-3] + '-USD'
    return None


BINANCE_MARKET_DATA_BASES = ("https://data-api.binance.vision", "https://api.binance.com")


def fetch_binance_history(symbol: str, start: date, end: date) -> list[tuple[str, float]]:
    """Fetch daily spot closes from Binance public market-data API (no API key)."""
    pair = symbol.strip().upper()
    if not pair or not pair.isalnum():
        raise ValueError(f"Nieprawidłowa para Binance: {symbol}")
    if start > end:
        return []
    cursor_ms = int(datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc).timestamp() * 1000)
    end_ms = int(datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc).timestamp() * 1000) - 1
    output: list[tuple[str, float]] = []
    session = requests.Session()
    while cursor_ms <= end_ms:
        params = {"symbol": pair, "interval": "1d", "startTime": cursor_ms, "endTime": end_ms, "limit": 1000}
        response_data = None
        last_error = None
        for base in BINANCE_MARKET_DATA_BASES:
            try:
                response = session.get(f"{base}/api/v3/klines", params=params, timeout=20)
                response.raise_for_status()
                candidate = response.json()
                if not isinstance(candidate, list):
                    raise RuntimeError(f"Nieoczekiwany format danych Binance dla {pair}.")
                response_data = candidate
                break
            except Exception as exc:
                last_error = exc
        if response_data is None:
            raise RuntimeError(f"Binance nie zwrócił historii dla {pair}: {last_error}") from last_error
        if not response_data:
            break
        for candle in response_data:
            try:
                day = datetime.fromtimestamp(int(candle[0]) / 1000, tz=timezone.utc).date().isoformat()
                close = float(candle[4])
                if start.isoformat() <= day <= end.isoformat() and close > 0:
                    output.append((day, close))
            except (IndexError, TypeError, ValueError):
                continue
        last_open = int(response_data[-1][0])
        next_cursor = last_open + 86_400_000
        if next_cursor <= cursor_ms:
            break
        cursor_ms = next_cursor
        if len(response_data) < 1000:
            break
    return sorted(set(output), key=lambda item: item[0])


def fetch_binance_latest(symbol: str) -> tuple[str, float] | None:
    """Fetch current spot price for a Binance pair using a public endpoint."""
    pair = symbol.strip().upper()
    last_error = None
    session = requests.Session()
    for base in BINANCE_MARKET_DATA_BASES:
        try:
            response = session.get(f"{base}/api/v3/ticker/price", params={"symbol": pair}, timeout=15)
            response.raise_for_status()
            payload = response.json()
            price = float(payload["price"])
            if price > 0:
                return date.today().isoformat(), price
            raise ValueError("Binance zwrócił cenę równą zero lub ujemną.")
        except Exception as exc:
            last_error = exc
    log.warning("Binance latest %s: %s", pair, last_error)
    return None


def _recently_updated(conn, asset_id: int, threshold_hours: int, source: str = "yahoo") -> bool:
    row = conn.execute("SELECT MAX(updated_at) FROM price_history WHERE asset_id=? AND source=?", (asset_id, source)).fetchone()
    if not row or not row[0]:
        return False
    try:
        # SQLite CURRENT_TIMESTAMP is UTC and has no timezone suffix.
        updated = datetime.fromisoformat(str(row[0]))
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        return (now - updated).total_seconds() < threshold_hours * 3600
    except ValueError:
        return False


def _setting_updated_within(conn, key: str, threshold_hours: int) -> bool:
    raw = get_setting(conn, key, "")
    if not raw:
        return False
    try:
        updated = datetime.fromisoformat(raw)
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - updated).total_seconds() < threshold_hours * 3600
    except ValueError:
        return False


def sync_market_data(conn, days_back: int = 10, full_history: bool = False, force: bool = False) -> dict[str, Any]:
    """Refresh crypto marks every 6h and other quoted assets daily. Never erase a good quote on failure."""
    today = date.today()
    start_default = today - timedelta(days=days_back)
    assets = rows(conn, "SELECT * FROM assets WHERE active=1 AND category<>'Obligacje' AND price_mode='auto' AND COALESCE(price_symbol,'')<>''")
    results = {"prices_saved": 0, "fx_saved": 0, "errors": [], "assets_checked": 0,
               "assets_skipped_schedule": 0, "crypto_intraday_saved": 0,
               "fallbacks_used": []}
    for asset in assets:
        configured_symbol = asset["price_symbol"].strip()
        is_binance = configured_symbol.upper().startswith(BINANCE_PREFIX)
        symbol = configured_symbol[len(BINANCE_PREFIX):] if is_binance else configured_symbol
        source = "binance" if is_binance else "yahoo"
        asset_id = int(asset["id"])
        crypto = asset["category"] == "Kryptowaluty"
        cadence = 6 if crypto else 24
        results["assets_checked"] += 1
        if not full_history and not force and _recently_updated(conn, asset_id, cadence, source):
            results["assets_skipped_schedule"] += 1
            continue
        try:
            tx = conn.execute("SELECT MIN(occurred_at) FROM transactions WHERE asset_id=?", (asset_id,)).fetchone()
            tx_start = date.fromisoformat(tx[0][:10]) if tx and tx[0] else None
            last = conn.execute("SELECT MAX(price_date) FROM price_history WHERE asset_id=? AND source=?", (asset_id, source)).fetchone()
            first = conn.execute("SELECT MIN(price_date) FROM price_history WHERE asset_id=? AND source=?", (asset_id, source)).fetchone()
            if full_history:
                start = tx_start or start_default
            elif force:
                start = start_default
            elif last and last[0]:
                start = date.fromisoformat(last[0]) + timedelta(days=1)
                if tx_start and first and first[0] and (date.fromisoformat(first[0]) - tx_start).days > 7:
                    start = tx_start
            else:
                start = tx_start or start_default

            # Daily history fills gaps. Crypto's current mark is then refreshed from the
            # latest hourly candle, overwriting only today's Yahoo row.
            history_end = today - timedelta(days=1) if crypto else today
            history_source = source
            history = []
            if start <= history_end:
                try:
                    history = (fetch_binance_history(symbol,start,history_end) if is_binance else
                               fetch_yahoo_history(symbol,start,history_end,auto_adjust=False))
                except Exception as primary_error:
                    alt = _yahoo_crypto_symbol(symbol) if is_binance and crypto else None
                    if not alt:
                        raise
                    try:
                        history = fetch_yahoo_history(alt,start,history_end,auto_adjust=False)
                    except Exception as fallback_error:
                        raise RuntimeError(f'Binance: {primary_error}; Yahoo: {fallback_error}') from fallback_error
                    history_source = 'yahoo_crypto_fallback'
                    results['fallbacks_used'].append(f"{asset['symbol']}: Yahoo USD (historia)")
                if not history and is_binance and crypto:
                    alt = _yahoo_crypto_symbol(symbol)
                    if alt:
                        history = fetch_yahoo_history(alt,start,history_end,auto_adjust=False)
                        if history:
                            history_source = 'yahoo_crypto_fallback'
                            results['fallbacks_used'].append(f"{asset['symbol']}: Yahoo USD (historia)")
            if history:
                valid = [(asset_id, day, float(price), asset['currency'], history_source)
                         for day,price in history if (start.isoformat() <= str(day) <= history_end.isoformat()
                         and isfinite(float(price)) and float(price) > 0)]
                conn.executemany("""INSERT INTO price_history(asset_id,price_date,price,currency,source) VALUES(?,?,?,?,?)
                    ON CONFLICT(asset_id,price_date,source) DO UPDATE SET price=excluded.price,currency=excluded.currency,updated_at=CURRENT_TIMESTAMP""", valid)
                results['prices_saved'] += len(valid)

            if crypto:
                intraday = fetch_binance_latest(symbol) if is_binance else fetch_yahoo_intraday(symbol)
                live_source = source
                if not intraday and is_binance:
                    alt = _yahoo_crypto_symbol(symbol)
                    if alt:
                        intraday = fetch_yahoo_intraday(alt)
                        if intraday:
                            live_source = 'yahoo_crypto_fallback'
                            results['fallbacks_used'].append(f"{asset['symbol']}: Yahoo USD (cena dzisiejsza)")
                if intraday:
                    day, price = intraday
                    if day <= today.isoformat() and isfinite(float(price)) and float(price) > 0:
                        conn.execute("""INSERT INTO price_history(asset_id,price_date,price,currency,source) VALUES(?,?,?,?,?)
                            ON CONFLICT(asset_id,price_date,source) DO UPDATE SET price=excluded.price,currency=excluded.currency,updated_at=CURRENT_TIMESTAMP""",
                            (asset_id,day,float(price),asset['currency'],live_source))
                        results['prices_saved'] += 1
                        results['crypto_intraday_saved'] += 1
                elif not is_binance:
                    fallback = fetch_yahoo_history(symbol,today,today,auto_adjust=False)
                    if fallback:
                        day, price = fallback[-1]
                        conn.execute("""INSERT INTO price_history(asset_id,price_date,price,currency,source) VALUES(?,?,?,?, 'yahoo')
                            ON CONFLICT(asset_id,price_date,source) DO UPDATE SET price=excluded.price,currency=excluded.currency,updated_at=CURRENT_TIMESTAMP""",
                            (asset_id,day,price,asset['currency']))
                        results['prices_saved'] += 1
        except Exception as exc:
            msg = f"{asset['symbol']} ({configured_symbol}): {exc}"
            results["errors"].append(msg)
            log.warning(msg)

    currencies = {"USD"}  # USD is also needed for the SPY benchmark.
    for asset in rows(conn, "SELECT DISTINCT currency FROM assets WHERE active=1"):
        currencies.add(str(asset["currency"]).upper())
    for tx in rows(conn, "SELECT DISTINCT cash_currency,fee_currency,to_cash_currency,price_currency FROM transactions"):
        for field in ("cash_currency", "fee_currency", "to_cash_currency", "price_currency"):
            if tx[field]: currencies.add(str(tx[field]).upper())
    tx_min = conn.execute("SELECT MIN(occurred_at) FROM transactions").fetchone()
    history_start = date.fromisoformat(tx_min[0][:10]) if tx_min and tx_min[0] else start_default
    for currency in sorted(currencies - {"PLN", "USDT", "USDC"}):
        schedule_key = f"last_fx_sync_{currency}"
        # If we checked before NBP published today's table, retry after 12:15
        # Warsaw time instead of waiting another 24 hours for the next run.
        published_window = False
        try:
            from zoneinfo import ZoneInfo
            local_now = datetime.now(ZoneInfo('Europe/Warsaw'))
            last_nbp = conn.execute("SELECT MAX(rate_date) FROM fx_history WHERE currency=? AND source='NBP'", (currency,)).fetchone()[0]
            published_window = (currency in {'EUR','USD'} and local_now.weekday() < 5
                and (local_now.hour, local_now.minute) >= (12,15)
                and (not last_nbp or last_nbp < local_now.date().isoformat()))
        except Exception:
            pass
        if not full_history and not force and _setting_updated_within(conn, schedule_key, 24) and not published_window:
            continue
        try:
            last = conn.execute("SELECT MAX(rate_date) FROM fx_history WHERE currency=? AND source='NBP'", (currency,)).fetchone()
            first = conn.execute("SELECT MIN(rate_date) FROM fx_history WHERE currency=? AND source='NBP'", (currency,)).fetchone()
            if full_history:
                fx_start = history_start
            elif force:
                fx_start = start_default
            elif last and last[0]:
                fx_start = date.fromisoformat(last[0]) + timedelta(days=1)
                if tx_min and tx_min[0] and first and first[0] and (date.fromisoformat(first[0]) - date.fromisoformat(tx_min[0][:10])).days > 7:
                    fx_start = date.fromisoformat(tx_min[0][:10])
            else:
                fx_start = history_start
            rates = fetch_nbp_rates(currency, fx_start, today) if fx_start <= today else []
            conn.executemany("""INSERT INTO fx_history(currency,rate_date,rate_to_pln,source) VALUES(?,?,?,'NBP')
                ON CONFLICT(currency,rate_date,source) DO UPDATE SET rate_to_pln=excluded.rate_to_pln,updated_at=CURRENT_TIMESTAMP""",
                [(currency, day, rate) for day, rate in rates])
            results["fx_saved"] += len(rates)
            set_setting(conn, schedule_key, datetime.now(timezone.utc).isoformat(timespec="seconds"))
        except Exception as exc:
            results["errors"].append(f"Kurs {currency} (NBP): {exc}")

    # Reference values for physical bullion (distinct from the futures series).
    # Only download these when the metal is actually owned/has transaction history;
    # this also makes fresh empty installations fast and avoids needless requests.
    from .metals import fetch_nbp_gold, fetch_silver_spot
    for symbol, source in (("GOLD", "nbp_gold"), ("SILVER", "silver_spot")):
        target = conn.execute("SELECT id FROM assets WHERE symbol=? AND category='Metale szlachetne'", (symbol,)).fetchone()
        if not target:
            continue
        asset_id = int(target[0])
        first_tx = conn.execute("SELECT MIN(occurred_at) FROM transactions WHERE asset_id=?", (asset_id,)).fetchone()
        if not first_tx or not first_tx[0]:
            continue
        if not full_history and not force and _recently_updated(conn, asset_id, 24, source):
            continue
        try:
            if symbol == "GOLD":
                last = conn.execute("SELECT MAX(price_date) FROM price_history WHERE asset_id=? AND source='nbp_gold'", (asset_id,)).fetchone()[0]
                if full_history:
                    start = date.fromisoformat(first_tx[0][:10])
                elif last:
                    start = date.fromisoformat(last) + timedelta(days=1)
                else:
                    start = min(start_default, date.fromisoformat(first_tx[0][:10]))
                values = fetch_nbp_gold(start, today) if start <= today else []
                data = [(asset_id, day, price, 'PLN', source) for day, price in values]
            else:
                day, price = fetch_silver_spot()
                data = [(asset_id, day, price, 'USD', source)]
            if data:
                conn.executemany("""INSERT INTO price_history(asset_id,price_date,price,currency,source)
                    VALUES(?,?,?,?,?) ON CONFLICT(asset_id,price_date,source)
                    DO UPDATE SET price=excluded.price,currency=excluded.currency,updated_at=CURRENT_TIMESTAMP""", data)
                results["prices_saved"] += len(data)
        except Exception as exc:
            results["errors"].append(f"{symbol} (referencyjna cena metalu): {exc}")

    conn.commit()
    set_setting(conn, "last_sync_date", datetime.now().isoformat(timespec="minutes"))
    return results

