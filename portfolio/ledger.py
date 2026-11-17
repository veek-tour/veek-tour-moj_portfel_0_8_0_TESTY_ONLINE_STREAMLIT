from __future__ import annotations

from collections import defaultdict
from typing import Any
import sqlite3
from datetime import date

from .bonds import bond_value_for_asset, list_bond_tranches

EPS = 1e-9

class LedgerError(ValueError):
    pass


def fx_currency(currency: str) -> str:
    """USDT and USDC are intentionally treated as USD in this personal tracker."""
    currency = (currency or "PLN").upper()
    return "USD" if currency in {"USDT", "USDC"} else currency


def _fx(value: Any, label: str) -> float:
    if value is None or float(value) <= 0:
        raise LedgerError(f"Brak prawidłowego kursu PLN dla: {label}.")
    return float(value)


def _positive(value: Any, label: str) -> float:
    if value is None or float(value) <= 0:
        raise LedgerError(f"Wartość {label} musi być większa od zera.")
    return float(value)


def latest_fx(conn: sqlite3.Connection, currency: str, as_of: str | None = None) -> float | None:
    currency = fx_currency(currency)
    if currency == "PLN":
        return 1.0
    sql = "SELECT rate_to_pln FROM fx_history WHERE currency=?"
    params: list[Any] = [currency]
    if as_of:
        sql += " AND rate_date<=?"
        params.append(as_of)
    sql += " ORDER BY rate_date DESC, CASE WHEN source='manual' THEN 0 ELSE 1 END, updated_at DESC LIMIT 1"
    row = conn.execute(sql, params).fetchone()
    return float(row[0]) if row else None


def calculate_ledger(conn: sqlite3.Connection, as_of: str | None = None) -> dict[str, Any]:
    """Replay transactions. Pooled average cost is maintained in PLN and asset currency."""
    assets = {int(r["id"]): dict(r) for r in conn.execute("SELECT * FROM assets").fetchall()}
    accounts = {int(r["id"]): dict(r) for r in conn.execute("SELECT * FROM accounts").fetchall()}
    sql = "SELECT * FROM transactions"
    params: tuple[Any, ...] = ()
    if as_of:
        sql += " WHERE substr(occurred_at,1,10) <= ?"
        params = (as_of,)
    sql += " ORDER BY occurred_at, id"
    transactions = [dict(r) for r in conn.execute(sql, params).fetchall()]

    account_assets: dict[tuple[int, int], float] = defaultdict(float)
    global_qty: dict[int, float] = defaultdict(float)
    global_cost: dict[int, float] = defaultdict(float)
    global_cost_ccy: dict[int, float] = defaultdict(float)
    ccy_basis_complete: dict[int, bool] = defaultdict(lambda: True)
    cash: dict[tuple[int, str], float] = defaultdict(float)
    realized = income = fees = 0.0
    income_by_asset: dict[int, float] = defaultdict(float)
    warnings: list[str] = []
    event_rows: list[dict[str, Any]] = []

    def cash_delta(account_id: int, currency: str, amount: float) -> None:
        if account_id not in accounts:
            raise LedgerError(f"Nie znaleziono rachunku o ID {account_id}.")
        cash[(account_id, fx_currency(currency))] += float(amount)

    def fee_pln(tx: dict[str, Any]) -> float:
        amount = float(tx.get("fee_amount") or 0)
        if amount <= EPS:
            return 0.0
        currency = fx_currency(tx.get("fee_currency") or "PLN")
        return amount * _fx(tx.get("fee_fx_to_pln") or (1.0 if currency == "PLN" else None), currency)

    def asset_rate(asset_id: int, tx: dict[str, Any]) -> float:
        currency = assets[asset_id]["currency"]
        rate = tx.get("price_fx_to_pln")
        if rate is None:
            rate = latest_fx(conn, currency, tx["occurred_at"][:10])
        return _fx(rate if rate is not None else (1.0 if fx_currency(currency) == "PLN" else None), currency)

    for tx in transactions:
        typ = tx["type"]
        aid = tx.get("account_id")
        if aid not in accounts:
            raise LedgerError(f"Operacja #{tx['id']} ({typ}): nie wskazano poprawnego rachunku.")
        asset_id = tx.get("asset_id")
        occurred = tx["occurred_at"]
        try:
            qty = float(tx.get("quantity") or 0)
            fee_value = fee_pln(tx)
            fees += fee_value
            if typ == "DEPOSIT":
                amount = _positive(tx.get("cash_amount"), "wpłata")
                cash_delta(aid, tx.get("cash_currency") or "PLN", amount)
            elif typ == "WITHDRAWAL":
                amount = _positive(tx.get("cash_amount"), "wypłata")
                cash_delta(aid, tx.get("cash_currency") or "PLN", -amount)
            elif typ == "CASH_TRANSFER":
                amount = _positive(tx.get("cash_amount"), "transfer gotówki")
                currency = tx.get("cash_currency") or "PLN"
                to_amount = float(tx.get("to_cash_amount") or amount)
                if abs(to_amount - amount) > 1e-6:
                    raise LedgerError("Transfer gotówki w tej samej walucie musi przenosić tę samą kwotę.")
                cash_delta(aid, currency, -amount)
                cash_delta(int(tx["to_account_id"]), currency, amount)
            elif typ == "FX_CONVERSION":
                amount = _positive(tx.get("cash_amount"), "kwota wymiany")
                target_amount = _positive(tx.get("to_cash_amount"), "kwota po wymianie")
                cash_delta(aid, tx.get("cash_currency") or "PLN", -amount)
                cash_delta(aid, tx.get("to_cash_currency") or "PLN", target_amount)
            elif typ in ("BUY", "SELL"):
                if asset_id not in assets:
                    raise LedgerError(f"Operacja {typ}: nie wskazano poprawnego aktywa.")
                qty = _positive(qty, "ilość")
                cash_amount = _positive(tx.get("cash_amount"), "kwota rozliczenia")
                cash_currency = fx_currency(tx.get("cash_currency") or tx.get("price_currency") or "PLN")
                cash_rate = _fx(tx.get("cash_fx_to_pln") or (1.0 if cash_currency == "PLN" else latest_fx(conn, cash_currency, occurred[:10])), cash_currency)
                fee_currency = tx.get("fee_currency") or cash_currency
                if typ == "BUY":
                    cash_delta(aid, cash_currency, -cash_amount)
                    if tx.get("fee_amount"):
                        cash_delta(aid, fee_currency, -float(tx["fee_amount"]))
                    total_cost_pln = cash_amount * cash_rate + fee_value
                    asset_ccy = fx_currency(assets[asset_id]["currency"])
                    price_ccy = fx_currency(tx.get("price_currency") or assets[asset_id]["currency"])
                    known_rate = latest_fx(conn, assets[asset_id]["currency"], occurred[:10])
                    # The transaction price can be quoted in a *different*
                    # currency (e.g. physical silver quoted in PLN while its
                    # instrument reference currency is USD). Do not mistake
                    # PLN->PLN=1 for the USD reference FX rate.
                    if price_ccy == asset_ccy and tx.get("price_fx_to_pln") is not None:
                        known_rate = float(tx["price_fx_to_pln"])
                    if known_rate is None and asset_ccy == "PLN":
                        known_rate = 1.0
                    if cash_currency != asset_ccy and known_rate:
                        # A broker may debit PLN for an instrument quoted in
                        # EUR/USD at a worse rate than the NBP reference.
                        # Include this real FX difference in the effective
                        # purchase cost, including fees, not as a separate P/L.
                        basis_ccy = total_cost_pln / known_rate
                    elif price_ccy == asset_ccy and tx.get("unit_price") is not None:
                        basis_ccy = qty * float(tx["unit_price"])
                        if fee_value and known_rate:
                            basis_ccy += fee_value / known_rate
                        elif fee_value:
                            ccy_basis_complete[asset_id] = False
                    elif cash_currency == asset_ccy:
                        basis_ccy = cash_amount
                        if fee_value and known_rate:
                            basis_ccy += fee_value / known_rate
                        elif fee_value:
                            ccy_basis_complete[asset_id] = False
                    elif known_rate:
                        basis_ccy = total_cost_pln / known_rate
                    else:
                        basis_ccy = 0.0
                        ccy_basis_complete[asset_id] = False
                    global_qty[asset_id] += qty
                    account_assets[(aid, asset_id)] += qty
                    global_cost[asset_id] += total_cost_pln
                    global_cost_ccy[asset_id] += basis_ccy
                else:
                    if qty > global_qty[asset_id] + EPS:
                        raise LedgerError(f"Sprzedaż {assets[asset_id]['symbol']}: ilość przekracza stan całego portfela.")
                    if qty > account_assets[(aid, asset_id)] + EPS:
                        raise LedgerError(f"Sprzedaż {assets[asset_id]['symbol']}: za mało jednostek na wybranym rachunku.")
                    avg_cost = global_cost[asset_id] / global_qty[asset_id] if global_qty[asset_id] > EPS else 0.0
                    avg_cost_ccy = global_cost_ccy[asset_id] / global_qty[asset_id] if global_qty[asset_id] > EPS else 0.0
                    allocated_cost = avg_cost * qty
                    allocated_ccy = avg_cost_ccy * qty
                    cash_delta(aid, cash_currency, cash_amount)
                    if tx.get("fee_amount"):
                        cash_delta(aid, fee_currency, -float(tx["fee_amount"]))
                    global_qty[asset_id] -= qty
                    account_assets[(aid, asset_id)] -= qty
                    global_cost[asset_id] -= allocated_cost
                    global_cost_ccy[asset_id] -= allocated_ccy
                    realized += cash_amount * cash_rate - fee_value - allocated_cost
            elif typ in ("DIVIDEND", "INTEREST"):
                if asset_id not in assets:
                    label = "Dywidenda" if typ == "DIVIDEND" else "Odsetki"
                    raise LedgerError(f"{label} w operacji #{tx['id']} wymaga przypisania do konkretnego aktywa.")
                amount = _positive(tx.get("cash_amount"), "przychód")
                currency = tx.get("cash_currency") or "PLN"
                rate = _fx(tx.get("cash_fx_to_pln") or (1.0 if fx_currency(currency) == "PLN" else latest_fx(conn, currency, occurred[:10])), currency)
                cash_delta(aid, currency, amount)
                if tx.get("fee_amount"):
                    cash_delta(aid, tx.get("fee_currency") or currency, -float(tx["fee_amount"]))
                net_income = amount * rate - fee_value
                income += net_income
                if asset_id in assets:
                    income_by_asset[int(asset_id)] += net_income
            elif typ == "FEE":
                amount = _positive(tx.get("cash_amount"), "opłata")
                currency = tx.get("cash_currency") or "PLN"
                rate = _fx(tx.get("cash_fx_to_pln") or (1.0 if fx_currency(currency) == "PLN" else latest_fx(conn, currency, occurred[:10])), currency)
                cash_delta(aid, currency, -amount)
                fees += amount * rate
            elif typ == "REWARD":
                if asset_id not in assets or assets[asset_id]['category'] != 'Kryptowaluty':
                    raise LedgerError("Nagroda jednostkowa wymaga wskazania kryptowaluty.")
                qty = _positive(qty, "ilość nagrody")
                if not str(tx.get('notes') or '').strip():
                    raise LedgerError("Podaj notatkę opisującą nagrodę.")
                # Zero-cost DCA reward: the market value of these extra units
                # appears in unrealized P/L, never booked again as cash income.
                global_qty[asset_id] += qty
                account_assets[(aid, asset_id)] += qty
            elif typ == "STAKING_REWARD":
                if asset_id not in assets:
                    raise LedgerError("Nagroda stakingowa wymaga wskazania kryptowaluty.")
                qty = _positive(qty, "ilość nagrody")
                price = _positive(tx.get("unit_price"), "cena jednostkowa nagrody")
                currency = tx.get("price_currency") or assets[asset_id]["currency"]
                rate = _fx(tx.get("price_fx_to_pln") or (1.0 if fx_currency(currency) == "PLN" else latest_fx(conn, currency, occurred[:10])), currency)
                value_pln = qty * price * rate
                ccy_rate = asset_rate(asset_id, tx)
                global_qty[asset_id] += qty
                account_assets[(aid, asset_id)] += qty
                global_cost[asset_id] += value_pln
                global_cost_ccy[asset_id] += value_pln / ccy_rate
                income += value_pln
                income_by_asset[int(asset_id)] += value_pln
            elif typ == "ASSET_TRANSFER":
                if asset_id not in assets:
                    raise LedgerError("Transfer aktywa wymaga wskazania aktywa.")
                qty = _positive(qty, "ilość transferu")
                to_account_id = int(tx.get("to_account_id") or 0)
                if to_account_id not in accounts:
                    raise LedgerError("Transfer aktywa wymaga prawidłowego rachunku docelowego.")
                if qty > account_assets[(aid, asset_id)] + EPS:
                    raise LedgerError(f"Transfer {assets[asset_id]['symbol']}: za mało jednostek na rachunku źródłowym.")
                account_assets[(aid, asset_id)] -= qty
                account_assets[(to_account_id, asset_id)] += qty
            elif typ == "SPLIT":
                raise LedgerError("Obsługa splitów została wyłączona. Skoryguj ilość aktywa ręcznie po takim zdarzeniu.")
            elif typ == "SWAP":
                out_id, in_id = asset_id, tx.get("to_asset_id")
                if out_id not in assets or in_id not in assets or out_id == in_id:
                    raise LedgerError("Wymiana wymaga dwóch różnych, poprawnych aktywów.")
                out_qty = _positive(qty, "ilość wymienianego aktywa")
                in_qty = _positive(tx.get("to_quantity"), "ilość otrzymanego aktywa")
                value_pln = _positive(tx.get("valuation_pln"), "wartość wymiany w PLN")
                if out_qty > account_assets[(aid, out_id)] + EPS or out_qty > global_qty[out_id] + EPS:
                    raise LedgerError("Wymiana: ilość oddawanego aktywa przekracza stan.")
                avg_cost = global_cost[out_id] / global_qty[out_id] if global_qty[out_id] > EPS else 0.0
                avg_ccy = global_cost_ccy[out_id] / global_qty[out_id] if global_qty[out_id] > EPS else 0.0
                allocated = avg_cost * out_qty
                allocated_ccy = avg_ccy * out_qty
                account_assets[(aid, out_id)] -= out_qty
                global_qty[out_id] -= out_qty
                global_cost[out_id] -= allocated
                global_cost_ccy[out_id] -= allocated_ccy
                in_rate = latest_fx(conn, assets[in_id]["currency"], occurred[:10])
                if in_rate is None and fx_currency(assets[in_id]["currency"]) == "PLN":
                    in_rate = 1.0
                if in_rate is None:
                    ccy_basis_complete[in_id] = False
                    in_basis_ccy = 0.0
                else:
                    in_basis_ccy = value_pln / in_rate
                account_assets[(aid, in_id)] += in_qty
                global_qty[in_id] += in_qty
                global_cost[in_id] += value_pln
                global_cost_ccy[in_id] += in_basis_ccy
                if tx.get("fee_amount"):
                    cash_delta(aid, tx.get("fee_currency") or "PLN", -float(tx["fee_amount"]))
                realized += value_pln - allocated - fee_value
            elif typ == "TOKEN_FEE":
                if asset_id not in assets:
                    raise LedgerError("Opłata pobrana w aktywie wymaga wskazania aktywa.")
                qty = _positive(qty, "ilość opłaty")
                if qty > account_assets[(aid, asset_id)] + EPS or qty > global_qty[asset_id] + EPS:
                    raise LedgerError("Opłata tokenowa przekracza dostępny stan aktywa.")
                avg_cost = global_cost[asset_id] / global_qty[asset_id] if global_qty[asset_id] > EPS else 0.0
                avg_ccy = global_cost_ccy[asset_id] / global_qty[asset_id] if global_qty[asset_id] > EPS else 0.0
                allocated = avg_cost * qty
                account_assets[(aid, asset_id)] -= qty
                global_qty[asset_id] -= qty
                global_cost[asset_id] -= allocated
                global_cost_ccy[asset_id] -= avg_ccy * qty
                realized -= allocated
                fees += allocated
            else:
                raise LedgerError(f"Nieobsługiwany typ operacji: {typ}")
        except (LedgerError, TypeError, ValueError) as exc:
            raise LedgerError(f"Operacja #{tx['id']} ({typ}, {occurred[:10]}): {exc}") from exc
        event_rows.append({"id": tx["id"], "date": occurred, "type": typ, "notes": tx.get("notes", "")})

    # Check that any tranche-specific bond sales refer to an open tranche and
    # cannot redeem more units than that tranche contains.
    for asset_id, asset in assets.items():
        if asset.get("category") == "Obligacje":
            try:
                list_bond_tranches(conn, asset_id=asset_id, as_of=as_of)
            except ValueError as exc:
                raise LedgerError(str(exc)) from exc

    for key in list(account_assets):
        if abs(account_assets[key]) < EPS:
            account_assets[key] = 0.0
    for asset_id in assets:
        if abs(global_qty[asset_id]) < EPS:
            global_qty[asset_id] = global_cost[asset_id] = global_cost_ccy[asset_id] = 0.0
            ccy_basis_complete[asset_id] = True

    positions = []
    for asset_id, asset in assets.items():
        qty = global_qty[asset_id]
        if qty > EPS:
            cost = global_cost[asset_id]
            cost_ccy = global_cost_ccy[asset_id]
            positions.append({"asset_id": asset_id, "symbol": asset["symbol"], "name": asset["name"],
                "category": asset["category"], "currency": asset["currency"], "unit": asset["unit"], "quantity": qty,
                "cost_pln": cost, "avg_cost_pln": cost / qty,
                "cost_asset_currency": cost_ccy if ccy_basis_complete[asset_id] else None,
                "avg_cost_asset_currency": cost_ccy / qty if ccy_basis_complete[asset_id] else None,
                "income_pln": income_by_asset.get(asset_id, 0.0)})
    cash = defaultdict(float, {k: v for k, v in cash.items() if abs(v) >= EPS})
    cash_rows = [{"account_id": aid, "account": accounts.get(aid, {}).get("name", f"Rachunek {aid}"),
                  "currency": currency, "balance": balance} for (aid, currency), balance in cash.items() if abs(balance) > EPS]
    for item in cash_rows:
        if item["balance"] < -EPS:
            warnings.append(f"Ujemne saldo gotówki: {item['account']} · {item['currency']} ({item['balance']:.2f}).")
    account_asset_rows = []
    for (account_id, asset_id), quantity in account_assets.items():
        if quantity > EPS and account_id in accounts and asset_id in assets:
            account_asset_rows.append({"account_id": account_id, "account": accounts[account_id]["name"],
                "account_kind": accounts[account_id]["kind"], "asset_id": asset_id, "symbol": assets[asset_id]["symbol"],
                "asset_name": assets[asset_id]["name"], "category": assets[asset_id]["category"],
                "listing_exchange": assets[asset_id]["listing_exchange"], "currency": assets[asset_id]["currency"],
                "unit": assets[asset_id]["unit"], "quantity": quantity})
    return {"assets": assets, "accounts": accounts, "positions": positions, "cash": cash_rows,
        "account_asset_rows": account_asset_rows, "account_assets": dict(account_assets), "global_qty": dict(global_qty),
        "global_cost": dict(global_cost), "global_cost_ccy": dict(global_cost_ccy), "cash_balances": dict(cash),
        "realized_pln": realized, "income_pln": income, "income_by_asset": dict(income_by_asset),
        "fees_pln": fees, "warnings": warnings, "events": event_rows}


def latest_price(conn: sqlite3.Connection, asset_id: int, currency: str,
                 as_of: str | None = None) -> tuple[float | None, str | None, str | None]:
    """Backward-compatible quote tuple, without currency information."""
    row = latest_price_with_currency(conn, asset_id, currency, as_of)
    return row['price'], row['date'], row['source']


def latest_price_with_currency(conn: sqlite3.Connection, asset_id: int, currency: str,
                               as_of: str | None = None) -> dict[str, Any]:
    info = conn.execute("SELECT category,symbol FROM assets WHERE id=?", (asset_id,)).fetchone()
    if info and info['category'] == 'Metale szlachetne':
        from .metals import latest_metal_price
        raw = latest_metal_price(conn, asset_id, info['symbol'], as_of or date.today().isoformat())
    else:
        sql = "SELECT price,price_date,source,currency FROM price_history WHERE asset_id=?"
        params: list[Any] = [asset_id]
        if as_of:
            sql += " AND price_date<=?"
            params.append(as_of)
        sql += " ORDER BY price_date DESC, CASE WHEN source='manual' THEN 0 ELSE 1 END, updated_at DESC LIMIT 1"
        raw = conn.execute(sql, params).fetchone()
    if not raw:
        return {"price": None, "date": None, "source": None, "currency": currency}
    return {"price": float(raw['price']), "date": raw['price_date'], "source": raw['source'],
            "currency": raw['currency']}



def _bond_unit_value(conn: sqlite3.Connection, position: dict[str, Any], as_of: str | None = None) -> tuple[float | None, str | None, str | None]:
    """Weighted per-unit value built from separately valued purchase tranches."""
    price, tranche_quantity, error = bond_value_for_asset(conn, int(position["asset_id"]), as_of)
    expected_quantity = float(position["quantity"])
    tolerance = max(1e-7, abs(expected_quantity) * 1e-7)
    if error:
        return None, as_of or date.today().isoformat(), error
    if abs(tranche_quantity - expected_quantity) > tolerance:
        return None, as_of or date.today().isoformat(), "bond_tranche_quantity_mismatch"
    return price, as_of or date.today().isoformat(), "bond_model"


def value_portfolio(conn: sqlite3.Connection, as_of: str | None = None) -> dict[str, Any]:
    ledger = calculate_ledger(conn, as_of)
    asset_value = cash_value = partial_unrealized = 0.0
    valued_positions, valued_cash, missing_prices = [], [], []
    complete_positions = 0
    for p in ledger["positions"]:
        if p.get("category") == "Obligacje":
            price, price_date, source = _bond_unit_value(conn, p, as_of)
            quote_currency = p["currency"]
        else:
            quote = latest_price_with_currency(conn, p["asset_id"], p["currency"], as_of)
            price, price_date, source = quote["price"], quote["date"], quote["source"]
            quote_currency = quote["currency"]
        fx = latest_fx(conn, quote_currency, as_of)
        if price is None or fx is None:
            if price is None and source == "model_inflation_missing":
                missing_prices.append(p["symbol"] + " (brak przyjętej inflacji w jednej z transz obligacji)")
            elif price is None and source == "bond_terms_invalid":
                missing_prices.append(p["symbol"] + " (niekompletne warunki transzy obligacji)")
            elif price is None and source == "bond_tranche_missing":
                missing_prices.append(p["symbol"] + " (brak transzy zakupu obligacji)")
            elif price is None and source == "bond_tranche_quantity_mismatch":
                missing_prices.append(p["symbol"] + " (niezgodność ilości transz z księgą)")
            else:
                missing_prices.append(p["symbol"] + (" (cena)" if price is None else " (kurs FX)"))
            market_value = pnl = None
        else:
            market_value = p["quantity"] * price * fx
            asset_value += market_value
            pnl = market_value - p["cost_pln"]
            partial_unrealized += pnl
            complete_positions += 1
        valued_positions.append({**p, "price": price, "price_date": price_date, "price_source": source,
            "price_currency": quote_currency, "fx_to_pln": fx, "market_value_pln": market_value, "unrealized_pln": pnl})
    for c in ledger["cash"]:
        fx = latest_fx(conn, c["currency"], as_of)
        if fx is None:
            missing_prices.append(f"Gotówka {c['currency']} · {c['account']} (kurs FX)")
            value = None
        else:
            value = c["balance"] * fx
            cash_value += value
        valued_cash.append({**c, "fx_to_pln": fx, "value_pln": value})
    cost_total = sum(p["cost_pln"] for p in ledger["positions"])
    missing = sorted(set(missing_prices))
    valuation_complete = not missing
    partial_total = asset_value + cash_value
    total_value = partial_total if valuation_complete else None
    flow_sql = "SELECT type,occurred_at,cash_amount,cash_currency,cash_fx_to_pln FROM transactions WHERE type IN ('DEPOSIT','WITHDRAWAL')"
    flow_params = ()
    if as_of:
        flow_sql += " AND substr(occurred_at,1,10)<=?"
        flow_params = (as_of,)
    net_contributions = 0.0
    for flow in conn.execute(flow_sql, flow_params).fetchall():
        curr = flow["cash_currency"] or "PLN"
        rate = flow["cash_fx_to_pln"]
        if rate is None or float(rate) <= 0:
            rate = latest_fx(conn, curr, flow["occurred_at"][:10])
        if rate is None or float(rate) <= 0:
            missing.append(f"Kurs historyczny wpłaty/wypłaty {curr}")
            continue
        amount = float(flow["cash_amount"] or 0) * float(rate)
        net_contributions += amount if flow["type"] == "DEPOSIT" else -amount
    valuation_complete = not missing
    if not valuation_complete:
        total_value = None
    unrealized = partial_unrealized if valuation_complete else None
    total_pnl = total_value - net_contributions if total_value is not None else None
    return {**ledger, "valued_positions": valued_positions, "valued_cash": valued_cash,
        "assets_value_pln": asset_value, "cash_value_pln": cash_value, "partial_total_value_pln": partial_total,
        "total_value_pln": total_value, "cost_total_pln": cost_total, "net_contributions_pln": net_contributions,
        "unrealized_pln": unrealized, "partial_unrealized_pln": partial_unrealized,
        "total_pnl_pln": total_pnl, "missing_prices": sorted(set(missing)), "valuation_complete": valuation_complete}
