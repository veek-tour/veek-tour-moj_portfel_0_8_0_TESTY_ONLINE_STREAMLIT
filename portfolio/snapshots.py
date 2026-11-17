from __future__ import annotations

from datetime import date, timedelta
from .ledger import value_portfolio


def save_snapshot(conn, snapshot_date: str | None = None, commit: bool = True) -> dict:
    day = snapshot_date or date.today().isoformat()
    value = value_portfolio(conn, day)
    conn.execute("""INSERT INTO portfolio_snapshots(snapshot_date,assets_value_pln,cash_value_pln,total_value_pln,
        invested_cost_pln,realized_pln,income_pln,valuation_complete,notes) VALUES(?,?,?,?,?,?,?,?,?)
        ON CONFLICT(snapshot_date) DO UPDATE SET assets_value_pln=excluded.assets_value_pln,
        cash_value_pln=excluded.cash_value_pln,total_value_pln=excluded.total_value_pln,
        invested_cost_pln=excluded.invested_cost_pln,realized_pln=excluded.realized_pln,
        income_pln=excluded.income_pln,valuation_complete=excluded.valuation_complete,notes=excluded.notes""",
        (day, value["assets_value_pln"], value["cash_value_pln"], value["partial_total_value_pln"],
         value["cost_total_pln"], value["realized_pln"], value["income_pln"], int(value["valuation_complete"]), ""))
    if commit:
        conn.commit()
    return value


def rebuild_snapshots(conn, start: str | None = None, end: str | None = None, commit: bool = True) -> int:
    """Recalculate stored snapshots. Price corrections and transaction edits are reflected."""
    end_day = date.fromisoformat(end) if end else date.today()
    if start:
        start_day = date.fromisoformat(start)
    else:
        row = conn.execute("SELECT MIN(occurred_at) FROM transactions").fetchone()
        start_day = date.fromisoformat(row[0][:10]) if row and row[0] else end_day
    count = 0
    day = start_day
    while day <= end_day:
        save_snapshot(conn, day.isoformat(), commit=False)
        count += 1
        day += timedelta(days=1)
    if commit:
        conn.commit()
    return count


def fill_missing_snapshots(conn, end: str | None = None) -> int:
    """Create missing daily snapshots without overwriting prior manual review records."""
    end_day = date.fromisoformat(end) if end else date.today()
    row = conn.execute("SELECT MIN(occurred_at) FROM transactions").fetchone()
    if not row or not row[0]:
        save_snapshot(conn, end_day.isoformat(), commit=False)
        conn.commit()
        return 1
    start_day = date.fromisoformat(row[0][:10])
    existing = {r["snapshot_date"]: bool(r["valuation_complete"]) for r in conn.execute(
        "SELECT snapshot_date,valuation_complete FROM portfolio_snapshots").fetchall()}
    count = 0
    day = start_day
    while day <= end_day:
        iso = day.isoformat()
        # Revisit incomplete snapshots: a later successful market-data sync may
        # have supplied the missing historical quote or FX rate.
        if iso not in existing or not existing[iso]:
            save_snapshot(conn, iso, commit=False)
            count += 1
        day += timedelta(days=1)
    conn.commit()
    return count


def set_snapshot_override(conn, snapshot_date: str, value_pln: float | None, notes: str = "") -> None:
    if value_pln is not None and value_pln < 0:
        raise ValueError("Wartość migawki nie może być ujemna.")
    conn.execute("UPDATE portfolio_snapshots SET manual_total_value_pln=?, manual_notes=? WHERE snapshot_date=?",
                 (value_pln, notes, snapshot_date))
    conn.commit()
