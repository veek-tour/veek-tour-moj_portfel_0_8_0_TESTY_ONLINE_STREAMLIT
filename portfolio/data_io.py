from __future__ import annotations

import csv
import hashlib
import io
import sqlite3
from datetime import date
from typing import Any

from .db import add_transaction
from .ledger import calculate_ledger
from .snapshots import rebuild_snapshots

TX_FIELDS = ["occurred_at", "type", "account_id", "to_account_id", "asset_id", "to_asset_id", "quantity",
    "to_quantity", "unit_price", "price_currency", "price_fx_to_pln", "cash_amount", "cash_currency",
    "cash_fx_to_pln", "to_cash_amount", "to_cash_currency", "to_cash_fx_to_pln", "fee_amount",
    "fee_currency", "fee_fx_to_pln", "valuation_pln", "split_ratio", "bond_tranche_purchase_id", "linked_purchase_id", "linked_deposit_follows_purchase", "notes"]
INT_FIELDS = {"account_id", "to_account_id", "asset_id", "to_asset_id", "bond_tranche_purchase_id", "linked_purchase_id", "linked_deposit_follows_purchase"}
FLOAT_FIELDS = {"quantity", "to_quantity", "unit_price", "price_fx_to_pln", "cash_amount", "cash_fx_to_pln",
    "to_cash_amount", "to_cash_fx_to_pln", "fee_amount", "fee_fx_to_pln", "valuation_pln", "split_ratio"}
TRANCHE_FLOAT_FIELDS = {"tranche_nominal_value", "tranche_annual_rate_pct", "tranche_inflation_margin_pct", "tranche_assumed_inflation_pct"}
TRANCHE_FIELDS = ["tranche_bond_type", "tranche_nominal_value", "tranche_maturity_date",
    "tranche_annual_rate_pct", "tranche_inflation_margin_pct", "tranche_assumed_inflation_pct", "tranche_compounding"]


def import_transactions_csv(conn: sqlite3.Connection, content: bytes, filename: str = "transakcje.csv") -> dict[str, Any]:
    file_hash = hashlib.sha256(content).hexdigest()
    old = conn.execute("SELECT imported_rows FROM import_batches WHERE file_hash=?", (file_hash,)).fetchone()
    if old:
        return {"imported": 0, "duplicate_file": True, "message": "Ten sam plik został już zaimportowany."}
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("Plik CSV musi być zapisany jako UTF-8.") from exc
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("Plik CSV jest pusty lub nieprawidłowy.")
    fields = set(reader.fieldnames)
    if not {"occurred_at", "type"}.issubset(fields):
        raise ValueError("CSV musi zawierać co najmniej kolumny: occurred_at, type.")
    if "account_id" not in fields and "account_name" not in fields:
        raise ValueError("CSV musi zawierać kolumnę account_id lub account_name.")
    parsed, errors = [], []
    for row_no, row in enumerate(reader, start=2):
        data: dict[str, Any] = {}
        try:
            for key in TX_FIELDS:
                raw = row.get(key)
                if raw is None or str(raw).strip() == "":
                    data[key] = None
                elif key in INT_FIELDS:
                    data[key] = int(float(raw))
                elif key in FLOAT_FIELDS:
                    data[key] = float(str(raw).replace(",", "."))
                else:
                    data[key] = str(raw).strip()
            tranche_values: dict[str, Any] = {}
            for key in TRANCHE_FIELDS:
                raw = row.get(key)
                if raw is None or str(raw).strip() == "":
                    tranche_values[key] = None
                elif key in TRANCHE_FLOAT_FIELDS:
                    tranche_values[key] = float(str(raw).replace(",", "."))
                elif key == "tranche_compounding":
                    tranche_values[key] = bool(int(float(raw)))
                else:
                    tranche_values[key] = str(raw).strip()
            if tranche_values.get("tranche_bond_type"):
                data["bond_terms"] = {
                    "bond_type": tranche_values["tranche_bond_type"],
                    "nominal_value": tranche_values["tranche_nominal_value"],
                    "maturity_date": tranche_values["tranche_maturity_date"],
                    "annual_rate_pct": tranche_values["tranche_annual_rate_pct"],
                    "inflation_margin_pct": tranche_values["tranche_inflation_margin_pct"],
                    "assumed_inflation_pct": tranche_values["tranche_assumed_inflation_pct"],
                    "compounding": False,  # Model obligacji bez kapitalizacji w wersji 0.8
                }

            # Name-based mapping makes exports portable to another installation
            # where SQLite-generated IDs differ. IDs remain supported for simple CSVs.
            account_name = (row.get("account_name") or "").strip()
            if account_name:
                found = conn.execute("SELECT id FROM accounts WHERE name=?", (account_name,)).fetchone()
                if not found:
                    raise ValueError(f"Nie znaleziono rachunku '{account_name}'. Najpierw utwórz rachunki.")
                data["account_id"] = int(found[0])
            to_account_name = (row.get("to_account_name") or "").strip()
            if to_account_name:
                found = conn.execute("SELECT id FROM accounts WHERE name=?", (to_account_name,)).fetchone()
                if not found:
                    raise ValueError(f"Nie znaleziono rachunku docelowego '{to_account_name}'.")
                data["to_account_id"] = int(found[0])

            def map_asset(symbol_key: str, name_key: str, id_key: str) -> None:
                symbol = (row.get(symbol_key) or "").strip()
                name = (row.get(name_key) or "").strip()
                if not symbol:
                    return
                sql = "SELECT id FROM assets WHERE symbol=?"
                params: list[Any] = [symbol]
                if name:
                    sql += " AND name=?"
                    params.append(name)
                found = conn.execute(sql + " ORDER BY id", params).fetchall()
                if len(found) != 1:
                    if not found:
                        raise ValueError(f"Nie znaleziono instrumentu '{symbol}' ({name or 'bez nazwy'}). Najpierw utwórz instrumenty.")
                    raise ValueError(f"Ticker '{symbol}' jest niejednoznaczny. Użyj eksportu zawierającego nazwę instrumentu.")
                data[id_key] = int(found[0][0])

            map_asset("asset_symbol", "asset_name", "asset_id")
            map_asset("to_asset_symbol", "to_asset_name", "to_asset_id")
            data["linked_deposit_follows_purchase"] = data.get("linked_deposit_follows_purchase") or 0
            data["fee_amount"] = data.get("fee_amount") or 0.0
            data["notes"] = data.get("notes") or ""
            if not data.get("occurred_at") or not data.get("type") or (data.get("account_id") is None and not (row.get("account_name") or "").strip()):
                raise ValueError("Brakuje daty, typu operacji lub rachunku (account_id / account_name).")
            date.fromisoformat(data["occurred_at"][:10])
            # Keep CSV-origin IDs only as references. SQLite IDs can differ on
            # another installation, so tranche links are remapped after inserts.
            source_id_raw = (row.get("id") or "").strip()
            source_tranche_raw = (row.get("bond_tranche_purchase_id") or "").strip()
            source_linked_raw = (row.get("linked_purchase_id") or "").strip()
            data["_source_linked_purchase_id"] = int(source_linked_raw) if source_linked_raw else None
            data["_source_id"] = int(source_id_raw) if source_id_raw else None
            data["_source_tranche_purchase_id"] = int(source_tranche_raw) if source_tranche_raw else None
            parsed.append(data)
        except (ValueError, TypeError) as exc:
            errors.append(f"Wiersz {row_no}: {exc}")
    if errors:
        raise ValueError("\n".join(errors[:20]))
    if not parsed:
        raise ValueError("Plik nie zawiera żadnych operacji do importu.")

    conn.execute("SAVEPOINT csv_import")
    try:
        min_date = min(x["occurred_at"][:10] for x in parsed)
        imported_ids: dict[int, int] = {}
        pending_tranche_links: list[tuple[int, int]] = []
        pending_cash_links: list[tuple[int, int]] = []
        for data in parsed:
            data = dict(data)
            source_id = data.pop("_source_id", None)
            source_tranche_id = data.pop("_source_tranche_purchase_id", None)
            source_linked_id = data.pop("_source_linked_purchase_id", None)
            # Do not let an old database ID accidentally target an unrelated
            # purchase in the destination database.
            data["bond_tranche_purchase_id"] = None
            data["linked_purchase_id"] = None
            new_id = add_transaction(conn, data)
            if source_id is not None:
                imported_ids[source_id] = new_id
            if source_tranche_id is not None:
                pending_tranche_links.append((new_id, source_tranche_id))
            if source_linked_id is not None:
                pending_cash_links.append((new_id, source_linked_id))
        for deposit_id, source_purchase_id in pending_cash_links:
            mapped_purchase_id = imported_ids.get(source_purchase_id)
            if mapped_purchase_id is None:
                raise ValueError(f"Brakuje zakupu #{source_purchase_id} dla powiązanej wpłaty w pliku CSV.")
            tx_type = conn.execute("SELECT type FROM transactions WHERE id=?", (deposit_id,)).fetchone()[0]
            purchase_type = conn.execute("SELECT type FROM transactions WHERE id=?", (mapped_purchase_id,)).fetchone()[0]
            if tx_type != "DEPOSIT" or purchase_type != "BUY":
                raise ValueError("Powiązanie wpłaty musi prowadzić od DEPOSIT do BUY.")
            conn.execute("UPDATE transactions SET linked_purchase_id=? WHERE id=?", (mapped_purchase_id, deposit_id))
        for sale_id, source_purchase_id in pending_tranche_links:
            mapped_purchase_id = imported_ids.get(source_purchase_id)
            if mapped_purchase_id is None:
                # Retain direct references only for hand-authored CSVs which do
                # not carry source transaction IDs; exports always do.
                source_rows = [x for x in parsed if x.get("_source_id") is not None]
                if source_rows:
                    raise ValueError(f"Nie znaleziono w imporcie zakupu obligacji o identyfikatorze źródłowym {source_purchase_id}.")
                mapped_purchase_id = source_purchase_id
            conn.execute("UPDATE transactions SET bond_tranche_purchase_id=? WHERE id=?",
                         (mapped_purchase_id, sale_id))
        calculate_ledger(conn)
        rebuild_snapshots(conn, min_date, commit=False)
        conn.execute("INSERT INTO import_batches(file_hash,file_name,imported_rows) VALUES(?,?,?)", (file_hash, filename, len(parsed)))
        conn.execute("RELEASE SAVEPOINT csv_import")
        conn.commit()
        return {"imported": len(parsed), "duplicate_file": False, "message": f"Zaimportowano {len(parsed)} operacji."}
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT csv_import")
        conn.execute("RELEASE SAVEPOINT csv_import")
        conn.rollback()
        raise


def restore_database_from_bytes(conn: sqlite3.Connection, content: bytes) -> dict[str, str]:
    """Validate an uploaded SQLite backup and copy it into the currently open database."""
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory(prefix="portfolio_restore_") as tmp:
        path = Path(tmp) / "restore.sqlite3"
        path.write_bytes(content)
        source = sqlite3.connect(path)
        try:
            try:
                result = source.execute("PRAGMA integrity_check").fetchone()
                if not result or result[0] != "ok":
                    raise ValueError("Plik bazy danych nie przeszedł kontroli integralności SQLite.")
                foreign_key_issues = source.execute("PRAGMA foreign_key_check").fetchall()
                if foreign_key_issues:
                    raise ValueError("Plik bazy zawiera nieprawidłowe powiązania między rekordami.")
                tables = {r[0] for r in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            except sqlite3.DatabaseError as exc:
                raise ValueError("Przesłany plik nie jest prawidłową bazą SQLite.") from exc
            required = {"accounts", "assets", "transactions"}
            if not required.issubset(tables):
                raise ValueError("Plik nie wygląda na kopię bazy aplikacji Mój Portfel.")
            required_columns = {
                "accounts": {"id", "name", "kind"},
                "assets": {"id", "symbol", "name", "category", "currency"},
                "transactions": {"id", "occurred_at", "type", "account_id", "asset_id"},
            }
            for table, expected in required_columns.items():
                actual = {r[1] for r in source.execute(f"PRAGMA table_info({table})").fetchall()}
                if not expected.issubset(actual):
                    raise ValueError(f"Tabela {table} nie zawiera wymaganych kolumn aplikacji.")
            conn.commit()
            source.backup(conn)
            conn.commit()
        finally:
            source.close()
    return {"status": "ok", "message": "Przywrócono bazę. Zastosowano dostępne migracje schematu."}


def database_bytes(conn: sqlite3.Connection) -> bytes:
    """Return a consistent standalone SQLite backup, including committed WAL data."""
    target = sqlite3.connect(":memory:")
    try:
        conn.commit()
        conn.backup(target)
        return target.serialize()
    finally:
        target.close()
