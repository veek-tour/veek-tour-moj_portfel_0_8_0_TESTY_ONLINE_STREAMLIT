from __future__ import annotations
import csv
import io
import pytest
from datetime import date
from portfolio.db import add_asset, add_transaction, connect, init_db, upsert_fx, upsert_price
from portfolio.ledger import calculate_ledger, latest_fx, value_portfolio, LedgerError
from portfolio.snapshots import save_snapshot
from portfolio.staking import start_staking, end_staking, forecast_staking, bond_forecast
from portfolio.data_io import import_transactions_csv, restore_database_from_bytes
from portfolio.benchmark import benchmark_series


def add_tx(conn, account, asset=None, **kw):
    d = {"occurred_at":"2025-01-01", "type":"DEPOSIT", "account_id":account, "to_account_id":None,
         "asset_id":None, "to_asset_id":None, "quantity":None, "to_quantity":None, "unit_price":None,
         "price_currency":None, "price_fx_to_pln":None, "cash_amount":None, "cash_currency":"PLN",
         "cash_fx_to_pln":1, "to_cash_amount":None, "to_cash_currency":None, "to_cash_fx_to_pln":None,
         "fee_amount":0, "fee_currency":None, "fee_fx_to_pln":None, "valuation_pln":None, "split_ratio":None, "notes":""}
    d.update(kw)
    if asset is not None and "asset_id" not in kw: d["asset_id"] = asset
    add_transaction(conn, d)
    conn.commit()


def setup_db(tmp_path):
    path=tmp_path/"p.sqlite3"; init_db(path); conn=connect(path)
    # Unit-test helper creates its own isolated sample assets; remove the
    # application defaults introduced in v0.6.3 to avoid intentional ticker collisions.
    conn.execute("DELETE FROM assets")
    conn.commit()
    a=conn.execute("INSERT INTO accounts(name,kind) VALUES('Broker','broker')").lastrowid
    st=conn.execute("INSERT INTO accounts(name,kind) VALUES('Staking','staking')").lastrowid
    def asset(sym, cat='ETF', currency='USD', **extra):
        data={"symbol":sym,"name":sym,"category":cat,"currency":currency,"listing_exchange":"","price_mode":"manual","price_symbol":"","unit":"szt.","bond_type":None,"annual_rate_pct":None,"inflation_margin_pct":None,"nominal_value":100,"maturity_date":None,"bond_compounding":1,"notes":""}
        data.update(extra); return add_asset(conn,data)
    return conn,a,st,asset


def test_average_cost_in_instrument_currency(db):
    conn, a1, _, asset, _ = db
    add_tx(conn,a1,asset,type="BUY",quantity=2,cash_amount=100,cash_currency="USD",cash_fx_to_pln=4,
           unit_price=50,price_currency="USD",price_fx_to_pln=4,fee_amount=4,fee_currency="PLN",fee_fx_to_pln=1)
    p=calculate_ledger(conn)["positions"][0]
    assert p["avg_cost_asset_currency"] == pytest.approx(50.5)
    assert p["avg_cost_pln"] == pytest.approx(202)


def test_stablecoin_uses_usd_fx_alias(db):
    conn, a1, _, _, _=db
    upsert_fx(conn,"USD","2025-01-01",4.1,"manual")
    assert latest_fx(conn,"USDT","2025-01-01")==pytest.approx(4.1)
    assert latest_fx(conn,"USDC","2025-01-01")==pytest.approx(4.1)


def test_missing_asset_price_makes_total_unavailable_not_understated(tmp_path):
    conn,a,st,asset_factory=setup_db(tmp_path)
    x=asset_factory("A"); y=asset_factory("B")
    add_tx(conn,a,x,type="BUY",quantity=2,cash_amount=100,cash_currency="PLN",cash_fx_to_pln=1,unit_price=50,price_currency="USD",price_fx_to_pln=4)
    add_tx(conn,a,y,type="BUY",quantity=1,cash_amount=100,cash_currency="PLN",cash_fx_to_pln=1,unit_price=25,price_currency="USD",price_fx_to_pln=4)
    upsert_fx(conn,"USD","2025-01-01",4,"manual"); upsert_price(conn,x,"2025-01-01",60,"USD","manual")
    v=value_portfolio(conn,"2025-01-01")
    assert v["valuation_complete"] is False
    assert v["total_value_pln"] is None
    assert v["partial_total_value_pln"] == pytest.approx(280)
    assert v["unrealized_pln"] is None
    assert v["partial_unrealized_pln"] == pytest.approx(380)


def test_snapshot_marks_partial_valuation(tmp_path):
    conn,a,st,asset_factory=setup_db(tmp_path)
    x=asset_factory("A")
    add_tx(conn,a,x,type="BUY",quantity=1,cash_amount=100,cash_currency="PLN",cash_fx_to_pln=1)
    save_snapshot(conn,"2025-01-01")
    r=conn.execute("SELECT valuation_complete,total_value_pln FROM portfolio_snapshots").fetchone()
    assert r[0]==0


def test_staking_plan_lock_and_unlock(tmp_path):
    conn,a,st,asset_factory=setup_db(tmp_path)
    coin=asset_factory("ETH","Kryptowaluty")
    add_tx(conn,a,coin,type="BUY",quantity=3,cash_amount=300,cash_currency="PLN",cash_fx_to_pln=1,unit_price=100,price_currency="PLN",price_fx_to_pln=1)
    plan_id=start_staking(conn,asset_id=coin,source_account_id=a,staking_account_id=st,quantity=2,start_date="2025-01-02",apy_pct=5,planned_end_date="2026-01-02")
    p=dict(conn.execute("SELECT * FROM staking_plans WHERE id=?",(plan_id,)).fetchone())
    f=forecast_staking(p,date(2025,1,2))
    assert f["projected_reward"] > 0
    l=calculate_ledger(conn)
    assert l["account_assets"][(st,coin)]==pytest.approx(2)
    end_staking(conn,plan_id,"2025-06-01")
    l=calculate_ledger(conn)
    assert l["account_assets"][(st,coin)]==pytest.approx(0)
    assert l["account_assets"][(a,coin)]==pytest.approx(3)


def test_bond_forecast_stops_at_maturity_and_compounds():
    asset={"nominal_value":100,"bond_type":"inflation","inflation_margin_pct":1.5,"maturity_date":"2026-01-01","bond_compounding":1}
    result=bond_forecast(asset,10,4.0,"2024-01-01",date(2025,1,1))
    assert result["assumed_rate_pct"]==pytest.approx(5.5)
    assert result["maturity_estimate_pln"] > result["current_estimate_pln"]
    overdue=bond_forecast(asset,10,4.0,"2024-01-01",date(2027,1,1))
    assert overdue["overdue"] is True
    assert overdue["days_to_maturity"] < 0
    assert overdue["current_estimate_pln"]==pytest.approx(overdue["maturity_estimate_pln"])


def test_csv_import_is_atomic_and_same_file_is_not_imported_twice(tmp_path):
    conn,a,st,asset_factory=setup_db(tmp_path)
    data=("occurred_at,type,account_id,cash_amount,cash_currency,cash_fx_to_pln,fee_amount,notes\n"
          f"2025-01-01,DEPOSIT,{a},100,PLN,1,0,test\n").encode()
    result=import_transactions_csv(conn,data)
    assert result["imported"]==1
    assert import_transactions_csv(conn,data)["duplicate_file"] is True
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]==1


def test_csv_import_rolls_back_if_ledger_invalid(tmp_path):
    conn,a,st,asset_factory=setup_db(tmp_path)
    asset=asset_factory("ABC")
    data=("occurred_at,type,account_id,asset_id,quantity,cash_amount,cash_currency,cash_fx_to_pln\n"
          f"2025-01-01,SELL,{a},{asset},1,100,PLN,1\n").encode()
    with pytest.raises(LedgerError): import_transactions_csv(conn,data)
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]==0


def test_database_restore_validates_and_replaces(tmp_path):
    conn,a,st,asset_factory=setup_db(tmp_path)
    target=tmp_path/"other.sqlite3"; init_db(target); other=connect(target)
    other.execute("INSERT INTO accounts(name,kind) VALUES('Restored','broker')"); other.commit()
    from portfolio.data_io import database_bytes
    payload=database_bytes(other)
    other.close()
    restore_database_from_bytes(conn,payload)
    init_db(tmp_path/"p.sqlite3")  # migration path remains usable after an in-app restore
    assert conn.execute("SELECT name FROM accounts").fetchone()[0]=="Restored"
    with pytest.raises(ValueError): restore_database_from_bytes(conn,b"not a database")


def test_benchmark_series_uses_same_daily_dates(tmp_path):
    conn,a,st,asset_factory=setup_db(tmp_path)
    conn.execute("INSERT INTO transactions(occurred_at,type,account_id,cash_amount,cash_currency,cash_fx_to_pln) VALUES('2025-01-01','DEPOSIT',?,1000,'PLN',1)",(a,))
    conn.execute("INSERT INTO benchmark_history(symbol,price_date,price,currency,source) VALUES('SPY','2025-01-01',100,'USD','manual')")
    conn.execute("INSERT INTO benchmark_history(symbol,price_date,price,currency,source) VALUES('SPY','2025-01-02',110,'USD','manual')")
    upsert_fx(conn,"USD","2025-01-01",4,"manual"); upsert_fx(conn,"USD","2025-01-02",4,"manual")
    conn.execute("INSERT INTO portfolio_snapshots(snapshot_date,assets_value_pln,cash_value_pln,total_value_pln,invested_cost_pln,realized_pln,income_pln,valuation_complete) VALUES('2025-01-01',0,1000,1000,0,0,0,1)")
    conn.execute("INSERT INTO portfolio_snapshots(snapshot_date,assets_value_pln,cash_value_pln,total_value_pln,invested_cost_pln,realized_pln,income_pln,valuation_complete) VALUES('2025-01-02',0,1050,1050,0,0,0,1)")
    conn.commit()
    series=benchmark_series(conn)
    assert len(series)==2
    assert series[-1]["spy_value_pln"]==pytest.approx(1100)


def test_staking_apy_is_effective_annual_yield():
    plan={"start_date":"2025-01-01","planned_end_date":"2026-01-01","quantity":10.0,
          "apy_pct":5.0,"compounding_frequency":"daily"}
    result=forecast_staking(plan,date(2025,1,1))
    assert result["projected_quantity"] == pytest.approx(10.5, rel=1e-5)
    assert result["projected_reward"] == pytest.approx(0.5, rel=1e-5)


def test_csv_import_remaps_account_and_asset_by_name_between_databases(tmp_path):
    conn, a1, _, asset_factory = setup_db(tmp_path)
    asset_id=asset_factory("ABC",name="ABC Corp")
    other_path=tmp_path/"other.sqlite3"
    init_db(other_path)
    other=connect(other_path)
    new_account=other.execute("INSERT INTO accounts(name,kind) VALUES('Broker','broker')").lastrowid
    new_asset=add_asset(other,{"symbol":"ABC","name":"ABC Corp","category":"ETF","currency":"USD","listing_exchange":"","price_mode":"manual","price_symbol":"","unit":"szt."})
    other.commit()
    csv_data=("occurred_at,type,account_id,account_name,asset_id,asset_symbol,asset_name,cash_amount,cash_currency,cash_fx_to_pln,quantity,unit_price,price_currency,price_fx_to_pln\n"
              f"2025-01-01,BUY,{a1},Broker,{asset_id},ABC,ABC Corp,100,USD,4,2,50,USD,4\n").encode()
    result=import_transactions_csv(other,csv_data)
    row=other.execute("SELECT account_id,asset_id FROM transactions").fetchone()
    assert result["imported"]==1
    assert row["account_id"]==new_account
    assert row["asset_id"]==new_asset
    other.close(); conn.close()


def test_date_scoped_ledger_includes_transactions_with_time_on_same_day(db):
    conn, a1, _, asset_factory, _ = db
    add_tx(conn,a1,type="DEPOSIT",cash_amount=100,cash_currency="PLN",cash_fx_to_pln=1,occurred_at="2025-02-01T15:30:00")
    ledger=calculate_ledger(conn,"2025-02-01")
    assert ledger["cash_balances"][(a1,"PLN")] == pytest.approx(100)


def test_fill_missing_snapshots_revisits_incomplete_after_price_arrives(tmp_path):
    from portfolio.snapshots import fill_missing_snapshots
    conn,a,st,asset_factory=setup_db(tmp_path)
    asset=asset_factory("ABC","ETF","USD")
    add_tx(conn,a,asset,type="BUY",quantity=1,cash_amount=100,cash_currency="PLN",cash_fx_to_pln=1)
    save_snapshot(conn,"2025-01-01")
    assert conn.execute("SELECT valuation_complete FROM portfolio_snapshots WHERE snapshot_date='2025-01-01'").fetchone()[0]==0
    upsert_price(conn,asset,"2025-01-01",50,"USD")
    upsert_fx(conn,"USD","2025-01-01",4,"manual")
    fill_missing_snapshots(conn,"2025-01-01")
    row=conn.execute("SELECT valuation_complete,total_value_pln FROM portfolio_snapshots WHERE snapshot_date='2025-01-01'").fetchone()
    assert row[0]==1
    assert row[1]==pytest.approx(100)


def test_external_cashflows_uses_historical_fx_if_transaction_rate_is_missing(db):
    from portfolio.finance import external_cashflows
    conn,a1,_,_,_=db
    upsert_fx(conn,"USD","2025-01-01",4.2,"manual")
    add_tx(conn,a1,type="DEPOSIT",cash_amount=100,cash_currency="USD",cash_fx_to_pln=None,occurred_at="2025-01-01")
    assert external_cashflows(conn)==[("2025-01-01",-420.0)]


def test_manual_snapshot_override_can_enter_portfolio_benchmark_chart(tmp_path):
    from portfolio.snapshots import set_snapshot_override
    conn,a,st,asset_factory=setup_db(tmp_path)
    add_tx(conn,a,type="DEPOSIT",cash_amount=1000,cash_currency="PLN",cash_fx_to_pln=1,occurred_at="2025-01-01")
    save_snapshot(conn,"2025-01-01")  # cash has a valid PLN valuation; snapshot is complete
    conn.execute("UPDATE portfolio_snapshots SET valuation_complete=0 WHERE snapshot_date='2025-01-01'")
    conn.execute("INSERT INTO benchmark_history(symbol,price_date,price,currency,source) VALUES('SPY','2025-01-01',100,'USD','manual')")
    upsert_fx(conn,"USD","2025-01-01",4,"manual")
    conn.commit()
    set_snapshot_override(conn,"2025-01-01",1234.0,"ręczna korekta")
    series=benchmark_series(conn)
    assert len(series)==1
    assert series[0]["portfolio_value_pln"]==pytest.approx(1234)


def test_zero_historical_flow_rate_falls_back_to_nbp_history(db):
    from portfolio.finance import external_cashflows
    conn,a1,_,_,_=db
    upsert_fx(conn,"EUR","2025-03-01",4.3,"manual")
    add_tx(conn,a1,type="DEPOSIT",cash_amount=100,cash_currency="EUR",cash_fx_to_pln=0,occurred_at="2025-03-01")
    assert external_cashflows(conn)==[("2025-03-01",-430.0)]
    valued=value_portfolio(conn)
    assert valued["net_contributions_pln"]==pytest.approx(430)


def test_init_db_migrates_legacy_v02_columns_and_tables(tmp_path):
    import sqlite3
    path=tmp_path/"legacy_v02.sqlite3"
    legacy=sqlite3.connect(path)
    legacy.executescript("""
      CREATE TABLE accounts(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL UNIQUE,kind TEXT NOT NULL DEFAULT 'broker',notes TEXT DEFAULT '',active INTEGER NOT NULL DEFAULT 1,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
      CREATE TABLE assets(id INTEGER PRIMARY KEY AUTOINCREMENT,symbol TEXT NOT NULL,name TEXT NOT NULL,category TEXT NOT NULL,currency TEXT NOT NULL DEFAULT 'PLN',listing_exchange TEXT DEFAULT '',price_mode TEXT NOT NULL DEFAULT 'auto',price_symbol TEXT DEFAULT '',unit TEXT DEFAULT 'szt.',bond_type TEXT DEFAULT '',annual_rate_pct REAL,inflation_margin_pct REAL,nominal_value REAL DEFAULT 100,maturity_date TEXT,manual_price REAL,active INTEGER NOT NULL DEFAULT 1,notes TEXT DEFAULT '',created_at TEXT DEFAULT CURRENT_TIMESTAMP,UNIQUE(symbol,category));
      CREATE TABLE transactions(id INTEGER PRIMARY KEY AUTOINCREMENT,occurred_at TEXT NOT NULL,type TEXT NOT NULL,account_id INTEGER,to_account_id INTEGER,asset_id INTEGER,to_asset_id INTEGER,quantity REAL,to_quantity REAL,unit_price REAL,price_currency TEXT,price_fx_to_pln REAL,cash_amount REAL DEFAULT 0,cash_currency TEXT,cash_fx_to_pln REAL,to_cash_amount REAL,to_cash_currency TEXT,to_cash_fx_to_pln REAL,fee_amount REAL DEFAULT 0,fee_currency TEXT,fee_fx_to_pln REAL,valuation_pln REAL,split_ratio REAL,notes TEXT DEFAULT '',created_at TEXT DEFAULT CURRENT_TIMESTAMP);
      CREATE TABLE portfolio_snapshots(snapshot_date TEXT PRIMARY KEY,assets_value_pln REAL NOT NULL,cash_value_pln REAL NOT NULL,total_value_pln REAL NOT NULL,invested_cost_pln REAL NOT NULL,realized_pln REAL NOT NULL,income_pln REAL NOT NULL,notes TEXT DEFAULT '',manual_total_value_pln REAL,manual_notes TEXT DEFAULT '',created_at TEXT DEFAULT CURRENT_TIMESTAMP);
      CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
      INSERT INTO accounts(name,kind) VALUES('Legacy Broker','broker');
      PRAGMA user_version=2;
    """)
    legacy.commit(); legacy.close()
    init_db(path)
    migrated=connect(path)
    asset_cols={r[1] for r in migrated.execute("PRAGMA table_info(assets)")}
    snap_cols={r[1] for r in migrated.execute("PRAGMA table_info(portfolio_snapshots)")}
    tables={r[0] for r in migrated.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "bond_compounding" in asset_cols
    assert "valuation_complete" in snap_cols
    assert "staking_plans" in tables and "import_batches" in tables
    assert migrated.execute("SELECT value FROM settings WHERE key='current_inflation_confirmed'").fetchone()[0]=="0"
    assert migrated.execute("SELECT name FROM accounts").fetchone()[0]=="Legacy Broker"
    migrated.close()


def test_market_does_not_create_separate_stablecoin_fx_rows(db, monkeypatch):
    from portfolio import market
    conn,a1,_,_,_=db
    add_asset(conn,{"symbol":"USDT","name":"Tether","category":"Kryptowaluty","currency":"USDT","listing_exchange":"","price_mode":"manual","price_symbol":"","unit":"szt."})
    monkeypatch.setattr(market,"fetch_nbp_rates",lambda currency,start,end:[("2025-01-01",4.1)] if currency=="USD" else [])
    result=market.sync_market_data(conn)
    currencies={r[0] for r in conn.execute("SELECT DISTINCT currency FROM fx_history")}
    assert "USD" in currencies
    assert "USDT" not in currencies and "USDC" not in currencies
    assert result["fx_saved"]==1
