"""Dated NBP references. Rates are saved and never projected from future days.

The real broker's conversion is separately editable on a transaction. Rates
from NBP are reference valuations, not an asserted execution rate.
"""
from __future__ import annotations

from datetime import date, timedelta
import sqlite3

from .operations import check_currency


def fx_quote(conn: sqlite3.Connection, currency: str, as_of: str,
             *, fetch_missing: bool = False, max_age_days: int = 10) -> dict | None:
    ccy = check_currency(currency)
    day = date.fromisoformat(as_of[:10])
    if day > date.today():
        raise ValueError("Nie można zastosować kursu z przyszłości.")
    if ccy == "PLN":
        return {"rate": 1.0, "date": as_of[:10], "source": "PLN", "stale": False}

    def read_quote():
        return conn.execute("""SELECT rate_to_pln,rate_date,source FROM fx_history
            WHERE currency=? AND rate_date<=? AND rate_to_pln>0
            ORDER BY rate_date DESC, CASE WHEN source='manual' THEN 0 ELSE 1 END,
                     updated_at DESC LIMIT 1""", (ccy, as_of[:10])).fetchone()

    row = read_quote()
    outdated = row is None or (day - date.fromisoformat(row['rate_date'])).days > max_age_days
    if fetch_missing and outdated:
        from .market import fetch_nbp_rates
        window_start = day - timedelta(days=max(14, max_age_days + 4))
        try:
            rates = fetch_nbp_rates(ccy, window_start, min(day, date.today()))
        except Exception:
            rates = []
        if rates:
            conn.executemany("""INSERT INTO fx_history(currency,rate_date,rate_to_pln,source)
                VALUES (?,?,?,'NBP') ON CONFLICT(currency,rate_date,source)
                DO UPDATE SET rate_to_pln=excluded.rate_to_pln,updated_at=CURRENT_TIMESTAMP""",
                [(ccy, day_text, rate) for day_text, rate in rates])
            conn.commit()
            row = read_quote()
    if row is None:
        return None
    age = (day - date.fromisoformat(row['rate_date'])).days
    return {"rate": float(row['rate_to_pln']), "date": row['rate_date'],
            "source": row['source'], "stale": age > max_age_days}
