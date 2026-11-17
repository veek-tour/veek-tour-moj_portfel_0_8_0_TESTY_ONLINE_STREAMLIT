from __future__ import annotations

import sqlite3
from datetime import date
from typing import Any

EPS = 1e-9


def ensure_legacy_bond_tranches(conn: sqlite3.Connection) -> int:
    """Create one tranche for each legacy bond BUY that predates tranche tracking.

    Terms are copied from the old asset-level fields. For inflation-linked bonds,
    the old global assumption is copied only if the user had confirmed it.
    """
    setting = conn.execute("SELECT value FROM settings WHERE key='current_inflation_pct'").fetchone()
    confirmed = conn.execute("SELECT value FROM settings WHERE key='current_inflation_confirmed'").fetchone()
    assumed = None
    if setting and confirmed and str(confirmed[0]) == "1":
        try:
            assumed = float(setting[0])
        except (TypeError, ValueError):
            assumed = None

    legacy = conn.execute(
        """SELECT t.id, a.bond_type, a.nominal_value, a.maturity_date,
                  a.annual_rate_pct, a.inflation_margin_pct, a.bond_compounding
           FROM transactions t
           JOIN assets a ON a.id=t.asset_id
           LEFT JOIN bond_tranches bt ON bt.purchase_transaction_id=t.id
           WHERE t.type='BUY' AND a.category='Obligacje' AND bt.id IS NULL
           ORDER BY t.occurred_at,t.id""").fetchall()
    for row in legacy:
        conn.execute(
            """INSERT INTO bond_tranches(
                   purchase_transaction_id,bond_type,nominal_value,maturity_date,
                   annual_rate_pct,inflation_margin_pct,assumed_inflation_pct,compounding
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (row["id"], row["bond_type"] or "fixed", float(row["nominal_value"] or 100),
             row["maturity_date"], row["annual_rate_pct"], row["inflation_margin_pct"],
             assumed if (row["bond_type"] or "fixed") == "inflation" else None,
             int(row["bond_compounding"] if row["bond_compounding"] is not None else 1)))
    return len(legacy)


def upsert_bond_tranche(conn: sqlite3.Connection, transaction_id: int, terms: dict[str, Any]) -> None:
    """Create/update the terms attached to one bond purchase transaction."""
    required = ("bond_type", "nominal_value", "maturity_date")
    missing = [key for key in required if terms.get(key) is None]
    if missing:
        raise ValueError("Brak parametrów transzy obligacji: " + ", ".join(missing))
    bond_type = str(terms["bond_type"])
    if bond_type not in {"fixed", "inflation"}:
        raise ValueError("Nieprawidłowy rodzaj oprocentowania transzy.")
    nominal = float(terms["nominal_value"])
    if nominal <= 0:
        raise ValueError("Wartość nominalna transzy musi być większa od zera.")
    maturity_date = date.fromisoformat(str(terms["maturity_date"])[:10])
    maturity = maturity_date.isoformat()
    tx = conn.execute(
        """SELECT t.type,t.quantity,t.occurred_at,a.category
           FROM transactions t JOIN assets a ON a.id=t.asset_id WHERE t.id=?""",
        (transaction_id,)).fetchone()
    if not tx or tx["type"] != "BUY" or tx["category"] != "Obligacje":
        raise ValueError("Transza musi być przypisana do zakupu obligacji.")
    purchase = date.fromisoformat(str(tx["occurred_at"])[:10])
    if maturity_date <= purchase:
        raise ValueError("Termin wykupu musi przypadać po dacie zakupu.")

    annual_rate = terms.get("annual_rate_pct") if bond_type == "fixed" else None
    margin = terms.get("inflation_margin_pct") if bond_type == "inflation" else None
    assumed = terms.get("assumed_inflation_pct") if bond_type == "inflation" else None
    if bond_type == "fixed" and annual_rate is None:
        raise ValueError("Podaj oprocentowanie stałe dla transzy.")
    if bond_type == "inflation" and margin is None:
        raise ValueError("Podaj marżę ponad inflację dla transzy.")
    if assumed is not None:
        assumed = float(assumed)
        if not -5 <= assumed <= 100:
            raise ValueError("Przyjęta stopa inflacji musi mieścić się w zakresie od -5% do 100%.")

    conn.execute(
        """INSERT INTO bond_tranches(
               purchase_transaction_id,bond_type,nominal_value,maturity_date,
               annual_rate_pct,inflation_margin_pct,assumed_inflation_pct,compounding
           ) VALUES(?,?,?,?,?,?,?,?)
           ON CONFLICT(purchase_transaction_id) DO UPDATE SET
               bond_type=excluded.bond_type, nominal_value=excluded.nominal_value,
               maturity_date=excluded.maturity_date, annual_rate_pct=excluded.annual_rate_pct,
               inflation_margin_pct=excluded.inflation_margin_pct,
               assumed_inflation_pct=excluded.assumed_inflation_pct,
               compounding=excluded.compounding""",
        (transaction_id, bond_type, nominal, maturity, annual_rate, margin,
         assumed, 0))


def list_bond_tranches(conn: sqlite3.Connection, asset_id: int | None = None,
                       as_of: str | None = None) -> list[dict[str, Any]]:
    """Return bond tranches with remaining quantities after sales.

    New sales can target one purchase tranche explicitly. Older sales without a
    target are allocated pro rata over the then-open tranches; FIFO is not used.
    """
    where = ["a.category='Obligacje'"]
    params: list[Any] = []
    if asset_id is not None:
        where.append("a.id=?")
        params.append(int(asset_id))
    if as_of:
        where.append("substr(t.occurred_at,1,10)<=?")
        params.append(str(as_of)[:10])

    sql = f"""SELECT bt.*,t.id AS purchase_transaction_id,t.occurred_at AS purchase_date,
                     t.quantity AS purchased_quantity,t.account_id,
                     a.id AS asset_id,a.symbol,a.name,a.currency,a.unit
              FROM bond_tranches bt
              JOIN transactions t ON t.id=bt.purchase_transaction_id
              JOIN assets a ON a.id=t.asset_id
              WHERE {' AND '.join(where)} AND t.type='BUY'
              ORDER BY t.occurred_at,t.id"""
    tranches = [dict(row) for row in conn.execute(sql, params).fetchall()]
    if not tranches:
        return []

    by_id = {int(row["purchase_transaction_id"]): row for row in tranches}
    asset_ids = sorted({int(row["asset_id"]) for row in tranches})
    for aid in asset_ids:
        event_sql = """SELECT id,occurred_at,type,quantity,bond_tranche_purchase_id FROM transactions
                       WHERE asset_id=? AND type IN ('BUY','SELL')"""
        event_params: list[Any] = [aid]
        if as_of:
            event_sql += " AND substr(occurred_at,1,10)<=?"
            event_params.append(str(as_of)[:10])
        event_sql += " ORDER BY occurred_at,id"
        open_rows: list[dict[str, Any]] = []
        for event in conn.execute(event_sql, event_params).fetchall():
            tx_id = int(event["id"])
            qty = max(0.0, float(event["quantity"] or 0))
            if event["type"] == "BUY":
                tranche = by_id.get(tx_id)
                if tranche is not None:
                    tranche["remaining_quantity"] = qty
                    open_rows.append(tranche)
            elif qty > EPS:
                selected_tranche_id = event["bond_tranche_purchase_id"]
                if selected_tranche_id is not None:
                    selected = next((item for item in open_rows
                                     if int(item["purchase_transaction_id"]) == int(selected_tranche_id)), None)
                    if selected is None:
                        raise ValueError(f"Sprzedaż obligacji #{tx_id} wskazuje transzę, która nie jest dostępna w tym dniu.")
                    available = float(selected.get("remaining_quantity", 0.0))
                    if qty > available + EPS:
                        raise ValueError(f"Sprzedaż obligacji #{tx_id} przekracza ilość dostępną w wybranej transzy.")
                    selected["remaining_quantity"] = max(0.0, available - qty)
                else:
                    # Legacy sales did not identify a tranche. Split them pro rata.
                    total_open = sum(float(item.get("remaining_quantity", 0.0)) for item in open_rows)
                    if total_open <= EPS:
                        continue
                    if qty > total_open + EPS:
                        raise ValueError(f"Sprzedaż obligacji #{tx_id} przekracza ilość otwartych transz.")
                    reduction = qty / total_open
                    for item in open_rows:
                        item["remaining_quantity"] = max(
                            0.0, float(item.get("remaining_quantity", 0.0)) * (1.0 - reduction))
    return [row for row in tranches if float(row.get("remaining_quantity", 0.0)) > EPS]


def estimate_tranche(tranche: dict[str, Any], as_of: date | str | None = None) -> dict[str, Any]:
    """Estimate one tranche; the current assumption is applied to its whole life."""
    today = as_of if isinstance(as_of, date) else date.fromisoformat(str(as_of)[:10]) if as_of else date.today()
    purchase = date.fromisoformat(str(tranche["purchase_date"])[:10])
    maturity = date.fromisoformat(str(tranche["maturity_date"])[:10])
    quantity = max(0.0, float(tranche.get("remaining_quantity", tranche.get("purchased_quantity", 0)) or 0))
    nominal = float(tranche.get("nominal_value") or 0)
    bond_type = str(tranche.get("bond_type") or "")
    if bond_type == "inflation":
        assumed = tranche.get("assumed_inflation_pct")
        if assumed is None:
            raise ValueError("Brak przyjętej stopy inflacji dla transzy.")
        rate_pct = float(assumed) + float(tranche.get("inflation_margin_pct") or 0)
    elif bond_type == "fixed":
        if tranche.get("annual_rate_pct") is None:
            raise ValueError("Brak oprocentowania stałego dla transzy.")
        rate_pct = float(tranche["annual_rate_pct"])
    else:
        raise ValueError("Nieprawidłowy rodzaj oprocentowania transzy.")
    rate = max(-0.99, rate_pct / 100.0)

    def growth(end: date) -> float:
        effective_end = min(end, maturity)
        days = max(0, (effective_end - purchase).days)
        years = days / 365.2425
        # User-selected indicative annual rate, simple interest only.
        return max(0.0, 1.0 + rate * years)

    principal = nominal * quantity
    current_value = principal * growth(today)
    maturity_value = principal * growth(maturity)
    return {
        "purchase_transaction_id": int(tranche["purchase_transaction_id"]),
        "asset_id": int(tranche["asset_id"]),
        "symbol": tranche["symbol"],
        "purchase_date": purchase.isoformat(),
        "maturity_date": maturity.isoformat(),
        "bond_type": bond_type,
        "quantity": quantity,
        "nominal_value": nominal,
        "principal": principal,
        "annual_rate_pct": tranche.get("annual_rate_pct"),
        "inflation_margin_pct": tranche.get("inflation_margin_pct"),
        "assumed_inflation_pct": tranche.get("assumed_inflation_pct"),
        "assumed_rate_pct": rate_pct,
        "current_value": current_value,
        "maturity_value": maturity_value,
        "estimated_interest": maturity_value - principal,
        "days_to_maturity": (maturity - today).days,
        "compounding": False,
        "overdue": maturity < today,
    }


def bond_value_for_asset(conn: sqlite3.Connection, asset_id: int,
                         as_of: str | None = None) -> tuple[float | None, float, str | None]:
    """Return weighted unit value, held quantity and optional valuation error."""
    tranches = list_bond_tranches(conn, asset_id=asset_id, as_of=as_of)
    if not tranches:
        return None, 0.0, "bond_tranche_missing"
    total_qty = sum(float(row.get("remaining_quantity", 0.0)) for row in tranches)
    if total_qty <= EPS:
        return None, 0.0, "bond_tranche_empty"
    total_value = 0.0
    for tranche in tranches:
        try:
            detail = estimate_tranche(tranche, as_of)
        except (TypeError, ValueError, OverflowError):
            return None, total_qty, "model_inflation_missing" if tranche.get("bond_type") == "inflation" and tranche.get("assumed_inflation_pct") is None else "bond_terms_invalid"
        # Cash interest already paid from this tranche is not still held in
        # its estimated market value. The actual payout is present in cash.
        # Only income explicitly linked to the tranche is deducted.
        payout_sql = """SELECT COALESCE(SUM(cash_amount * COALESCE(cash_fx_to_pln,1)),0)
            FROM transactions WHERE type='INTEREST' AND bond_tranche_purchase_id=?"""
        payout_args = [int(tranche['purchase_transaction_id'])]
        if as_of:
            payout_sql += " AND substr(occurred_at,1,10)<=?"
            payout_args.append(str(as_of)[:10])
        paid_pln = float(conn.execute(payout_sql, payout_args).fetchone()[0] or 0.0)
        principal = detail['principal']
        remaining_estimate = max(0.0, detail['current_value'] - principal - paid_pln)
        total_value += principal + remaining_estimate
    return total_value / total_qty, total_qty, None
