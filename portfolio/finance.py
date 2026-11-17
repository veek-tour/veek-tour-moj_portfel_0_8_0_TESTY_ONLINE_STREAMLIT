from __future__ import annotations
from datetime import date

from .ledger import latest_fx


def xirr(cashflows: list[tuple[str, float]]) -> float | None:
    flows = [(date.fromisoformat(d[:10]), float(v)) for d, v in cashflows if abs(float(v)) > 1e-10]
    if not flows or not any(v < 0 for _, v in flows) or not any(v > 0 for _, v in flows):
        return None
    start = min(d for d, _ in flows)
    def npv(rate: float) -> float:
        return sum(value / ((1 + rate) ** ((day - start).days / 365.0)) for day, value in flows)
    low, high = -0.9999, 10.0
    f_low, f_high = npv(low), npv(high)
    expansions = 0
    while f_low * f_high > 0 and expansions < 20:
        high *= 2
        f_high = npv(high)
        expansions += 1
    if f_low * f_high > 0:
        return None
    for _ in range(200):
        mid = (low + high) / 2
        f_mid = npv(mid)
        if abs(f_mid) < 1e-7:
            return mid
        if f_low * f_mid <= 0:
            high, f_high = mid, f_mid
        else:
            low, f_low = mid, f_mid
    return (low + high) / 2


def external_cashflows(conn, as_of: str | None = None) -> list[tuple[str, float]]:
    flows = []
    sql = "SELECT * FROM transactions WHERE type IN ('DEPOSIT','WITHDRAWAL')"
    params = ()
    if as_of:
        sql += " AND substr(occurred_at,1,10)<=?"
        params = (as_of,)
    txs = conn.execute(sql + " ORDER BY occurred_at,id", params).fetchall()
    for tx in txs:
        amount = float(tx['cash_amount'] or 0)
        rate = tx['cash_fx_to_pln']
        currency = (tx['cash_currency'] or 'PLN').upper()
        if rate is None or float(rate) <= 0:
            rate = latest_fx(conn, currency, tx['occurred_at'][:10])
        if rate is None or float(rate) <= 0:
            continue
        sign = -1 if tx['type'] == 'DEPOSIT' else 1
        flows.append((tx['occurred_at'], sign * amount * float(rate)))
    return flows
