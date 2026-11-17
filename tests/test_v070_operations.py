"""Regression scenarios for dynamic-entry accounting and market references."""
from datetime import date
import sqlite3
import pytest

from portfolio.db import connect, init_db, add_account, add_transaction, upsert_fx, upsert_price
from portfolio.ledger import calculate_ledger, latest_price_with_currency, value_portfolio
from portfolio.metals import TROY_OUNCE_GRAMS, fetch_nbp_gold, fetch_silver_spot
from portfolio.operations import metal_trade_values, settlement_amount, check_currency
from portfolio.fx_quotes import fx_quote


def setup(tmp_path):
    path = tmp_path / 'portfolio.sqlite3'
    init_db(path)
    conn = connect(path)
    account = add_account(conn, 'Test', 'broker')
    assets = {r['symbol']: r['id'] for r in conn.execute('SELECT id,symbol FROM assets')}
    return path, conn, account, assets


def insert_metal(conn, account, asset_id, day, qty, spot, adj, typ='BUY', fee=0):
    breakdown = metal_trade_values(qty, spot, adj, additional_fee=fee)
    add_transaction(conn, dict(type=typ, occurred_at=day, account_id=account,
        asset_id=asset_id, quantity=qty, unit_price=spot, price_currency='PLN',
        price_fx_to_pln=1, cash_amount=breakdown['cash_amount_pln'],
        cash_currency='PLN', cash_fx_to_pln=1, fee_amount=fee,
        fee_currency='PLN', fee_fx_to_pln=1,
        metal_spot_pln_per_oz=spot, metal_adjustment_pln_per_oz=adj,
        price_fx_rate_date=day, price_fx_source='PLN',
        cash_fx_rate_date=day, cash_fx_source='PLN'))
    conn.commit()


def test_metals_are_valued_at_spot_with_immediate_premium_loss(tmp_path):
    path, conn, account, assets = setup(tmp_path)
    upsert_fx(conn, 'USD', '2025-01-01', 4)
    insert_metal(conn, account, assets['SILVER'], '2025-01-01', 1, 200, 20)
    # No external quotes: use transaction's reference price, not 220 paid.
    valued = value_portfolio(conn, '2025-01-01')
    pos = next(p for p in valued['valued_positions'] if p['symbol'] == 'SILVER')
    assert pos['market_value_pln'] == pytest.approx(200)
    assert pos['cost_pln'] == pytest.approx(220)
    assert pos['unrealized_pln'] == pytest.approx(-20)
    assert pos['price_source'] == 'transaction_reference'
    assert pos['avg_cost_asset_currency'] == pytest.approx(55)
    conn.close()


def test_metal_sale_with_positive_adjustment_realizes_110pln(tmp_path):
    _, conn, account, assets = setup(tmp_path)
    upsert_fx(conn, 'USD', '2025-01-01', 4)
    insert_metal(conn, account, assets['SILVER'], '2025-01-01', 1, 200, 20)
    insert_metal(conn, account, assets['SILVER'], '2025-05-01', 1, 300, 30, typ='SELL')
    book = calculate_ledger(conn)
    assert book['realized_pln'] == pytest.approx(110)
    assert not book['positions']
    conn.close()


def test_metal_negative_adjustment_and_separate_fee_not_doubled(tmp_path):
    _, conn, account, assets = setup(tmp_path)
    upsert_fx(conn, 'USD', '2025-01-01', 4)
    insert_metal(conn, account, assets['GOLD'], '2025-01-01', 2, 150, 15, fee=5)
    pos = value_portfolio(conn, '2025-01-01')['valued_positions'][0]
    assert pos['cost_pln'] == pytest.approx(335)
    assert pos['unrealized_pln'] == pytest.approx(-35)
    insert_metal(conn, account, assets['GOLD'], '2025-02-01', 2, 210, -10, typ='SELL')
    assert calculate_ledger(conn)['realized_pln'] == pytest.approx(400 - 335)
    conn.close()


def test_metal_input_validation_and_totals():
    assert metal_trade_values(2, 150, 15)['cash_amount_pln'] == pytest.approx(330)
    assert metal_trade_values(2, 210, -10)['cash_amount_pln'] == pytest.approx(400)
    with pytest.raises(ValueError):
        metal_trade_values(1, 100, -101)
    with pytest.raises(ValueError):
        metal_trade_values(0, 100, 20)
    with pytest.raises(ValueError):
        metal_trade_values(1, float('nan'), 20)


def test_broker_cash_amount_overrides_reference_exchange_and_fx_spread_in_cost(tmp_path):
    _, conn, account, assets = setup(tmp_path)
    upsert_fx(conn, 'EUR', '2025-02-01', 4)
    add_transaction(conn, dict(type='BUY', occurred_at='2025-02-01',
        account_id=account, asset_id=assets['EUNL'], quantity=2,
        unit_price=50, price_currency='EUR', price_fx_to_pln=4,
        cash_amount=460, cash_currency='PLN', cash_fx_to_pln=1,
        fee_amount=5, fee_currency='PLN', fee_fx_to_pln=1))
    pos = calculate_ledger(conn)['positions'][0]
    assert pos['cost_pln'] == pytest.approx(465)
    assert pos['avg_cost_pln'] == pytest.approx(232.5)
    assert pos['avg_cost_asset_currency'] == pytest.approx(465 / 4 / 2)
    assert settlement_amount(2, 50, 'EUR', 'PLN', reference_fx_price=4, reference_fx_settlement=1) == 400
    assert settlement_amount(2, 50, 'EUR', 'PLN', real_amount=460, reference_fx_price=4, reference_fx_settlement=1) == 460
    conn.close()


def test_only_three_new_currencies_and_no_future_fx(tmp_path):
    _, conn, _, _ = setup(tmp_path)
    with pytest.raises(ValueError):
        check_currency('GBP')
    upsert_fx(conn, 'EUR', '2025-01-03', 4.2)
    upsert_fx(conn, 'EUR', '2025-01-07', 4.4)
    quote = fx_quote(conn, 'EUR', '2025-01-05')
    assert quote['rate'] == pytest.approx(4.2)
    assert quote['date'] == '2025-01-03'
    assert fx_quote(conn, 'USD', '2025-01-05') is None
    with pytest.raises(ValueError):
        fx_quote(conn, 'EUR', '2099-01-01')
    conn.close()


def test_nbp_reference_fetches_prior_day_when_no_daily_quote(tmp_path, monkeypatch):
    _, conn, _, _ = setup(tmp_path)
    import portfolio.market as market
    called = []
    def fetch(ccy, start, end):
        called.append((ccy, start, end))
        return [('2025-01-03', 4.25)]
    monkeypatch.setattr(market, 'fetch_nbp_rates', fetch)
    quote = fx_quote(conn, 'EUR', '2025-01-05', fetch_missing=True)
    assert quote['date'] == '2025-01-03'
    assert quote['rate'] == pytest.approx(4.25)
    assert called and called[0][0] == 'EUR'
    conn.close()


def test_metal_quote_in_pln_does_not_get_an_extra_usd_fx_factor(tmp_path):
    _, conn, account, assets = setup(tmp_path)
    upsert_fx(conn, 'USD', '2025-01-01', 4)
    insert_metal(conn, account, assets['GOLD'], '2025-01-01', 1, 200, 20)
    upsert_price(conn, assets['GOLD'], '2025-01-01', 200, 'PLN', 'nbp_gold')
    marked = value_portfolio(conn, '2025-01-01')['valued_positions'][0]
    assert marked['price_currency'] == 'PLN'
    assert marked['market_value_pln'] == pytest.approx(200)
    assert marked['unrealized_pln'] == pytest.approx(-20)
    conn.close()


def test_nbp_gold_per_gram_converts_to_troy_ounces(monkeypatch):
    class Response:
        status_code = 200
        def json(self):return [{'data':'2025-01-02','cena':100.0}]
    class Session:
        def get(self, *args, **kwargs):return Response()
    import portfolio.metals as metals
    monkeypatch.setattr(metals.requests, 'Session', Session)
    assert fetch_nbp_gold(date(2025,1,1),date(2025,1,2)) == [
        ('2025-01-02', pytest.approx(100*TROY_OUNCE_GRAMS))]


def test_silver_spot_reads_ounce_price_not_futures(monkeypatch):
    class Response:
        def raise_for_status(self):pass
        def json(self):return {'currency':'USD','price':50.4,'updatedAt':'2025-01-02T12:00:00Z'}
    import portfolio.metals as metals
    monkeypatch.setattr(metals.requests,'get',lambda *args,**kwargs:Response())
    assert fetch_silver_spot() == ('2025-01-02', 50.4)


def test_migration_copies_existing_data_first(tmp_path):
    path, conn, account, assets = setup(tmp_path)
    add_transaction(conn, {'occurred_at':'2025-01-01','type':'DEPOSIT',
                           'account_id':account,'cash_amount':123,'cash_currency':'PLN',
                           'cash_fx_to_pln':1})
    conn.execute('PRAGMA user_version=7')
    conn.commit()
    conn.close()
    init_db(path)
    with connect(path) as reopened:
        assert reopened.execute('PRAGMA user_version').fetchone()[0] == 9
        assert reopened.execute('SELECT SUM(cash_amount) FROM transactions').fetchone()[0] == 123
        cols = {r[1] for r in reopened.execute('PRAGMA table_info(transactions)')}
        assert {'metal_spot_pln_per_oz','metal_adjustment_pln_per_oz',
                'cash_fx_rate_date','price_fx_source'}.issubset(cols)
    assert list((tmp_path/'backups').glob('*.sqlite3'))
