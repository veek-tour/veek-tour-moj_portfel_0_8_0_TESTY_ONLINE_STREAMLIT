from __future__ import annotations

from datetime import date, timedelta
import sqlite3
from typing import Any

from .ledger import LedgerError, calculate_ledger, latest_fx, latest_price
from .bonds import list_bond_tranches


def run_diagnostics(conn: sqlite3.Connection) -> dict[str, Any]:
    """Run a broad, read-only audit of the SQLite store and portfolio ledger."""
    errors: list[str] = []
    warnings: list[str] = []
    checks: list[dict[str, str]] = []

    integrity = conn.execute("PRAGMA integrity_check").fetchone()
    if not integrity or str(integrity[0]).lower() != "ok":
        errors.append(f"Kontrola integralności SQLite: {integrity[0] if integrity else 'brak odpowiedzi'}")
    else:
        checks.append({"Kontrola": "Integralność bazy SQLite", "Wynik": "Poprawna"})

    fk_issues = conn.execute("PRAGMA foreign_key_check").fetchall()
    if fk_issues:
        errors.append(f"Baza zawiera {len(fk_issues)} nieprawidłowych powiązań między tabelami.")
    else:
        checks.append({"Kontrola": "Powiązania między tabelami", "Wynik": "Poprawne"})

    try:
        ledger = calculate_ledger(conn)
        checks.append({"Kontrola": "Księgowanie i salda", "Wynik": "Poprawne"})
        for warning in ledger.get("warnings", []):
            warnings.append(str(warning))
    except (LedgerError, ValueError, TypeError, sqlite3.DatabaseError) as exc:
        ledger = None
        errors.append(f"Nie można przeliczyć księgi operacji: {exc}")

    # Validate transaction date/type values independently, with the offending row ID.
    allowed_types = {"BUY", "SELL", "DEPOSIT", "WITHDRAWAL", "DIVIDEND", "INTEREST", "FEE",
                     "STAKING_REWARD", "REWARD", "ASSET_TRANSFER", "CASH_TRANSFER", "FX_CONVERSION", "SWAP", "TOKEN_FEE"}
    for tx in conn.execute("SELECT id,occurred_at,type FROM transactions ORDER BY id"):
        try:
            date.fromisoformat(str(tx["occurred_at"])[:10])
        except (TypeError, ValueError):
            errors.append(f"Operacja #{tx['id']} ma nieprawidłową datę: {tx['occurred_at']!r}.")
        if tx["type"] not in allowed_types:
            errors.append(f"Operacja #{tx['id']} ma nieobsługiwany typ: {tx['type']!r}.")

    for row in conn.execute("SELECT id,symbol,name,category,currency,price_mode,price_symbol,manual_price FROM assets WHERE active=1"):
        if not str(row["symbol"] or "").strip() or not str(row["name"] or "").strip():
            errors.append(f"Aktywo #{row['id']} nie ma symbolu lub nazwy.")
        if row["category"] != "Obligacje" and row["price_mode"] == "auto" and not (row["price_symbol"] or "").strip():
            warnings.append(f"Aktywo {row['symbol']} nie ma symbolu źródła notowań.")
        if row["manual_price"] is not None:
            warnings.append(f"Aktywo {row['symbol']} ma starą ręczną wycenę z poprzedniej wersji; nie jest ona używana jako bieżąca cena.")

    # Validate tranche-specific terms. Asset-level bond fields are only defaults
    # for future purchases in version 0.6.2.
    for tx in conn.execute("""SELECT t.id,t.occurred_at,t.quantity,bt.*
        FROM transactions t JOIN assets a ON a.id=t.asset_id
        LEFT JOIN bond_tranches bt ON bt.purchase_transaction_id=t.id
        WHERE t.type='BUY' AND a.category='Obligacje' ORDER BY t.occurred_at,t.id"""):
        prefix = f"Transza zakupu #{tx['id']}"
        try:
            purchase = date.fromisoformat(str(tx["occurred_at"])[:10])
            if float(tx["quantity"] or 0) <= 0:
                errors.append(f"{prefix} ma nieprawidłową ilość.")
            if tx["bond_type"] not in ("fixed", "inflation"):
                errors.append(f"{prefix} ma nieprawidłowy rodzaj oprocentowania.")
            if float(tx["nominal_value"] or 0) <= 0:
                errors.append(f"{prefix} ma nieprawidłowy nominał.")
            if not tx["maturity_date"]:
                errors.append(f"{prefix} nie ma terminu wykupu.")
            else:
                maturity = date.fromisoformat(str(tx["maturity_date"])[:10])
                if maturity <= purchase:
                    errors.append(f"{prefix} ma termin wykupu niepóźniejszy niż data zakupu.")
            if tx["bond_type"] == "fixed" and tx["annual_rate_pct"] is None:
                errors.append(f"{prefix} nie ma oprocentowania stałego.")
            if tx["bond_type"] == "inflation":
                if tx["inflation_margin_pct"] is None:
                    errors.append(f"{prefix} nie ma zapisanej marży ponad inflację.")
                if tx["assumed_inflation_pct"] is None:
                    warnings.append(f"{prefix} nie ma przyjętej stopy inflacji; wycena tej transzy będzie niekompletna.")
                elif not -5 <= float(tx["assumed_inflation_pct"]) <= 100:
                    errors.append(f"{prefix} ma przyjętą stopę inflacji poza dozwolonym zakresem.")
        except (TypeError, ValueError):
            errors.append(f"{prefix} ma nieprawidłową datę lub parametry.")

    missing_tranches = conn.execute("""SELECT COUNT(*) FROM transactions t
        JOIN assets a ON a.id=t.asset_id LEFT JOIN bond_tranches bt ON bt.purchase_transaction_id=t.id
        WHERE t.type='BUY' AND a.category='Obligacje' AND bt.id IS NULL""").fetchone()[0]
    if missing_tranches:
        errors.append(f"{missing_tranches} zakupów obligacji nie ma przypisanej transzy.")

    for event in conn.execute("SELECT id,title,event_date FROM calendar_events"):
        if not str(event["title"] or "").strip():
            errors.append(f"Wydarzenie kalendarza #{event['id']} nie ma nazwy.")
        try:
            date.fromisoformat(str(event["event_date"])[:10])
        except (TypeError, ValueError):
            errors.append(f"Wydarzenie kalendarza #{event['id']} ma nieprawidłową datę.")
    for note in conn.execute("SELECT id,title FROM user_notes"):
        if not str(note["title"] or "").strip():
            errors.append(f"Notatka #{note['id']} nie ma tytułu.")

    for quote in conn.execute("SELECT id,price_date FROM price_history"):
        try:
            date.fromisoformat(str(quote["price_date"])[:10])
        except (TypeError, ValueError):
            errors.append(f"Wycena #{quote['id']} ma nieprawidłową datę.")
    for rate in conn.execute("SELECT id,rate_date FROM fx_history"):
        try:
            date.fromisoformat(str(rate["rate_date"])[:10])
        except (TypeError, ValueError):
            errors.append(f"Kurs walutowy #{rate['id']} ma nieprawidłową datę.")

    bad_prices = int(conn.execute("SELECT COUNT(*) FROM price_history WHERE price<=0 OR price IS NULL").fetchone()[0])
    bad_fx = int(conn.execute("SELECT COUNT(*) FROM fx_history WHERE rate_to_pln<=0 OR rate_to_pln IS NULL").fetchone()[0])
    if bad_prices:
        errors.append(f"W historii cen znaleziono {bad_prices} nieprawidłowych wartości (cena musi być dodatnia).")
    if bad_fx:
        errors.append(f"W historii kursów znaleziono {bad_fx} nieprawidłowych wartości.")
    if not bad_prices and not bad_fx:
        checks.append({"Kontrola": "Historyczne ceny i kursy", "Wynik": "Poprawne"})

    if ledger is not None:
        today = date.today()
        for pos in ledger["positions"]:
            asset = conn.execute("SELECT category FROM assets WHERE id=?", (pos["asset_id"],)).fetchone()
            if asset and asset["category"] == "Obligacje":
                continue
            price, price_day, _source = latest_price(conn, pos["asset_id"], pos["currency"])
            fx = latest_fx(conn, pos["currency"])
            if price is None:
                warnings.append(f"Brak automatycznej ceny dla {pos['symbol']} — wycena portfela może być niekompletna.")
            elif price_day:
                age = (today - date.fromisoformat(price_day)).days
                threshold = 1 if asset and asset["category"] == "Kryptowaluty" else 7
                if age > threshold:
                    warnings.append(f"Cena {pos['symbol']} jest nieaktualna ({price_day}, {age} dni temu).")
            if fx is None:
                warnings.append(f"Brak kursu waluty {pos['currency']} do PLN dla {pos['symbol']}.")
            elif str(pos["currency"]).upper() != "PLN":
                fx_row = conn.execute("SELECT MAX(rate_date) FROM fx_history WHERE currency=?", (str(pos["currency"]).upper(),)).fetchone()
                if fx_row and fx_row[0]:
                    fx_age = (today - date.fromisoformat(fx_row[0])).days
                    if fx_age > 7:
                        warnings.append(f"Kurs {pos['currency']}/PLN jest nieaktualny ({fx_row[0]}, {fx_age} dni temu).")
        for cash_item in ledger["cash"]:
            if latest_fx(conn, cash_item["currency"]) is None:
                warnings.append(f"Brak kursu waluty {cash_item['currency']} do PLN dla gotówki na rachunku {cash_item['account']}.")

    last_snapshot = conn.execute("SELECT MAX(snapshot_date) FROM portfolio_snapshots").fetchone()[0]
    if last_snapshot:
        try:
            snapshot_day = date.fromisoformat(last_snapshot)
            age = (date.today() - snapshot_day).days
        except ValueError:
            errors.append(f"Ostatnia migawka ma nieprawidłową datę: {last_snapshot}.")
            age = 0
        if age > 1:
            warnings.append(f"Ostatnia migawka portfela pochodzi z {last_snapshot}; nie ma bieżącej migawki.")
    elif conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]:
        warnings.append("Portfel ma operacje, ale nie zapisano jeszcze żadnej migawki historii.")

    checks.append({"Kontrola": "Spójność historii i konfiguracji", "Wynik": "Zakończona"})
    return {"ok": not errors, "errors": errors, "warnings": list(dict.fromkeys(warnings)), "checks": checks}
