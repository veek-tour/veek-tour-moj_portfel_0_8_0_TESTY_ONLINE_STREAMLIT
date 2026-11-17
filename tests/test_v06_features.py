from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import pytest

from portfolio.db import add_asset, add_transaction, backup_db_if_due, connect, init_db, set_setting, upsert_fx
from portfolio.ledger import calculate_ledger, latest_price, value_portfolio
from portfolio.diagnostics import run_diagnostics
from portfolio.analytics import portfolio_history_series
from portfolio.data_io import import_transactions_csv
from portfolio.market import _recently_updated, _setting_updated_within
from portfolio.snapshots import save_snapshot
from portfolio.bonds import list_bond_tranches, upsert_bond_tranche


def add_tx(conn, account, **values):
    data = {
        "occurred_at": "2025-01-01", "type": "DEPOSIT", "account_id": account,
        "to_account_id": None, "asset_id": None, "to_asset_id": None,
        "quantity": None, "to_quantity": None, "unit_price": None,
        "price_currency": None, "price_fx_to_pln": None, "cash_amount": 100.0,
        "cash_currency": "PLN", "cash_fx_to_pln": 1.0, "to_cash_amount": None,
        "to_cash_currency": None, "to_cash_fx_to_pln": None, "fee_amount": 0.0,
        "fee_currency": None, "fee_fx_to_pln": None, "valuation_pln": None,
        "split_ratio": None, "notes": "",
    }
    data.update(values)
    add_transaction(conn, data)
    conn.commit()


def make_db(tmp_path):
    db_path = tmp_path / "portfolio.sqlite3"
    init_db(db_path)
    conn = connect(db_path)
    # These tests create their own instruments; seeded app defaults would trigger live quote downloads.
    conn.execute("DELETE FROM assets")
    conn.commit()
    account = conn.execute("INSERT INTO accounts(name,kind) VALUES('Broker','broker')").lastrowid
    return db_path, conn, int(account)


def test_notes_and_calendar_tables_are_created_by_migration(tmp_path):
    _, conn, account = make_db(tmp_path)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"user_notes", "calendar_events"}.issubset(tables)
    conn.execute("INSERT INTO user_notes(title,body) VALUES('Plan','DCA')")
    conn.execute("INSERT INTO calendar_events(title,event_date) VALUES('Wykup','2026-12-01')")
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM user_notes").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM calendar_events").fetchone()[0] == 1
    conn.close()


def test_daily_backup_is_created_only_once_per_day(tmp_path):
    db_path, conn, account = make_db(tmp_path)
    add_tx(conn, account)
    conn.close()
    backup = backup_db_if_due(db_path)
    assert backup is not None and backup.exists()
    assert backup_db_if_due(db_path) is None


def test_diagnostics_detect_invalid_ledger_entry(tmp_path):
    _, conn, account = make_db(tmp_path)
    add_tx(conn, account, type="WITHDRAWAL", cash_amount=1000.0)
    report = run_diagnostics(conn)
    assert report["ok"] is True
    assert any("Ujemne saldo" in message for message in report["warnings"])
    conn.close()


def test_diagnostics_reports_database_integrity_on_empty_database(tmp_path):
    _, conn, _ = make_db(tmp_path)
    report = run_diagnostics(conn)
    assert report["ok"] is True
    assert any(item["Kontrola"] == "Integralność bazy SQLite" for item in report["checks"])
    conn.close()


def test_asset_income_is_attributed_to_the_instrument(tmp_path):
    _, conn, account = make_db(tmp_path)
    asset_id = add_asset(conn, {"symbol": "ABC", "name": "ABC", "category": "ETF", "currency": "PLN",
                                "price_mode": "auto", "price_symbol": "ABC"})
    add_tx(conn, account, type="DIVIDEND", asset_id=asset_id, cash_amount=12.5, cash_currency="PLN", cash_fx_to_pln=1)
    add_tx(conn, account, type="INTEREST", asset_id=asset_id, cash_amount=7.5, cash_currency="PLN", cash_fx_to_pln=1)
    result = calculate_ledger(conn)
    assert result["income_by_asset"][asset_id] == pytest.approx(20.0)
    conn.close()


def test_bond_value_uses_recorded_rate_without_market_quote(tmp_path):
    _, conn, account = make_db(tmp_path)
    bond_id = add_asset(conn, {"symbol": "BOND", "name": "Bond", "category": "Obligacje", "currency": "PLN",
                               "bond_type": "fixed", "annual_rate_pct": 5.0, "nominal_value": 100.0,
                               "maturity_date": "2027-01-01", "bond_compounding": 1,
                               "price_mode": "auto", "price_symbol": ""})
    add_tx(conn, account, occurred_at="2025-01-01", type="BUY", asset_id=bond_id, quantity=2,
           unit_price=100, price_currency="PLN", price_fx_to_pln=1,
           cash_amount=200, cash_currency="PLN", cash_fx_to_pln=1)
    value = value_portfolio(conn, "2025-07-01")
    bond = next(p for p in value["valued_positions"] if p["asset_id"] == bond_id)
    assert bond["price_source"] == "bond_model"
    assert bond["price"] > 100
    assert bond["market_value_pln"] > 200
    assert value["valuation_complete"] is True
    conn.close()


def test_inflation_bond_requires_one_confirmed_inflation_assumption(tmp_path):
    _, conn, account = make_db(tmp_path)
    bond_id = add_asset(conn, {"symbol": "COI", "name": "COI", "category": "Obligacje", "currency": "PLN",
                               "bond_type": "inflation", "inflation_margin_pct": 1.5, "nominal_value": 100,
                               "maturity_date": "2027-01-01", "price_mode": "auto", "price_symbol": ""})
    add_tx(conn, account, type="BUY", asset_id=bond_id, quantity=1, unit_price=100, price_currency="PLN",
           price_fx_to_pln=1, cash_amount=100, cash_currency="PLN", cash_fx_to_pln=1)
    before = value_portfolio(conn)
    assert before["valuation_complete"] is False
    # A global setting change must not silently overwrite a tranche's own assumption.
    set_setting(conn, "current_inflation_pct", "4.0")
    set_setting(conn, "current_inflation_confirmed", "1")
    assert value_portfolio(conn)["valuation_complete"] is False
    tranche = list_bond_tranches(conn)[0]
    upsert_bond_tranche(conn, int(tranche["purchase_transaction_id"]), {
        "bond_type": "inflation", "annual_rate_pct": None, "inflation_margin_pct": 1.5,
        "assumed_inflation_pct": 4.0, "nominal_value": 100,
        "maturity_date": "2027-01-01", "compounding": True})
    after = value_portfolio(conn)
    assert after["valuation_complete"] is True
    conn.close()


def test_csv_import_works_with_account_name_without_account_id(tmp_path):
    _, conn, account = make_db(tmp_path)
    csv_data = b"occurred_at,type,account_name,cash_amount,cash_currency,cash_fx_to_pln\n2025-01-01,DEPOSIT,Broker,250,PLN,1\n"
    result = import_transactions_csv(conn, csv_data)
    assert result["imported"] == 1
    tx = conn.execute("SELECT account_id,cash_amount FROM transactions").fetchone()
    assert tx["account_id"] == account
    assert tx["cash_amount"] == pytest.approx(250)
    conn.close()


def test_history_chart_has_portfolio_and_net_contribution_lines(tmp_path):
    _, conn, account = make_db(tmp_path)
    add_tx(conn, account, occurred_at="2025-01-01", type="DEPOSIT", cash_amount=1000,
           cash_currency="PLN", cash_fx_to_pln=1)
    save_snapshot(conn, "2025-01-01")
    add_tx(conn, account, occurred_at="2025-02-01", type="WITHDRAWAL", cash_amount=200,
           cash_currency="PLN", cash_fx_to_pln=1)
    save_snapshot(conn, "2025-02-01")
    series = portfolio_history_series(conn)
    assert series["complete"] is True
    assert series["series"][0]["net_contributions_pln"] == pytest.approx(1000)
    assert series["series"][1]["net_contributions_pln"] == pytest.approx(800)
    conn.close()


def test_crypto_recent_update_schedule_uses_six_hour_window(tmp_path):
    _, conn, account = make_db(tmp_path)
    asset_id = add_asset(conn, {"symbol": "BTC", "name": "Bitcoin", "category": "Kryptowaluty", "currency": "USD",
                                "price_mode": "auto", "price_symbol": "BTC-USD"})
    conn.execute("INSERT INTO price_history(asset_id,price_date,price,currency,source) VALUES(?,?,?,?,?)",
                 (asset_id, date.today().isoformat(), 50000, "USD", "yahoo"))
    conn.commit()
    assert _recently_updated(conn, asset_id, 6) is True
    conn.execute("UPDATE price_history SET updated_at='2000-01-01 00:00:00' WHERE asset_id=?", (asset_id,))
    conn.commit()
    assert _recently_updated(conn, asset_id, 6) is False
    conn.close()


def test_price_refresh_window_compares_sqlite_utc_timestamps_correctly(tmp_path):
    _, conn, _ = make_db(tmp_path)
    asset_id = add_asset(conn, {"symbol": "BTC", "name": "Bitcoin", "category": "Kryptowaluty", "currency": "USD",
                                "price_mode": "auto", "price_symbol": "BTC-USD"})
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("INSERT INTO price_history(asset_id,price_date,price,currency,source,updated_at) VALUES(?,?,?,?,?,?)",
                 (asset_id, date.today().isoformat(), 50000, "USD", "yahoo", stamp))
    conn.commit()
    assert _recently_updated(conn, asset_id, 6) is True
    conn.close()


def test_currency_refresh_schedule_is_limited_to_once_daily(tmp_path):
    _, conn, _ = make_db(tmp_path)
    now = datetime.now(timezone.utc)
    set_setting(conn, "last_fx_sync_EUR", now.isoformat(timespec="seconds"))
    assert _setting_updated_within(conn, "last_fx_sync_EUR", 24) is True
    set_setting(conn, "last_fx_sync_EUR", (now - timedelta(hours=25)).isoformat(timespec="seconds"))
    assert _setting_updated_within(conn, "last_fx_sync_EUR", 24) is False
    conn.close()


def test_spy_refresh_schedule_is_limited_to_once_daily(tmp_path, monkeypatch):
    from portfolio import benchmark

    _, conn, _ = make_db(tmp_path)
    calls = []
    monkeypatch.setattr(benchmark, "fetch_yahoo_history", lambda *args, **kwargs: calls.append(1) or [(date.today().isoformat(), 500.0)])
    assert benchmark.sync_spy(conn) == 1
    assert benchmark.sync_spy(conn) == 0
    assert len(calls) == 1
    conn.close()


def test_undated_legacy_manual_price_is_not_used_as_current_quote(tmp_path):
    _, conn, _ = make_db(tmp_path)
    asset_id = add_asset(conn, {"symbol": "XYZ", "name": "Old manual quote", "category": "ETF", "currency": "PLN",
                                "price_mode": "auto", "price_symbol": "XYZ", "manual_price": 123.45})
    assert latest_price(conn, asset_id, "PLN") == (None, None, None)
    conn.close()
