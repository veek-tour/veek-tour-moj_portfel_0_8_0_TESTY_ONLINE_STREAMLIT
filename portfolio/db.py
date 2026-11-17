from __future__ import annotations

import sqlite3
import shutil
import os
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_DIR / "data"
DB_PATH = Path(os.environ.get("PORTFOLIO_DB", str(DATA_DIR / "portfolio.sqlite3")))

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL DEFAULT 'broker',
    notes TEXT DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS assets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    currency TEXT NOT NULL DEFAULT 'PLN',
    listing_exchange TEXT DEFAULT '',
    price_mode TEXT NOT NULL DEFAULT 'auto',
    price_symbol TEXT DEFAULT '',
    unit TEXT DEFAULT 'szt.',
    bond_type TEXT DEFAULT '',
    annual_rate_pct REAL,
    inflation_margin_pct REAL,
    nominal_value REAL DEFAULT 100,
    maturity_date TEXT,
    bond_compounding INTEGER NOT NULL DEFAULT 1,
    manual_price REAL,
    active INTEGER NOT NULL DEFAULT 1,
    notes TEXT DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(symbol, category)
);
CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    type TEXT NOT NULL,
    account_id INTEGER,
    to_account_id INTEGER,
    asset_id INTEGER,
    to_asset_id INTEGER,
    quantity REAL,
    to_quantity REAL,
    unit_price REAL,
    price_currency TEXT,
    price_fx_to_pln REAL,
    cash_amount REAL DEFAULT 0,
    cash_currency TEXT,
    cash_fx_to_pln REAL,
    to_cash_amount REAL,
    to_cash_currency TEXT,
    to_cash_fx_to_pln REAL,
    fee_amount REAL DEFAULT 0,
    fee_currency TEXT,
    fee_fx_to_pln REAL,
    valuation_pln REAL,
    split_ratio REAL,
    bond_tranche_purchase_id INTEGER,
    linked_purchase_id INTEGER REFERENCES transactions(id) ON DELETE CASCADE,
    linked_deposit_follows_purchase INTEGER NOT NULL DEFAULT 0,
    metal_spot_pln_per_oz REAL,
    metal_adjustment_pln_per_oz REAL,
    price_fx_rate_date TEXT,
    price_fx_source TEXT,
    cash_fx_rate_date TEXT,
    cash_fx_source TEXT,
    fee_fx_rate_date TEXT,
    fee_fx_source TEXT,
    notes TEXT DEFAULT '', 
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(account_id) REFERENCES accounts(id),
    FOREIGN KEY(to_account_id) REFERENCES accounts(id),
    FOREIGN KEY(asset_id) REFERENCES assets(id),
    FOREIGN KEY(to_asset_id) REFERENCES assets(id)
);
CREATE INDEX IF NOT EXISTS idx_transactions_date ON transactions(occurred_at, id);
CREATE INDEX IF NOT EXISTS idx_transactions_asset ON transactions(asset_id);
CREATE TABLE IF NOT EXISTS price_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id INTEGER NOT NULL,
    price_date TEXT NOT NULL,
    price REAL NOT NULL,
    currency TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'manual',
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(asset_id) REFERENCES assets(id) ON DELETE CASCADE,
    UNIQUE(asset_id, price_date, source)
);
CREATE INDEX IF NOT EXISTS idx_price_history_date ON price_history(asset_id, price_date);
CREATE TABLE IF NOT EXISTS fx_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    currency TEXT NOT NULL,
    rate_date TEXT NOT NULL,
    rate_to_pln REAL NOT NULL,
    source TEXT NOT NULL DEFAULT 'NBP',
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(currency, rate_date, source)
);
CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    snapshot_date TEXT PRIMARY KEY,
    assets_value_pln REAL NOT NULL,
    cash_value_pln REAL NOT NULL,
    total_value_pln REAL NOT NULL,
    invested_cost_pln REAL NOT NULL,
    realized_pln REAL NOT NULL,
    income_pln REAL NOT NULL,
    valuation_complete INTEGER NOT NULL DEFAULT 1,
    notes TEXT DEFAULT '',
    manual_total_value_pln REAL,
    manual_notes TEXT DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS allocation_targets (
    category TEXT PRIMARY KEY,
    target_pct REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS watchlist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    name TEXT NOT NULL,
    price_symbol TEXT DEFAULT '',
    currency TEXT DEFAULT 'PLN',
    notes TEXT DEFAULT '',
    UNIQUE(symbol)
);
CREATE TABLE IF NOT EXISTS watchlist_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    watchlist_id INTEGER NOT NULL,
    price_date TEXT NOT NULL,
    price REAL NOT NULL,
    currency TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'yahoo',
    UNIQUE(watchlist_id,price_date,source),
    FOREIGN KEY(watchlist_id) REFERENCES watchlist(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS benchmark_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    price_date TEXT NOT NULL,
    price REAL NOT NULL,
    currency TEXT NOT NULL DEFAULT 'USD',
    source TEXT NOT NULL DEFAULT 'yahoo',
    UNIQUE(symbol, price_date, source)
);
CREATE TABLE IF NOT EXISTS staking_plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id INTEGER NOT NULL,
    source_account_id INTEGER NOT NULL,
    staking_account_id INTEGER NOT NULL,
    quantity REAL NOT NULL,
    start_date TEXT NOT NULL,
    planned_end_date TEXT,
    actual_end_date TEXT,
    apy_pct REAL NOT NULL DEFAULT 0,
    compounding_frequency TEXT NOT NULL DEFAULT 'daily',
    active INTEGER NOT NULL DEFAULT 1,
    lock_transaction_id INTEGER,
    unlock_transaction_id INTEGER,
    notes TEXT DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(asset_id) REFERENCES assets(id),
    FOREIGN KEY(source_account_id) REFERENCES accounts(id),
    FOREIGN KEY(staking_account_id) REFERENCES accounts(id),
    FOREIGN KEY(lock_transaction_id) REFERENCES transactions(id),
    FOREIGN KEY(unlock_transaction_id) REFERENCES transactions(id)
);
CREATE INDEX IF NOT EXISTS idx_staking_active ON staking_plans(active, start_date);
CREATE TABLE IF NOT EXISTS import_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_hash TEXT NOT NULL UNIQUE,
    file_name TEXT NOT NULL,
    imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    imported_rows INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS user_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    asset_id INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(asset_id) REFERENCES assets(id) ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS calendar_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    event_date TEXT NOT NULL,
    event_type TEXT NOT NULL DEFAULT 'Przypomnienie',
    asset_id INTEGER,
    notes TEXT DEFAULT '',
    completed INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(asset_id) REFERENCES assets(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_calendar_events_date ON calendar_events(event_date, completed);
CREATE TABLE IF NOT EXISTS bond_tranches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    purchase_transaction_id INTEGER NOT NULL UNIQUE,
    bond_type TEXT NOT NULL,
    nominal_value REAL NOT NULL,
    maturity_date TEXT,
    annual_rate_pct REAL,
    inflation_margin_pct REAL,
    assumed_inflation_pct REAL,
    compounding INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(purchase_transaction_id) REFERENCES transactions(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_bond_tranches_maturity ON bond_tranches(maturity_date);
PRAGMA user_version = 9;
"""


class ClosingConnection(sqlite3.Connection):
    """SQLite connection that also closes when used as a context manager."""
    def __exit__(self, exc_type, exc, tb):
        try:
            return super().__exit__(exc_type, exc, tb)
        finally:
            self.close()


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    target = Path(path) if path else DB_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, timeout=30, factory=ClosingConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(path: str | Path | None = None) -> None:
    target = Path(path) if path else DB_PATH
    # Preserve a copy before every schema upgrade (including the metal/FX fields).
    if target.exists() and target.stat().st_size > 0:
        try:
            probe = sqlite3.connect(target)
            version = int(probe.execute("PRAGMA user_version").fetchone()[0])
            has_app_tables = probe.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='transactions'"
            ).fetchone() is not None
            probe.close()
            if has_app_tables and version < 9:
                backup_db(target)
        except sqlite3.DatabaseError:
            # The regular initialization/integrity checks will report a damaged DB.
            pass
    # If this is an existing user database, preserve a copy before adding the
    # predefined instrument set. This only runs when one or more default records
    # are missing; normal Streamlit reruns do not create repeated backups.
    if target.exists() and target.stat().st_size > 0:
        try:
            probe = sqlite3.connect(target)
            has_assets = probe.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='assets'"
            ).fetchone() is not None
            missing_seed = False
            if has_assets:
                from .default_assets import DEFAULT_ASSETS
                existing = {(r[0], r[1]) for r in probe.execute("SELECT symbol,category FROM assets")}
                missing_seed = any((a["symbol"], a["category"]) not in existing for a in DEFAULT_ASSETS)
            probe.close()
            if has_assets and missing_seed:
                backup_db(target)
        except sqlite3.DatabaseError:
            pass
    with connect(target) as conn:
        conn.executescript(SCHEMA)
        # Lightweight additive migrations for local databases created by earlier 0.2 builds.
        snapshot_cols = {r[1] for r in conn.execute("PRAGMA table_info(portfolio_snapshots)").fetchall()}
        if "manual_total_value_pln" not in snapshot_cols:
            conn.execute("ALTER TABLE portfolio_snapshots ADD COLUMN manual_total_value_pln REAL")
        if "manual_notes" not in snapshot_cols:
            conn.execute("ALTER TABLE portfolio_snapshots ADD COLUMN manual_notes TEXT DEFAULT ''")
        if "valuation_complete" not in snapshot_cols:
            conn.execute("ALTER TABLE portfolio_snapshots ADD COLUMN valuation_complete INTEGER NOT NULL DEFAULT 1")
        tx_cols = {r[1] for r in conn.execute("PRAGMA table_info(transactions)").fetchall()}
        if "bond_tranche_purchase_id" not in tx_cols:
            conn.execute("ALTER TABLE transactions ADD COLUMN bond_tranche_purchase_id INTEGER REFERENCES transactions(id)")
        for name, sql_type in (
            ("metal_spot_pln_per_oz", "REAL"), ("metal_adjustment_pln_per_oz", "REAL"),
            ("price_fx_rate_date", "TEXT"), ("price_fx_source", "TEXT"),
            ("cash_fx_rate_date", "TEXT"), ("cash_fx_source", "TEXT"),
            ("fee_fx_rate_date", "TEXT"), ("fee_fx_source", "TEXT"),
        ):
            if name not in tx_cols:
                conn.execute(f"ALTER TABLE transactions ADD COLUMN {name} {sql_type}")
        if "linked_purchase_id" not in tx_cols:
            conn.execute("ALTER TABLE transactions ADD COLUMN linked_purchase_id INTEGER REFERENCES transactions(id) ON DELETE CASCADE")
        if "linked_deposit_follows_purchase" not in tx_cols:
            conn.execute("ALTER TABLE transactions ADD COLUMN linked_deposit_follows_purchase INTEGER NOT NULL DEFAULT 0")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_linked_deposit ON transactions(linked_purchase_id) WHERE linked_purchase_id IS NOT NULL")
        asset_cols = {r[1] for r in conn.execute("PRAGMA table_info(assets)").fetchall()}
        if "bond_compounding" not in asset_cols:
            conn.execute("ALTER TABLE assets ADD COLUMN bond_compounding INTEGER NOT NULL DEFAULT 1")
        conn.execute("CREATE TABLE IF NOT EXISTS staking_plans (id INTEGER PRIMARY KEY AUTOINCREMENT, asset_id INTEGER NOT NULL, source_account_id INTEGER NOT NULL, staking_account_id INTEGER NOT NULL, quantity REAL NOT NULL, start_date TEXT NOT NULL, planned_end_date TEXT, actual_end_date TEXT, apy_pct REAL NOT NULL DEFAULT 0, compounding_frequency TEXT NOT NULL DEFAULT 'daily', active INTEGER NOT NULL DEFAULT 1, lock_transaction_id INTEGER, unlock_transaction_id INTEGER, notes TEXT DEFAULT '', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(asset_id) REFERENCES assets(id), FOREIGN KEY(source_account_id) REFERENCES accounts(id), FOREIGN KEY(staking_account_id) REFERENCES accounts(id), FOREIGN KEY(lock_transaction_id) REFERENCES transactions(id), FOREIGN KEY(unlock_transaction_id) REFERENCES transactions(id))")
        conn.execute("CREATE TABLE IF NOT EXISTS import_batches (id INTEGER PRIMARY KEY AUTOINCREMENT, file_hash TEXT NOT NULL UNIQUE, file_name TEXT NOT NULL, imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, imported_rows INTEGER NOT NULL DEFAULT 0)")
        conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('base_currency','PLN')")
        conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('current_inflation_pct','4.0')")
        conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('current_inflation_confirmed','0')")
        conn.execute("CREATE TABLE IF NOT EXISTS user_notes (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, body TEXT NOT NULL DEFAULT '', asset_id INTEGER, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(asset_id) REFERENCES assets(id) ON DELETE SET NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS calendar_events (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, event_date TEXT NOT NULL, event_type TEXT NOT NULL DEFAULT 'Przypomnienie', asset_id INTEGER, notes TEXT DEFAULT '', completed INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(asset_id) REFERENCES assets(id) ON DELETE SET NULL)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_calendar_events_date ON calendar_events(event_date, completed)")
        conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('last_sync_date','')")
        # Older versions stored bond terms on the instrument. Convert each old BUY
        # into its own tranche so different purchase dates remain independent.
        from .bonds import ensure_legacy_bond_tranches
        ensure_legacy_bond_tranches(conn)
        from .default_assets import seed_default_assets
        seed_default_assets(conn)
        # Retain older EUR-listed instruments and their transactions. Hide only
        # unused legacy listings rather than silently rewriting EUR trades as USD.
        for old_symbol in ('IS3N', 'IS3Q'):
            conn.execute("""UPDATE assets SET active=0 WHERE symbol=? AND category='ETF'
                AND NOT EXISTS (SELECT 1 FROM transactions WHERE asset_id=assets.id OR to_asset_id=assets.id)""", (old_symbol,))
        conn.execute("PRAGMA user_version = 9")
        conn.commit()


def backup_db(path: str | Path | None = None, keep: int = 30) -> Path | None:
    source = Path(path) if path else DB_PATH
    if not source.exists():
        return None
    backup_dir = source.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    target = backup_dir / f"portfolio_{stamp}.sqlite3"
    # SQLite backup API gives a consistent copy even when WAL is active.
    src = sqlite3.connect(source)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    backups = sorted(backup_dir.glob("portfolio_*.sqlite3"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in backups[keep:]:
        old.unlink(missing_ok=True)
    return target


def rows(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    return list(conn.execute(sql, params).fetchall())


def add_account(conn: sqlite3.Connection, name: str, kind: str, notes: str = "") -> int:
    cur = conn.execute("INSERT INTO accounts(name,kind,notes) VALUES(?,?,?)", (name.strip(), kind, notes))
    conn.commit()
    return int(cur.lastrowid)


def add_asset(conn: sqlite3.Connection, data: dict[str, Any]) -> int:
    fields = ["symbol", "name", "category", "currency", "listing_exchange", "price_mode", "price_symbol", "unit",
              "bond_type", "annual_rate_pct", "inflation_margin_pct", "nominal_value", "maturity_date", "bond_compounding", "manual_price", "notes"]
    values = [data.get(k, 1) if k == "bond_compounding" else data.get(k) for k in fields]
    cur = conn.execute(f"INSERT INTO assets({','.join(fields)}) VALUES({','.join('?' for _ in fields)})", values)
    conn.commit()
    return int(cur.lastrowid)


def add_transaction(conn: sqlite3.Connection, data: dict[str, Any], transaction_id: int | None = None) -> int:
    fields = ["occurred_at", "type", "account_id", "to_account_id", "asset_id", "to_asset_id", "quantity", "to_quantity",
              "unit_price", "price_currency", "price_fx_to_pln", "cash_amount", "cash_currency", "cash_fx_to_pln",
              "to_cash_amount", "to_cash_currency", "to_cash_fx_to_pln", "fee_amount", "fee_currency", "fee_fx_to_pln",
              "valuation_pln", "split_ratio", "bond_tranche_purchase_id", "linked_purchase_id", "linked_deposit_follows_purchase",
              "metal_spot_pln_per_oz", "metal_adjustment_pln_per_oz",
              "price_fx_rate_date", "price_fx_source", "cash_fx_rate_date", "cash_fx_source",
              "fee_fx_rate_date", "fee_fx_source", "notes"]
    values = [data.get(k, 0) if k == "linked_deposit_follows_purchase" else data.get(k) for k in fields]
    if transaction_id is None:
        cur = conn.execute(f"INSERT INTO transactions({','.join(fields)}) VALUES({','.join('?' for _ in fields)})", values)
        new_id = int(cur.lastrowid)
    else:
        conn.execute(f"UPDATE transactions SET {','.join(k+'=?' for k in fields)} WHERE id=?", (*values, transaction_id))
        new_id = int(transaction_id)

    tx = conn.execute(
        "SELECT t.type,t.asset_id,a.category FROM transactions t LEFT JOIN assets a ON a.id=t.asset_id WHERE t.id=?",
        (new_id,)).fetchone()
    if not tx or tx["type"] != "BUY" or tx["category"] != "Obligacje":
        conn.execute("DELETE FROM bond_tranches WHERE purchase_transaction_id=?", (new_id,))
    else:
        terms = data.get("bond_terms")
        if terms is not None:
            from .bonds import upsert_bond_tranche
            upsert_bond_tranche(conn, new_id, terms)
        else:
            # For CSV imports and legacy edit paths, copy the instrument's defaults
            # only when this transaction does not already have its own tranche.
            existing = conn.execute("SELECT 1 FROM bond_tranches WHERE purchase_transaction_id=?", (new_id,)).fetchone()
            if existing is None:
                asset = conn.execute("SELECT * FROM assets WHERE id=?", (tx["asset_id"],)).fetchone()
                setting = conn.execute("SELECT value FROM settings WHERE key='current_inflation_pct'").fetchone()
                confirmed = conn.execute("SELECT value FROM settings WHERE key='current_inflation_confirmed'").fetchone()
                assumed = None
                if setting and confirmed and str(confirmed[0]) == "1":
                    try:
                        assumed = float(setting[0])
                    except (TypeError, ValueError):
                        assumed = None
                conn.execute(
                    """INSERT INTO bond_tranches(purchase_transaction_id,bond_type,nominal_value,maturity_date,
                           annual_rate_pct,inflation_margin_pct,assumed_inflation_pct,compounding)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (new_id, asset["bond_type"] or "fixed", float(asset["nominal_value"] or 100),
                     asset["maturity_date"], asset["annual_rate_pct"], asset["inflation_margin_pct"],
                     assumed if (asset["bond_type"] or "fixed") == "inflation" else None,
                     int(asset["bond_compounding"] if asset["bond_compounding"] is not None else 1)))
    return new_id


def delete_transaction(conn: sqlite3.Connection, transaction_id: int) -> None:
    # Deleting a BUY also deletes its linked automatic deposit. A manual deposit
    # without a link is never affected. This operation is wrapped in a savepoint
    # by the UI so ledger/snapshot failures roll back both deletions.
    conn.execute("DELETE FROM transactions WHERE linked_purchase_id=?", (transaction_id,))
    conn.execute("DELETE FROM transactions WHERE id=?", (transaction_id,))


def upsert_price(conn: sqlite3.Connection, asset_id: int, date: str, price: float, currency: str, source: str = "manual") -> None:
    conn.execute("""INSERT INTO price_history(asset_id,price_date,price,currency,source) VALUES(?,?,?,?,?)
        ON CONFLICT(asset_id,price_date,source) DO UPDATE SET price=excluded.price,currency=excluded.currency,updated_at=CURRENT_TIMESTAMP""",
        (asset_id, date, float(price), currency.upper(), source))
    conn.commit()


def upsert_fx(conn: sqlite3.Connection, currency: str, date: str, rate: float, source: str = "NBP") -> None:
    conn.execute("""INSERT INTO fx_history(currency,rate_date,rate_to_pln,source) VALUES(?,?,?,?)
        ON CONFLICT(currency,rate_date,source) DO UPDATE SET rate_to_pln=excluded.rate_to_pln,updated_at=CURRENT_TIMESTAMP""",
        (currency.upper(), date, float(rate), source))
    conn.commit()


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
    conn.commit()


def get_setting(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return str(row[0]) if row else default


def backup_db_if_due(path: str | Path | None = None) -> Path | None:
    """Create at most one automatic backup per calendar day when the app/worker runs."""
    source = Path(path) if path else DB_PATH
    if not source.exists():
        return None
    with connect(source) as conn:
        today = datetime.now().date().isoformat()
        last = conn.execute("SELECT value FROM settings WHERE key='last_auto_backup_date'").fetchone()
        if last and str(last[0]) == today:
            return None
        target = backup_db(source)
        if target is not None:
            conn.execute("INSERT INTO settings(key,value) VALUES('last_auto_backup_date',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (today,))
            conn.commit()
        return target
