from __future__ import annotations
from datetime import date, timedelta, datetime, timezone
from bisect import bisect_right
from .finance import external_cashflows
from .market import fetch_yahoo_history
from .db import get_setting, set_setting


def _all_external_flows_have_rates(conn, as_of: str | None = None) -> bool:
    sql = "SELECT COUNT(*) FROM transactions WHERE type IN ('DEPOSIT','WITHDRAWAL')"
    params = ()
    if as_of:
        sql += " AND substr(occurred_at,1,10)<=?"
        params = (as_of,)
    required = int(conn.execute(sql, params).fetchone()[0])
    return len(external_cashflows(conn, as_of)) == required


def sync_spy(conn, start_date: str | None = None, full_history: bool = False) -> int:
    # SPY follows the daily schedule, not the 6-hour crypto refresh cycle.
    last_sync = get_setting(conn, "last_spy_sync_at", "")
    if not full_history and not start_date and last_sync:
        try:
            stamp = datetime.fromisoformat(last_sync)
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - stamp).total_seconds() < 24 * 3600:
                return 0
        except ValueError:
            pass
    end = date.today()
    if start_date:
        start = date.fromisoformat(start_date)
    else:
        last = conn.execute("SELECT MAX(price_date) FROM benchmark_history WHERE symbol='SPY'").fetchone()
        first = conn.execute("SELECT MIN(price_date) FROM benchmark_history WHERE symbol='SPY'").fetchone()
        row = conn.execute("SELECT MIN(occurred_at) FROM transactions WHERE type IN ('DEPOSIT','WITHDRAWAL')").fetchone()
        flow_start = date.fromisoformat(row[0][:10]) if row and row[0] else end - timedelta(days=365)
        if full_history or not (last and last[0]):
            start = flow_start
        else:
            start = date.fromisoformat(last[0]) + timedelta(days=1)
            if first and first[0] and (date.fromisoformat(first[0]) - flow_start).days > 7:
                start = flow_start
    history = fetch_yahoo_history('SPY', start, end, auto_adjust=True)
    conn.executemany("""INSERT INTO benchmark_history(symbol,price_date,price,currency,source) VALUES('SPY',?,?,'USD','yahoo')
        ON CONFLICT(symbol,price_date,source) DO UPDATE SET price=excluded.price""", history)
    conn.commit()
    set_setting(conn, "last_spy_sync_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    return len(history)


def _market_maps(conn):
    prices = {r['price_date']: float(r['price']) for r in conn.execute("SELECT price_date,price FROM benchmark_history WHERE symbol='SPY' ORDER BY price_date")}
    fx = {r['rate_date']: float(r['rate_to_pln']) for r in conn.execute("SELECT rate_date,rate_to_pln FROM fx_history WHERE currency='USD' ORDER BY rate_date")}
    return prices, fx


def _carry_forward_map(source: dict[str, float], days: list[str]) -> dict[str, float | None]:
    """Map each sorted day to the latest source value on or before that day.

    This is linear in the number of dates, unlike repeatedly scanning all
    historical quotes for every snapshot day.
    """
    source_days = sorted(source)
    result: dict[str, float | None] = {}
    index = 0
    current = None
    for day in days:
        while index < len(source_days) and source_days[index] <= day:
            current = source[source_days[index]]
            index += 1
        result[day] = current
    return result


def benchmark_series(conn, start_date: str | None = None, end_date: str | None = None) -> list[dict]:
    """Daily benchmark value aligned to stored portfolio snapshots."""
    end = end_date or date.today().isoformat()
    snapshots = conn.execute("""SELECT snapshot_date,COALESCE(manual_total_value_pln,total_value_pln) value,
        valuation_complete,manual_total_value_pln FROM portfolio_snapshots WHERE snapshot_date<=? ORDER BY snapshot_date""", (end,)).fetchall()
    if start_date:
        snapshots = [r for r in snapshots if r['snapshot_date'] >= start_date]
    if not snapshots:
        return []
    first_day = start_date or snapshots[0]['snapshot_date']
    flows = external_cashflows(conn, end)
    if not flows or not _all_external_flows_have_rates(conn, end):
        return []
    flow_map = {}
    for day, value in flows:
        flow_map[day[:10]] = flow_map.get(day[:10], 0.0) + value
    prices, fx = _market_maps(conn)
    all_days = sorted(set(prices) | set(fx) | set(flow_map) | {r['snapshot_date'] for r in snapshots})
    price_by_day = _carry_forward_map(prices, all_days)
    fx_by_day = _carry_forward_map(fx, all_days)
    units = 0.0
    benchmark_by_day = {}
    for day in all_days:
        last_price = price_by_day[day]
        last_fx = fx_by_day[day]
        flow = flow_map.get(day, 0.0)
        if last_price and last_fx and abs(flow) > 1e-10:
            # XIRR convention: deposits are negative flows, withdrawals positive.
            if flow < 0:
                units += (-flow) / (last_price * last_fx)
            else:
                units = max(0.0, units - flow / (last_price * last_fx))
        if last_price and last_fx:
            benchmark_by_day[day] = units * last_price * last_fx
    result = []
    benchmark_days = sorted(benchmark_by_day)
    for row in snapshots:
        if row['snapshot_date'] < first_day or (not row['valuation_complete'] and row['manual_total_value_pln'] is None):
            continue
        bval = benchmark_by_day.get(row['snapshot_date'])
        if bval is None:
            prior_index = bisect_right(benchmark_days, row['snapshot_date']) - 1
            bval = benchmark_by_day[benchmark_days[prior_index]] if prior_index >= 0 else None
        if bval is not None and row['value'] is not None:
            result.append({'date': row['snapshot_date'], 'portfolio_value_pln': float(row['value']), 'spy_value_pln': float(bval)})
    return result


def benchmark_value(conn, as_of: str | None = None) -> dict:
    flows = external_cashflows(conn, as_of)
    if not flows or not _all_external_flows_have_rates(conn, as_of):
        return {"value_pln": None, "units": 0.0, "flows": len(flows), "missing_fx": True}
    target = as_of or date.today().isoformat()
    prices, fx = _market_maps(conn)
    flow_map = {}
    for day, value in flows:
        flow_map[day[:10]] = flow_map.get(day[:10], 0.0) + value
    all_days = sorted(d for d in (set(prices) | set(fx) | set(flow_map)) if d <= target)
    price_by_day = _carry_forward_map(prices, all_days)
    fx_by_day = _carry_forward_map(fx, all_days)
    units = 0.0
    last_price = last_fx = None
    for day in all_days:
        last_price = price_by_day[day]
        last_fx = fx_by_day[day]
        flow = flow_map.get(day, 0.0)
        if last_price and last_fx:
            if flow < 0:
                units += (-flow) / (last_price * last_fx)
            elif flow > 0:
                units = max(0.0, units - flow / (last_price * last_fx))
    if not last_price or not last_fx:
        # A quote/rate may exist before target even if there was no flow day
        # after it; find the latest available observation directly.
        price_days = sorted(d for d in prices if d <= target)
        fx_days = sorted(d for d in fx if d <= target)
        if not price_days or not fx_days:
            return {"value_pln": None, "units": units, "flows": len(flows)}
        last_price = prices[price_days[-1]]
        last_fx = fx[fx_days[-1]]
    return {"value_pln": units * last_price * last_fx, "units": units, "flows": len(flows)}
