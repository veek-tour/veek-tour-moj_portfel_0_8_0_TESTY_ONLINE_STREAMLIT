from __future__ import annotations

from datetime import date, timedelta
import math

from .db import add_transaction
from .ledger import calculate_ledger
from .snapshots import rebuild_snapshots


FREQUENCIES = {"daily": 365, "monthly": 12, "quarterly": 4, "annually": 1}


def start_staking(conn, *, asset_id: int, source_account_id: int, staking_account_id: int,
                  quantity: float, start_date: str, apy_pct: float,
                  planned_end_date: str | None = None, frequency: str = "daily", notes: str = "") -> int:
    if quantity <= 0 or apy_pct < 0:
        raise ValueError("Ilość musi być dodatnia, a APY nie może być ujemne.")
    if frequency not in FREQUENCIES:
        raise ValueError("Nieprawidłowa częstotliwość kapitalizacji.")
    if source_account_id == staking_account_id:
        raise ValueError("Rachunek źródłowy i stakingowy muszą być różne.")
    if planned_end_date and planned_end_date < start_date:
        raise ValueError("Planowany koniec stakingu nie może poprzedzać rozpoczęcia.")
    account = conn.execute("SELECT kind FROM accounts WHERE id=?", (staking_account_id,)).fetchone()
    if not account or account["kind"] != "staking":
        raise ValueError("Rachunek docelowy musi mieć typ Staking.")
    conn.execute("SAVEPOINT start_staking")
    try:
        tx_id = add_transaction(conn, {
            "occurred_at": start_date, "type": "ASSET_TRANSFER", "account_id": source_account_id,
            "to_account_id": staking_account_id, "asset_id": asset_id, "quantity": quantity,
            "notes": f"Blokada staking: {notes}".strip()
        })
        calculate_ledger(conn)
        cur = conn.execute("""INSERT INTO staking_plans(asset_id,source_account_id,staking_account_id,quantity,
            start_date,planned_end_date,apy_pct,compounding_frequency,lock_transaction_id,notes)
            VALUES(?,?,?,?,?,?,?,?,?,?)""", (asset_id, source_account_id, staking_account_id, quantity,
            start_date, planned_end_date, apy_pct, frequency, tx_id, notes))
        rebuild_snapshots(conn, start_date, commit=False)
        conn.execute("RELEASE SAVEPOINT start_staking")
        conn.commit()
        return int(cur.lastrowid)
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT start_staking")
        conn.execute("RELEASE SAVEPOINT start_staking")
        conn.rollback()
        raise


def end_staking(conn, plan_id: int, end_date: str, quantity: float | None = None) -> None:
    plan = conn.execute("SELECT * FROM staking_plans WHERE id=? AND active=1", (plan_id,)).fetchone()
    if not plan:
        raise ValueError("Nie znaleziono aktywnego planu stakingowego.")
    qty = float(plan["quantity"] if quantity is None else quantity)
    if qty <= 0 or qty > float(plan["quantity"]) + 1e-9:
        raise ValueError("Nieprawidłowa ilość odblokowania.")
    if end_date < plan["start_date"]:
        raise ValueError("Data odblokowania nie może poprzedzać rozpoczęcia stakingu.")
    conn.execute("SAVEPOINT end_staking")
    try:
        tx_id = add_transaction(conn, {
            "occurred_at": end_date, "type": "ASSET_TRANSFER", "account_id": plan["staking_account_id"],
            "to_account_id": plan["source_account_id"], "asset_id": plan["asset_id"], "quantity": qty,
            "notes": f"Odblokowanie stakingu #{plan_id}"
        })
        calculate_ledger(conn)
        remaining = float(plan["quantity"]) - qty
        if remaining <= 1e-9:
            conn.execute("UPDATE staking_plans SET quantity=0,active=0,actual_end_date=?,unlock_transaction_id=? WHERE id=?",
                         (end_date, tx_id, plan_id))
        else:
            conn.execute("UPDATE staking_plans SET quantity=?,actual_end_date=NULL,unlock_transaction_id=? WHERE id=?",
                         (remaining, tx_id, plan_id))
        rebuild_snapshots(conn, end_date, commit=False)
        conn.execute("RELEASE SAVEPOINT end_staking")
        conn.commit()
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT end_staking")
        conn.execute("RELEASE SAVEPOINT end_staking")
        conn.rollback()
        raise


def forecast_staking(plan: dict, as_of: date | None = None) -> dict:
    today = as_of or date.today()
    start = date.fromisoformat(plan["start_date"])
    target = date.fromisoformat(plan["planned_end_date"]) if plan.get("planned_end_date") else today + timedelta(days=365)
    days = max(0, (target - max(start, today)).days)
    n = FREQUENCIES.get(plan.get("compounding_frequency", "daily"), 365)
    apy = max(0.0, float(plan.get("apy_pct") or 0)) / 100.0
    # APY is an effective annual yield. Convert it to the selected period rate
    # so one full year projects exactly APY, regardless of capitalization frequency.
    period_rate = (1.0 + apy) ** (1.0 / n) - 1.0
    projected_quantity = float(plan["quantity"]) * ((1 + period_rate) ** (n * days / 365.0))
    return {"days_remaining": days, "projected_reward": projected_quantity - float(plan["quantity"]),
            "projected_quantity": projected_quantity, "target_date": target.isoformat()}


def bond_forecast(asset: dict, quantity: float, inflation_pct: float, start_date: str | None = None,
                  as_of: date | None = None) -> dict:
    today = as_of or date.today()
    maturity_raw = asset.get("maturity_date")
    maturity = date.fromisoformat(maturity_raw) if maturity_raw else today
    principal = max(0.0, float(quantity)) * float(asset.get("nominal_value") or 100)
    indexed = asset.get("bond_type") == "inflation"
    rate_pct = float(inflation_pct) + float(asset.get("inflation_margin_pct") or 0) if indexed else float(asset.get("annual_rate_pct") or 0)
    rate = max(-0.99, rate_pct / 100.0)
    origin = date.fromisoformat(start_date) if start_date else today
    elapsed = max(0, (min(today, maturity) - origin).days)
    remaining = max(0, (maturity - max(today, origin)).days)
    capitalizes = False  # 0.8: orientacyjna wycena liniowa, bez kapitalizacji
    def growth(days: int) -> float:
        years = days / 365.2425
        return max(0.0, 1 + rate * years)
    current_estimate = principal * growth(elapsed)
    maturity_estimate = principal * growth(max(0, (maturity - origin).days))
    return {"principal_pln": principal, "assumed_rate_pct": rate_pct, "current_estimate_pln": current_estimate,
            "maturity_estimate_pln": maturity_estimate, "estimated_interest_pln": maturity_estimate - principal,
            "days_to_maturity": (maturity - today).days, "maturity_date": maturity.isoformat(),
            "capitalizes": capitalizes, "overdue": maturity < today}
