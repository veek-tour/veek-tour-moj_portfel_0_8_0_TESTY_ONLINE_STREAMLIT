"""Regression tests for the 0.8 online-only test release.

These scenarios protect the new zero-cost reward, payout separation and
linked cash contributions. They intentionally run without an internet feed.
"""
from datetime import date
from pathlib import Path

import pytest

from portfolio.db import (connect, init_db, add_account, add_transaction,
                          delete_transaction, upsert_fx, upsert_price)
from portfolio.ledger import calculate_ledger, value_portfolio
from portfolio.bonds import estimate_tranche, list_bond_tranches
from portfolio.linked_operations import (purchase_settlement_total, attach_deposit,
                                         sync_deposit_after_purchase_edit,
                                         unlink_deposit_on_manual_edit)


def setup(tmp_path):
    path = tmp_path / 'portfolio.sqlite3'
    init_db(path)
    conn = connect(path)
    account = add_account(conn, 'Mój rachunek', 'broker')
    asset = int(conn.execute("SELECT id FROM assets WHERE symbol='BTC'").fetchone()[0])
    return conn, account, asset


def buy_data(account, asset, *, amount=100.0, fee=5.0):
    return {'occurred_at':'2025-01-01','type':'BUY','account_id':account,'asset_id':asset,
            'quantity':1.0,'unit_price':100.0,'price_currency':'USD','price_fx_to_pln':4.0,
            'cash_amount':amount,'cash_currency':'USD','cash_fx_to_pln':4.0,
            'fee_amount':fee,'fee_currency':'USD','fee_fx_to_pln':4.0}


def test_linked_deposit_is_external_cash_and_preserves_cost(tmp_path):
    conn,account,asset = setup(tmp_path)
    data = buy_data(account,asset)
    purchase = add_transaction(conn,data)
    amount = purchase_settlement_total(dict(conn.execute('SELECT * FROM transactions WHERE id=?',(purchase,)).fetchone()))
    assert amount == pytest.approx(105)
    deposit = attach_deposit(conn,purchase,amount,True)
    ledger = calculate_ledger(conn)
    assert ledger['global_cost'][asset] == pytest.approx(420)
    assert ledger['cash_balances'] == {}
    assert conn.execute('SELECT linked_purchase_id FROM transactions WHERE id=?',(deposit,)).fetchone()[0] == purchase
    assert value_portfolio(conn)['net_contributions_pln'] == pytest.approx(420)
    conn.close()


def test_edit_purchase_updates_following_deposit_and_delete_is_cascade(tmp_path):
    conn,account,asset=setup(tmp_path)
    data=buy_data(account,asset)
    purchase=add_transaction(conn,data)
    dep=attach_deposit(conn,purchase,105,True)
    independent=add_transaction(conn,{'occurred_at':'2025-01-01','type':'DEPOSIT',
        'account_id':account,'cash_amount':8,'cash_currency':'USD','cash_fx_to_pln':4})
    data['cash_amount']=120
    add_transaction(conn,data,purchase)
    sync_deposit_after_purchase_edit(conn,purchase)
    assert conn.execute('SELECT cash_amount FROM transactions WHERE id=?',(dep,)).fetchone()[0] == pytest.approx(125)
    delete_transaction(conn,purchase)
    assert conn.execute('SELECT COUNT(*) FROM transactions WHERE id IN (?,?)',(purchase,dep)).fetchone()[0] == 0
    assert conn.execute('SELECT COUNT(*) FROM transactions WHERE id=?',(independent,)).fetchone()[0] == 1
    conn.close()


def test_custom_linked_deposit_stays_custom_on_edit(tmp_path):
    conn,account,asset=setup(tmp_path)
    purchase=add_transaction(conn,buy_data(account,asset))
    dep=attach_deposit(conn,purchase,60,False)
    edited=buy_data(account,asset,amount=130)
    add_transaction(conn,edited,purchase)
    sync_deposit_after_purchase_edit(conn,purchase)
    assert conn.execute('SELECT cash_amount FROM transactions WHERE id=?',(dep,)).fetchone()[0] == pytest.approx(60)
    unlink_deposit_on_manual_edit(conn,dep,60,55)
    assert conn.execute('SELECT linked_deposit_follows_purchase FROM transactions WHERE id=?',(dep,)).fetchone()[0]==0
    conn.close()


def test_fee_in_other_currency_is_converted_for_linked_deposit(tmp_path):
    tx={'cash_amount':100,'cash_currency':'USD','cash_fx_to_pln':4,
        'fee_amount':10,'fee_currency':'PLN','fee_fx_to_pln':1}
    assert purchase_settlement_total(tx) == pytest.approx(102.5)


def test_free_staking_reward_has_zero_cost_and_only_one_pnl(tmp_path):
    conn,account,asset=setup(tmp_path)
    conn.execute('DELETE FROM price_history')
    upsert_fx(conn,'USD','2025-01-01',4.0)
    upsert_price(conn,asset,'2025-01-01',20.0,'USD')
    add_transaction(conn,{'occurred_at':'2025-01-01','type':'REWARD','account_id':account,
        'asset_id':asset,'quantity':0.125,'unit_price':0.0,'price_currency':'USD',
        'notes':'Nagroda za staking'})
    ledger=calculate_ledger(conn)
    assert ledger['global_qty'][asset] == pytest.approx(0.125)
    assert ledger['global_cost'][asset] == pytest.approx(0)
    assert ledger['income_pln'] == pytest.approx(0)
    value=value_portfolio(conn,'2025-01-01')
    assert value['total_value_pln'] == pytest.approx(10)
    assert value['total_pnl_pln'] == pytest.approx(10)
    conn.close()


def test_bond_linear_estimate_and_linked_interest_prevent_double_count(tmp_path):
    conn,account,_=setup(tmp_path)
    bond = int(conn.execute("SELECT id FROM assets WHERE symbol='OBLIGACJE'").fetchone()[0])
    purchase=add_transaction(conn,{'occurred_at':'2025-01-01','type':'BUY',
        'account_id':account,'asset_id':bond,'quantity':10,'unit_price':100,
        'price_currency':'PLN','price_fx_to_pln':1,'cash_amount':1000,
        'cash_currency':'PLN','cash_fx_to_pln':1,
        'bond_terms':{'bond_type':'fixed','nominal_value':100,
            'maturity_date':'2027-01-01','annual_rate_pct':10.0,'compounding':True}})
    tranche=list_bond_tranches(conn,bond,'2025-07-01')[0]
    estimate=estimate_tranche(tranche,'2025-07-01')
    expected=1000*(1+0.1*181/365.2425)
    assert estimate['compounding'] is False
    assert estimate['current_value']==pytest.approx(expected)
    assert conn.execute('SELECT compounding FROM bond_tranches WHERE purchase_transaction_id=?',(purchase,)).fetchone()[0] == 0
    before=value_portfolio(conn,'2025-07-01')
    assert before['total_value_pln']==pytest.approx(expected-1000)  # historical BUY paid from cash
    add_transaction(conn,{'occurred_at':'2025-07-01','type':'INTEREST','account_id':account,
        'asset_id':bond,'bond_tranche_purchase_id':purchase,
        'cash_amount':30,'cash_currency':'PLN','cash_fx_to_pln':1,
        'notes':'Odsetki rzeczywiście otrzymane'})
    after=value_portfolio(conn,'2025-07-01')
    assert after['total_value_pln']==pytest.approx(before['total_value_pln'])
    assert after['income_pln']==pytest.approx(30)
    conn.close()


def test_new_usd_etfs_keep_original_eur_listing_data_on_upgrade(tmp_path):
    conn, account, asset=setup(tmp_path)
    for ticker, yahoo, isin in [('EIMI','EIMI.L','IE00BKM4GZ66'),
                                ('IWQU','IWQU.L','IE00BP3QZ601')]:
        row=conn.execute('SELECT * FROM assets WHERE symbol=?',(ticker,)).fetchone()
        assert row is not None and row['currency']=='USD' and row['price_symbol']==yahoo
        assert isin in row['notes']
    conn.execute("INSERT INTO assets(symbol,name,category,currency,price_mode,price_symbol) VALUES('IS3N','Stare notowanie EUR','ETF','EUR','auto','IS3N.DE')")
    old=int(conn.execute("SELECT id FROM assets WHERE symbol='IS3N'").fetchone()[0])
    add_transaction(conn,{'occurred_at':'2025-01-01','type':'BUY','account_id':account,
        'asset_id':old,'quantity':1,'unit_price':100,'price_currency':'EUR',
        'price_fx_to_pln':4.3,'cash_amount':100,'cash_currency':'EUR','cash_fx_to_pln':4.3})
    path=Path(conn.execute('PRAGMA database_list').fetchone()[2]); conn.commit();conn.close()
    init_db(path)
    with connect(path) as check:
        row=check.execute("SELECT active,currency,price_symbol FROM assets WHERE id=?",(old,)).fetchone()
        assert row['currency']=='EUR' and row['price_symbol']=='IS3N.DE'
        assert row['active']==1
        assert check.execute('SELECT COUNT(*) FROM transactions WHERE asset_id=?',(old,)).fetchone()[0]==1


def test_crypto_provider_fallback_preserves_usd_quote_and_source(tmp_path,monkeypatch):
    from portfolio import market
    conn,account,_=setup(tmp_path)
    conn.execute("UPDATE assets SET active=0 WHERE category NOT IN ('Kryptowaluty')")
    conn.execute("UPDATE assets SET active=0 WHERE category='Kryptowaluty' AND symbol<>'BIO'")
    conn.commit()
    def unavailable(*_args,**_kwargs):
        raise RuntimeError('Binance chwilowo niedostępny')
    monkeypatch.setattr(market,'fetch_binance_history',unavailable)
    monkeypatch.setattr(market,'fetch_binance_latest',lambda *_:None)
    monkeypatch.setattr(market,'fetch_yahoo_history',lambda symbol,start,end,**kw:
        [(date.today().isoformat(),10.0)] if symbol=='BIO-USD' and start <= date.today() <= end else [])
    monkeypatch.setattr(market,'fetch_yahoo_intraday',lambda symbol:
        (date.today().isoformat(),11.0) if symbol=='BIO-USD' else None)
    monkeypatch.setattr(market,'fetch_nbp_rates',lambda *_:[])
    result=market.sync_market_data(conn,days_back=2,force=True)
    assert result['fallbacks_used']
    row=conn.execute('''SELECT p.price,p.currency,p.source FROM price_history p
                        JOIN assets a ON a.id=p.asset_id WHERE a.symbol='BIO'
                        ORDER BY p.price_date DESC LIMIT 1''').fetchone()
    assert row and row['price']==11.0
    assert row['currency']=='USD' and row['source']=='yahoo_crypto_fallback'
    conn.close()


def test_reward_is_accepted_by_diagnostics(tmp_path):
    from portfolio.diagnostics import run_diagnostics
    conn, account, asset = setup(tmp_path)
    add_transaction(conn, {'occurred_at':'2025-01-01','type':'REWARD','account_id':account,
        'asset_id':asset,'quantity':0.01,'unit_price':0,'price_currency':'USD',
        'notes':'Nagroda staking'})
    result = run_diagnostics(conn)
    assert not any('nieobsługiwany typ' in str(err) for err in result['errors'])
    conn.close()


def test_csv_import_remaps_linked_deposit_in_a_different_database(tmp_path):
    from portfolio.data_io import import_transactions_csv
    conn, account, asset = setup(tmp_path)
    conn.execute('DELETE FROM transactions')
    # Occupy a destination row ID so that original IDs cannot be trusted.
    add_transaction(conn, {'occurred_at':'2024-12-01','type':'DEPOSIT','account_id':account,
        'cash_amount':1,'cash_currency':'PLN','cash_fx_to_pln':1})
    content = ("id,occurred_at,type,account_name,asset_symbol,asset_name,quantity,unit_price,price_currency,price_fx_to_pln,"
        "cash_amount,cash_currency,cash_fx_to_pln,fee_amount,linked_purchase_id,linked_deposit_follows_purchase\n"
        "10,2025-01-01,BUY,Mój rachunek,BTC,Bitcoin (BTC),1,100,USD,4,100,USD,4,5,,\n"
        "20,2025-01-01,DEPOSIT,Mój rachunek,,,,,,,105,USD,4,,10,1\n").encode('utf-8')
    result = import_transactions_csv(conn, content)
    assert result['imported']==2
    purchase=conn.execute("SELECT id FROM transactions WHERE type='BUY'").fetchone()[0]
    linked=conn.execute('SELECT linked_purchase_id,linked_deposit_follows_purchase FROM transactions WHERE type=\'DEPOSIT\' AND linked_purchase_id IS NOT NULL').fetchone()
    assert tuple(linked)==(purchase,1)
    assert calculate_ledger(conn)['cash_balances'] != {}
    conn.close()
