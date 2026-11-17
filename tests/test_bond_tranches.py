from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from portfolio.bonds import estimate_tranche, list_bond_tranches, upsert_bond_tranche
from portfolio.db import add_asset, add_transaction, connect, init_db, set_setting
from portfolio.diagnostics import run_diagnostics
from portfolio.ledger import calculate_ledger, value_portfolio
from portfolio.snapshots import rebuild_snapshots, save_snapshot, set_snapshot_override


def make_db(tmp_path):
    path = tmp_path / "portfolio.sqlite3"
    init_db(path)
    conn = connect(path)
    account = int(conn.execute("INSERT INTO accounts(name,kind) VALUES('Broker','broker')").lastrowid)
    return path, conn, account


def create_bond(conn, symbol="COI", **overrides):
    data = {"symbol": symbol, "name": symbol, "category": "Obligacje", "currency": "PLN",
            "bond_type": "fixed", "annual_rate_pct": 4.0, "inflation_margin_pct": 1.0,
            "nominal_value": 100.0, "maturity_date": "2030-01-01", "bond_compounding": 1,
            "price_mode": "auto", "price_symbol": ""}
    data.update(overrides)
    return int(add_asset(conn, data))


def buy(conn, account, asset_id, when, quantity=1.0, terms=None, price=100.0):
    data = {"occurred_at": when, "type": "BUY", "account_id": account, "asset_id": asset_id,
            "quantity": quantity, "unit_price": price, "price_currency": "PLN", "price_fx_to_pln": 1.0,
            "cash_amount": quantity * price, "cash_currency": "PLN", "cash_fx_to_pln": 1.0,
            "fee_amount": 0.0, "notes": ""}
    if terms is not None:
        data["bond_terms"] = terms
    tx_id = int(add_transaction(conn, data))
    conn.commit()
    return tx_id


def fixed_terms(rate, maturity, nominal=100.0, compounding=True):
    return {"bond_type": "fixed", "annual_rate_pct": rate, "inflation_margin_pct": None,
            "assumed_inflation_pct": None, "nominal_value": nominal, "maturity_date": maturity,
            "compounding": compounding}


def inflation_terms(inflation, margin=1.0, maturity="2030-01-01", nominal=100.0):
    return {"bond_type": "inflation", "annual_rate_pct": None, "inflation_margin_pct": margin,
            "assumed_inflation_pct": inflation, "nominal_value": nominal, "maturity_date": maturity,
            "compounding": True}


def test_each_bond_purchase_has_independent_terms_even_on_same_day(tmp_path):
    _, conn, account = make_db(tmp_path)
    asset = create_bond(conn)
    first = buy(conn, account, asset, "2025-01-01", terms=fixed_terms(3.0, "2030-01-01"))
    second = buy(conn, account, asset, "2025-01-01", terms=fixed_terms(7.0, "2028-01-01"))

    tranches = list_bond_tranches(conn, asset_id=asset, as_of="2026-01-01")
    assert {t["purchase_transaction_id"] for t in tranches} == {first, second}
    assert {t["annual_rate_pct"] for t in tranches} == {3.0, 7.0}
    assert {t["maturity_date"] for t in tranches} == {"2030-01-01", "2028-01-01"}
    assert len(tranches) == 2
    conn.close()


def test_inflation_change_recalculates_full_history_for_one_tranche_only(tmp_path):
    _, conn, account = make_db(tmp_path)
    asset = create_bond(conn, bond_type="inflation", annual_rate_pct=None, inflation_margin_pct=1.0)
    first = buy(conn, account, asset, "2025-01-01", terms=inflation_terms(4.0, margin=1.0))
    second = buy(conn, account, asset, "2025-06-01", terms=inflation_terms(3.0, margin=2.0))

    first_before = next(t for t in list_bond_tranches(conn, asset, "2025-07-01") if t["purchase_transaction_id"] == first)
    second_before = next(t for t in list_bond_tranches(conn, asset, "2025-07-01") if t["purchase_transaction_id"] == second)
    value_first_before = estimate_tranche(first_before, "2025-07-01")["current_value"]
    value_second_before = estimate_tranche(second_before, "2025-07-01")["current_value"]

    save_snapshot(conn, "2025-07-01")
    set_snapshot_override(conn, "2025-07-01", 999.0, "Ręczna korekta testowa")
    upsert_bond_tranche(conn, first, inflation_terms(5.0, margin=1.0))
    rebuild_snapshots(conn, "2025-01-01")

    first_after = next(t for t in list_bond_tranches(conn, asset, "2025-07-01") if t["purchase_transaction_id"] == first)
    second_after = next(t for t in list_bond_tranches(conn, asset, "2025-07-01") if t["purchase_transaction_id"] == second)
    value_first_after = estimate_tranche(first_after, "2025-07-01")["current_value"]
    value_second_after = estimate_tranche(second_after, "2025-07-01")["current_value"]
    assert value_first_after > value_first_before
    assert value_second_after == pytest.approx(value_second_before)
    snapshot = conn.execute("SELECT manual_total_value_pln,manual_notes FROM portfolio_snapshots WHERE snapshot_date='2025-07-01'").fetchone()
    assert snapshot["manual_total_value_pln"] == pytest.approx(999.0)
    assert snapshot["manual_notes"] == "Ręczna korekta testowa"
    conn.close()


def test_bond_portfolio_value_is_sum_of_separately_calculated_tranches(tmp_path):
    _, conn, account = make_db(tmp_path)
    asset = create_bond(conn)
    buy(conn, account, asset, "2025-01-01", terms=fixed_terms(2.0, "2030-01-01"))
    buy(conn, account, asset, "2026-01-01", terms=fixed_terms(8.0, "2029-01-01"))
    tranches = list_bond_tranches(conn, asset, "2026-07-01")
    expected_value = sum(estimate_tranche(t, "2026-07-01")["current_value"] for t in tranches)
    position_value = value_portfolio(conn, "2026-07-01")
    bond_row = next(row for row in position_value["valued_positions"] if row["asset_id"] == asset)
    assert bond_row["market_value_pln"] == pytest.approx(expected_value)
    assert bond_row["price_source"] == "bond_model"
    conn.close()


def test_sales_reduce_open_bond_tranches_proportionally_not_fifo(tmp_path):
    _, conn, account = make_db(tmp_path)
    asset = create_bond(conn)
    first = buy(conn, account, asset, "2025-01-01", quantity=2,
                 terms=fixed_terms(3.0, "2030-01-01"))
    second = buy(conn, account, asset, "2025-02-01", quantity=1,
                  terms=fixed_terms(6.0, "2029-01-01"))
    add_transaction(conn, {"occurred_at": "2025-03-01", "type": "SELL", "account_id": account,
        "asset_id": asset, "quantity": 1, "unit_price": 100, "price_currency": "PLN",
        "price_fx_to_pln": 1.0, "cash_amount": 100, "cash_currency": "PLN", "cash_fx_to_pln": 1.0,
        "fee_amount": 0.0, "notes": ""})
    conn.commit()
    tranches = list_bond_tranches(conn, asset, "2025-03-01")
    remaining = {t["purchase_transaction_id"]: t["remaining_quantity"] for t in tranches}
    assert remaining[first] == pytest.approx(4.0 / 3.0)
    assert remaining[second] == pytest.approx(2.0 / 3.0)
    assert sum(remaining.values()) == pytest.approx(calculate_ledger(conn)["global_qty"][asset])
    conn.close()


def test_interest_accrual_stops_at_maturity(tmp_path):
    _, conn, account = make_db(tmp_path)
    asset = create_bond(conn)
    buy(conn, account, asset, "2025-01-01", terms=fixed_terms(5.0, "2026-01-01"))
    tranche = list_bond_tranches(conn, asset)[0]
    at_maturity = estimate_tranche(tranche, "2026-01-01")["current_value"]
    long_after = estimate_tranche(tranche, "2028-01-01")["current_value"]
    assert long_after == pytest.approx(at_maturity)
    conn.close()


def test_legacy_v06_bond_buys_migrate_to_separate_tranches_with_backup(tmp_path):
    path, conn, account = make_db(tmp_path)
    asset = create_bond(conn, bond_type="inflation", annual_rate_pct=None,
                        inflation_margin_pct=1.25, nominal_value=100.0,
                        maturity_date="2030-01-01")
    set_setting(conn, "current_inflation_pct", "4.5")
    set_setting(conn, "current_inflation_confirmed", "1")
    first = buy(conn, account, asset, "2025-01-01", quantity=2)
    second = buy(conn, account, asset, "2025-03-01", quantity=3)
    conn.execute("DROP TABLE bond_tranches")
    conn.execute("PRAGMA user_version=6")
    conn.commit()
    conn.close()

    init_db(path)
    migrated = connect(path)
    tranches = migrated.execute("""SELECT bt.*,t.occurred_at,t.quantity FROM bond_tranches bt
        JOIN transactions t ON t.id=bt.purchase_transaction_id ORDER BY t.occurred_at,t.id""").fetchall()
    assert len(tranches) == 2
    assert [int(r["purchase_transaction_id"]) for r in tranches] == [first, second]
    assert [r["quantity"] for r in tranches] == [2, 3]
    assert [r["assumed_inflation_pct"] for r in tranches] == [4.5, 4.5]
    backups = list((tmp_path / "backups").glob("portfolio_*.sqlite3"))
    assert backups, "A backup should be created before the schema migration"
    assert migrated.execute("PRAGMA user_version").fetchone()[0] == 9
    migrated.close()


def test_diagnostics_warn_when_inflation_assumption_missing_for_open_tranche(tmp_path):
    _, conn, account = make_db(tmp_path)
    asset = create_bond(conn, bond_type="inflation", annual_rate_pct=None,
                        inflation_margin_pct=1.0)
    buy(conn, account, asset, "2025-01-01", terms=inflation_terms(4.0))
    conn.execute("UPDATE bond_tranches SET assumed_inflation_pct=NULL")
    conn.commit()
    report = run_diagnostics(conn)
    assert any("nie ma przyjętej stopy inflacji" in warning for warning in report["warnings"])
    conn.close()


def test_redemption_can_target_only_the_selected_bond_tranche(tmp_path):
    _, conn, account = make_db(tmp_path)
    asset = create_bond(conn)
    first = buy(conn, account, asset, "2025-01-01", quantity=2,
                terms=fixed_terms(3.0, "2026-01-01"))
    second = buy(conn, account, asset, "2025-02-01", quantity=1,
                 terms=fixed_terms(6.0, "2029-01-01"))
    add_transaction(conn, {"occurred_at": "2026-01-01", "type": "SELL", "account_id": account,
        "asset_id": asset, "quantity": 2, "unit_price": 110, "price_currency": "PLN",
        "price_fx_to_pln": 1.0, "cash_amount": 220, "cash_currency": "PLN", "cash_fx_to_pln": 1.0,
        "fee_amount": 0.0, "bond_tranche_purchase_id": first, "notes": "Wykup pierwszej transzy"})
    conn.commit()
    open_tranches = list_bond_tranches(conn, asset_id=asset, as_of="2026-01-01")
    assert len(open_tranches) == 1
    assert open_tranches[0]["purchase_transaction_id"] == second
    assert open_tranches[0]["remaining_quantity"] == pytest.approx(1.0)
    conn.close()


def test_sale_cannot_redeem_more_than_selected_tranche_quantity(tmp_path):
    _, conn, account = make_db(tmp_path)
    asset = create_bond(conn)
    first = buy(conn, account, asset, "2025-01-01", quantity=1,
                terms=fixed_terms(3.0, "2030-01-01"))
    buy(conn, account, asset, "2025-02-01", quantity=2,
        terms=fixed_terms(6.0, "2029-01-01"))
    add_transaction(conn, {"occurred_at": "2025-03-01", "type": "SELL", "account_id": account,
        "asset_id": asset, "quantity": 2, "unit_price": 100, "price_currency": "PLN",
        "price_fx_to_pln": 1.0, "cash_amount": 200, "cash_currency": "PLN", "cash_fx_to_pln": 1.0,
        "fee_amount": 0.0, "bond_tranche_purchase_id": first, "notes": "Błędny wykup"})
    conn.commit()
    from portfolio.ledger import LedgerError
    with pytest.raises(LedgerError, match="przekracza ilość dostępną w wybranej transzy"):
        calculate_ledger(conn)
    conn.close()


def test_csv_import_preserves_tranche_specific_terms(tmp_path):
    from portfolio.data_io import import_transactions_csv
    _, conn, account = make_db(tmp_path)
    asset = create_bond(conn, symbol="EDO", bond_type="fixed")
    csv_data = (
        "occurred_at,type,account_name,asset_symbol,asset_name,quantity,unit_price,price_currency,price_fx_to_pln,"
        "cash_amount,cash_currency,cash_fx_to_pln,tranche_bond_type,tranche_nominal_value,tranche_maturity_date,"
        "tranche_annual_rate_pct,tranche_inflation_margin_pct,tranche_assumed_inflation_pct,tranche_compounding\n"
        "2025-01-01,BUY,Broker,EDO,EDO,2,100,PLN,1,200,PLN,1,fixed,100,2030-01-01,5.5,,,1\n"
    ).encode("utf-8")
    result = import_transactions_csv(conn, csv_data)
    assert result["imported"] == 1
    tranche = list_bond_tranches(conn, asset_id=asset)[0]
    assert tranche["annual_rate_pct"] == pytest.approx(5.5)
    assert tranche["maturity_date"] == "2030-01-01"
    assert tranche["purchased_quantity"] == pytest.approx(2)
    conn.close()


def test_csv_import_remaps_bond_sale_to_correct_purchase_on_another_database(tmp_path):
    from portfolio.data_io import import_transactions_csv

    _, conn, account = make_db(tmp_path)
    asset = create_bond(conn, symbol="EDO")
    # Occupy a destination ID so CSV source IDs cannot accidentally be reused.
    add_transaction(conn, {"occurred_at": "2026-09-30", "type": "DEPOSIT", "account_id": account,
        "cash_amount": 10, "cash_currency": "PLN", "cash_fx_to_pln": 1.0, "fee_amount": 0.0, "notes": "existing"})

    csv_data = (
        "id,occurred_at,type,account_name,asset_symbol,asset_name,quantity,unit_price,price_currency,price_fx_to_pln,"
        "cash_amount,cash_currency,cash_fx_to_pln,fee_amount,bond_tranche_purchase_id,tranche_bond_type,"
        "tranche_nominal_value,tranche_maturity_date,tranche_annual_rate_pct,tranche_compounding\n"
        "10,2026-10-01,BUY,Broker,EDO,EDO,2,100,PLN,1,200,PLN,1,0,,fixed,100,2030-01-01,3,1\n"
        "20,2026-10-02,BUY,Broker,EDO,EDO,1,100,PLN,1,100,PLN,1,0,,fixed,100,2029-01-01,7,1\n"
        "30,2026-10-03,SELL,Broker,EDO,EDO,1,105,PLN,1,105,PLN,1,0,10,,,,,\n"
    ).encode("utf-8")
    result = import_transactions_csv(conn, csv_data)
    assert result["imported"] == 3

    new_purchases = conn.execute("SELECT id FROM transactions WHERE type='BUY' ORDER BY id").fetchall()
    purchase_ids = [int(row[0]) for row in new_purchases]
    assert purchase_ids != [10, 20]
    sale_target = conn.execute("SELECT bond_tranche_purchase_id FROM transactions WHERE type='SELL'").fetchone()[0]
    assert sale_target == purchase_ids[0]
    tranches = list_bond_tranches(conn, asset_id=asset, as_of="2026-10-04")
    remaining = {row["purchase_transaction_id"]: row["remaining_quantity"] for row in tranches}
    assert remaining[purchase_ids[0]] == pytest.approx(1.0)
    assert remaining[purchase_ids[1]] == pytest.approx(1.0)
    conn.close()
