import pytest
from portfolio.db import add_transaction, upsert_price, upsert_fx
from portfolio.ledger import calculate_ledger, value_portfolio, LedgerError
from portfolio.finance import xirr
from portfolio.snapshots import save_snapshot, rebuild_snapshots


def tx(conn, **kwargs):
    base = {"occurred_at":"2025-01-01", "type":"DEPOSIT", "account_id":1, "to_account_id":None,
            "asset_id":None, "to_asset_id":None, "quantity":None, "to_quantity":None, "unit_price":None,
            "price_currency":None, "price_fx_to_pln":None, "cash_amount":None, "cash_currency":None,
            "cash_fx_to_pln":None, "to_cash_amount":None, "to_cash_currency":None, "to_cash_fx_to_pln":None,
            "fee_amount":0, "fee_currency":None, "fee_fx_to_pln":None, "valuation_pln":None, "split_ratio":None, "notes":""}
    base.update(kwargs)
    add_transaction(conn, base)
    conn.commit()


def test_weighted_average_and_partial_sale(db):
    conn, a1, _, asset, _ = db
    tx(conn, type="BUY", account_id=a1, asset_id=asset, quantity=10, cash_amount=1000, cash_currency="PLN", cash_fx_to_pln=1)
    tx(conn, occurred_at="2025-02-01", type="BUY", account_id=a1, asset_id=asset, quantity=10, cash_amount=1400, cash_currency="PLN", cash_fx_to_pln=1)
    tx(conn, occurred_at="2025-03-01", type="SELL", account_id=a1, asset_id=asset, quantity=5, cash_amount=800, cash_currency="PLN", cash_fx_to_pln=1)
    ledger = calculate_ledger(conn)
    assert ledger["global_qty"][asset] == pytest.approx(15)
    assert ledger["global_cost"][asset] == pytest.approx(1800)
    assert ledger["realized_pln"] == pytest.approx(200)


def test_fx_is_embedded_in_acquisition_cost(db):
    conn, a1, _, asset, _ = db
    tx(conn, type="BUY", account_id=a1, asset_id=asset, quantity=2, cash_amount=100, cash_currency="USD", cash_fx_to_pln=4.0)
    ledger = calculate_ledger(conn)
    assert ledger["global_cost"][asset] == pytest.approx(400)
    assert ledger["positions"][0]["avg_cost_pln"] == pytest.approx(200)


def test_buy_fees_increase_cost_basis(db):
    conn, a1, _, asset, _ = db
    tx(conn, type="BUY", account_id=a1, asset_id=asset, quantity=1, cash_amount=100, cash_currency="PLN", cash_fx_to_pln=1,
       fee_amount=5, fee_currency="PLN", fee_fx_to_pln=1)
    assert calculate_ledger(conn)["global_cost"][asset] == pytest.approx(105)


def test_transfer_preserves_global_cost_and_account_balances(db):
    conn, a1, a2, asset, _ = db
    tx(conn, type="BUY", account_id=a1, asset_id=asset, quantity=3, cash_amount=300, cash_currency="PLN", cash_fx_to_pln=1)
    tx(conn, occurred_at="2025-01-02", type="ASSET_TRANSFER", account_id=a1, to_account_id=a2, asset_id=asset, quantity=2)
    ledger = calculate_ledger(conn)
    assert ledger["global_qty"][asset] == pytest.approx(3)
    assert ledger["global_cost"][asset] == pytest.approx(300)
    assert ledger["account_assets"][(a1, asset)] == pytest.approx(1)
    assert ledger["account_assets"][(a2, asset)] == pytest.approx(2)


def test_split_is_explicitly_not_supported(db):
    conn, a1, _, asset, _ = db
    tx(conn, type="BUY", account_id=a1, asset_id=asset, quantity=4, cash_amount=400, cash_currency="PLN", cash_fx_to_pln=1)
    tx(conn, occurred_at="2025-01-02", type="SPLIT", account_id=a1, asset_id=asset, split_ratio=2)
    with pytest.raises(LedgerError, match="Obsługa splitów została wyłączona"):
        calculate_ledger(conn)


def test_staking_reward_adds_position_and_income(db):
    conn, a1, a2, asset, _ = db
    tx(conn, type="STAKING_REWARD", account_id=a2, asset_id=asset, quantity=0.5, unit_price=200, price_currency="PLN", price_fx_to_pln=1)
    ledger = calculate_ledger(conn)
    assert ledger["global_qty"][asset] == pytest.approx(0.5)
    assert ledger["global_cost"][asset] == pytest.approx(100)
    assert ledger["income_pln"] == pytest.approx(100)


def test_swap_has_no_phantom_cash_and_moves_value(db):
    conn, a1, _, asset, asset2 = db
    tx(conn, type="DEPOSIT", account_id=a1, cash_amount=200, cash_currency="PLN", cash_fx_to_pln=1)
    tx(conn, occurred_at="2025-01-01", type="BUY", account_id=a1, asset_id=asset, quantity=2, cash_amount=200, cash_currency="PLN", cash_fx_to_pln=1)
    tx(conn, occurred_at="2025-01-02", type="SWAP", account_id=a1, asset_id=asset, to_asset_id=asset2,
       quantity=1, to_quantity=4, valuation_pln=150)
    ledger = calculate_ledger(conn)
    assert ledger["global_qty"][asset] == pytest.approx(1)
    assert ledger["global_qty"][asset2] == pytest.approx(4)
    assert ledger["global_cost"][asset2] == pytest.approx(150)
    assert ledger["cash_balances"] == {}
    assert ledger["realized_pln"] == pytest.approx(50)


def test_overselling_is_rejected(db):
    conn, a1, _, asset, _ = db
    tx(conn, type="BUY", account_id=a1, asset_id=asset, quantity=1, cash_amount=100, cash_currency="PLN", cash_fx_to_pln=1)
    tx(conn, occurred_at="2025-01-02", type="SELL", account_id=a1, asset_id=asset, quantity=2, cash_amount=200, cash_currency="PLN", cash_fx_to_pln=1)
    with pytest.raises(LedgerError):
        calculate_ledger(conn)


def test_dividend_included_in_income_and_cash_and_assigned_to_asset(db):
    conn, a1, _, asset, _ = db
    tx(conn, type="DIVIDEND", account_id=a1, asset_id=asset, cash_amount=20, cash_currency="PLN", cash_fx_to_pln=1)
    ledger = calculate_ledger(conn)
    assert ledger["income_pln"] == pytest.approx(20)
    assert ledger["income_by_asset"][asset] == pytest.approx(20)
    assert ledger["cash_balances"][(a1, "PLN")] == pytest.approx(20)


@pytest.mark.parametrize("operation_type", ["DIVIDEND", "INTEREST"])
def test_income_operations_require_asset_assignment(db, operation_type):
    conn, a1, _, _, _ = db
    tx(conn, type=operation_type, account_id=a1, cash_amount=20, cash_currency="PLN", cash_fx_to_pln=1)
    with pytest.raises(LedgerError, match="wymaga przypisania do konkretnego aktywa"):
        calculate_ledger(conn)


def test_cash_transfers_are_not_external_performance_flows(db):
    conn, a1, a2, _, _ = db
    tx(conn, type="DEPOSIT", account_id=a1, cash_amount=100, cash_currency="PLN", cash_fx_to_pln=1)
    tx(conn, occurred_at="2025-01-02", type="CASH_TRANSFER", account_id=a1, to_account_id=a2, cash_amount=40, cash_currency="PLN")
    ledger = calculate_ledger(conn)
    assert ledger["cash_balances"][(a1, "PLN")] == pytest.approx(60)
    assert ledger["cash_balances"][(a2, "PLN")] == pytest.approx(40)


def test_manual_price_and_fx_value_position(db):
    conn, a1, _, asset, _ = db
    tx(conn, type="BUY", account_id=a1, asset_id=asset, quantity=2, cash_amount=100, cash_currency="USD", cash_fx_to_pln=4)
    upsert_price(conn, asset, "2025-01-01", 70, "USD", "manual")
    upsert_fx(conn, "USD", "2025-01-01", 4.1, "manual")
    value = value_portfolio(conn, "2025-01-01")
    assert value["assets_value_pln"] == pytest.approx(574)
    assert value["unrealized_pln"] == pytest.approx(174)


def test_snapshot_can_be_rebuilt_after_manual_price_edit(db):
    conn, a1, _, asset, _ = db
    tx(conn, type="BUY", account_id=a1, asset_id=asset, quantity=1, cash_amount=100, cash_currency="PLN", cash_fx_to_pln=1)
    upsert_price(conn, asset, "2025-01-01", 100, "USD", "manual")
    upsert_fx(conn, "USD", "2025-01-01", 4, "manual")
    save_snapshot(conn, "2025-01-01")
    upsert_price(conn, asset, "2025-01-01", 110, "USD", "manual")
    rebuild_snapshots(conn, "2025-01-01", "2025-01-01")
    row = conn.execute("SELECT assets_value_pln FROM portfolio_snapshots WHERE snapshot_date='2025-01-01'").fetchone()
    assert row[0] == pytest.approx(440)


def test_xirr_one_year_ten_percent():
    result = xirr([("2024-01-01", -1000), ("2025-01-01", 1100)])
    assert result == pytest.approx(0.10, abs=0.001)


def test_foreign_currency_cash_revaluation_is_in_total_result(db):
    conn, a1, _, _, _ = db
    tx(conn, type="DEPOSIT", account_id=a1, cash_amount=100, cash_currency="USD", cash_fx_to_pln=4.0)
    upsert_fx(conn, "USD", "2025-01-01", 4.2, "manual")
    value = value_portfolio(conn, "2025-01-01")
    assert value["cash_value_pln"] == pytest.approx(420)
    assert value["net_contributions_pln"] == pytest.approx(400)
    assert value["total_pnl_pln"] == pytest.approx(20)


def test_snapshot_manual_override_survives_rebuild(db):
    from portfolio.snapshots import set_snapshot_override
    conn, a1, _, _, _ = db
    tx(conn, type="DEPOSIT", account_id=a1, cash_amount=100, cash_currency="PLN", cash_fx_to_pln=1)
    save_snapshot(conn, "2025-01-01")
    set_snapshot_override(conn, "2025-01-01", 125, "Korekta ręczna")
    rebuild_snapshots(conn, "2025-01-01", "2025-01-01")
    row = conn.execute("SELECT total_value_pln,manual_total_value_pln,manual_notes FROM portfolio_snapshots WHERE snapshot_date='2025-01-01'").fetchone()
    assert row[0] == pytest.approx(100)
    assert row[1] == pytest.approx(125)
    assert row[2] == "Korekta ręczna"


def test_cash_fee_reduces_portfolio_cash_and_is_reported(db):
    conn, a1, _, _, _ = db
    tx(conn, type="DEPOSIT", account_id=a1, cash_amount=100, cash_currency="PLN", cash_fx_to_pln=1)
    tx(conn, occurred_at="2025-01-02", type="FEE", account_id=a1, cash_amount=5, cash_currency="PLN", cash_fx_to_pln=1)
    ledger = calculate_ledger(conn)
    assert ledger["cash_balances"][(a1, "PLN")] == pytest.approx(95)
    assert ledger["fees_pln"] == pytest.approx(5)


def test_spy_benchmark_uses_external_flows_in_pln(db):
    from portfolio.benchmark import benchmark_value
    conn, a1, _, _, _ = db
    tx(conn, occurred_at="2025-01-01", type="DEPOSIT", account_id=a1, cash_amount=1000, cash_currency="PLN", cash_fx_to_pln=1)
    conn.execute("INSERT INTO benchmark_history(symbol,price_date,price,currency,source) VALUES('SPY','2025-01-01',100,'USD','yahoo')")
    conn.execute("INSERT INTO benchmark_history(symbol,price_date,price,currency,source) VALUES('SPY','2025-01-02',120,'USD','yahoo')")
    upsert_fx(conn, "USD", "2025-01-01", 4, "manual")
    upsert_fx(conn, "USD", "2025-01-02", 4.5, "manual")
    result = benchmark_value(conn, "2025-01-02")
    assert result["units"] == pytest.approx(2.5)
    assert result["value_pln"] == pytest.approx(1350)


def test_snapshot_rebuild_can_participate_in_outer_transaction(db):
    conn, a1, _, _, _ = db
    draft = {"occurred_at":"2025-01-01", "type":"DEPOSIT", "account_id":a1,
             "cash_amount":100, "cash_currency":"PLN", "cash_fx_to_pln":1,
             "fee_amount":0, "notes":""}
    add_transaction(conn, draft)
    rebuild_snapshots(conn, "2025-01-01", "2025-01-01", commit=False)
    assert conn.execute("SELECT COUNT(*) FROM portfolio_snapshots").fetchone()[0] == 1
    conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM portfolio_snapshots").fetchone()[0] == 0


def test_benchmark_series_carries_forward_quotes_in_single_pass(db):
    from portfolio.benchmark import benchmark_series, benchmark_value
    conn, a1, _, _, _ = db
    tx(conn, occurred_at="2025-01-01", type="DEPOSIT", account_id=a1,
       cash_amount=1000, cash_currency="PLN", cash_fx_to_pln=1)
    tx(conn, occurred_at="2025-01-02", type="WITHDRAWAL", account_id=a1,
       cash_amount=200, cash_currency="PLN", cash_fx_to_pln=1)
    conn.executemany(
        "INSERT INTO benchmark_history(symbol,price_date,price,currency,source) VALUES('SPY',?,?,'USD','yahoo')",
        [("2025-01-01", 100), ("2025-01-03", 110)],
    )
    upsert_fx(conn, "USD", "2025-01-01", 4.0, "manual")
    upsert_fx(conn, "USD", "2025-01-03", 4.2, "manual")
    conn.executemany(
        "INSERT INTO portfolio_snapshots(snapshot_date,assets_value_pln,cash_value_pln,total_value_pln,invested_cost_pln,realized_pln,income_pln,valuation_complete) VALUES(?,?,?, ?,0,0,0,1)",
        [("2025-01-01", 1000, 0, 1000), ("2025-01-02", 900, 0, 900), ("2025-01-03", 950, 0, 950)],
    )
    series = benchmark_series(conn, "2025-01-01", "2025-01-03")
    assert [row["spy_value_pln"] for row in series] == pytest.approx([1000, 800, 924])
    assert benchmark_value(conn, "2025-01-02")["value_pln"] == pytest.approx(800)
