from portfolio.db import add_transaction, backup_db, rows
from portfolio.ledger import calculate_ledger


def test_transaction_can_be_edited_and_replayed(db):
    conn, a1, _, asset, _ = db
    data = {"occurred_at":"2025-01-01", "type":"BUY", "account_id":a1, "asset_id":asset,
            "quantity":1, "cash_amount":100, "cash_currency":"PLN", "cash_fx_to_pln":1,
            "fee_amount":0, "notes":"test"}
    tx_id = add_transaction(conn, data)
    conn.commit()
    assert calculate_ledger(conn)["global_cost"][asset] == 100
    data["cash_amount"] = 125
    add_transaction(conn, data, tx_id)
    conn.commit()
    assert calculate_ledger(conn)["global_cost"][asset] == 125


def test_backup_creates_sqlite_copy(db, tmp_path):
    conn, *_ = db
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    backup = backup_db(path)
    assert backup is not None and backup.exists()
