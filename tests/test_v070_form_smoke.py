"""Headless smoke checks for dynamic Streamlit form branches.

These mock widgets check Python execution, *not* visual/browser interaction.
"""
from datetime import date
from contextlib import nullcontext
from types import SimpleNamespace
import importlib
import sys

import pytest

from portfolio.db import connect, init_db, add_account, upsert_fx


class FakeUI:
    def __init__(self, typ, asset_id):
        self.typ = typ
        self.asset_id = asset_id
        self.session_state = {}
        self.messages = []
    def __enter__(self):return self
    def __exit__(self, *_):return False
    def expander(self, *_a, **_k):return self
    def columns(self, n):
        count = n if isinstance(n, int) else len(n)
        return [self] * count
    def selectbox(self, label, options, **kwargs):
        items = list(options)
        key = kwargs.get('key', '')
        if key == 'new_tx_type_v070':return self.typ
        if str(key).startswith('entry_asset_v070_'):
            return self.asset_id if self.asset_id in items else items[0]
        index = kwargs.get('index', 0)
        return items[index]
    def radio(self, label, options, **kwargs):return list(options)[0]
    def date_input(self, label, value=None, **kwargs):return value or date.today()
    def text_input(self, label, value='', **kwargs):return value
    def number_input(self, label, value=0.0, **kwargs):return value
    def checkbox(self, *args, **kwargs):return kwargs.get('value', False)
    def toggle(self, *args, **kwargs):return kwargs.get('value', False)
    def button(self, *args, **kwargs):return False
    def caption(self, *args, **kwargs):pass
    def warning(self, *args, **kwargs):self.messages.append(('warning',args))
    def info(self, *args, **kwargs):self.messages.append(('info',args))
    def error(self, *args, **kwargs):self.messages.append(('error',args))
    def markdown(self, *args, **kwargs):pass
    def divider(self, *args, **kwargs):pass


@pytest.mark.parametrize('typ,symbol', [
    ('BUY', 'EUNL'), ('SELL','EUNL'), ('BUY','GOLD'),
    ('SELL','SILVER'), ('BUY','OBLIGACJE'), ('SELL','OBLIGACJE'),
    ('STAKING_START','BTC'), ('STAKING_END','BTC'),
    ('REWARD','BTC'), ('DEPOSIT','EUNL'),
    ('CASH_TRANSFER','EUNL'), ('FX_CONVERSION','EUNL'), ('SWAP','BTC')
])
def test_form_branch_renders_headlessly(tmp_path, monkeypatch, typ, symbol):
    path = tmp_path / 'test.sqlite3'
    init_db(path)
    with connect(path) as conn:
        acct = add_account(conn, 'Rachunek', 'broker')
        stake = add_account(conn, 'Staking', 'staking')
        assets = {r['symbol']:r['id'] for r in conn.execute('SELECT symbol,id FROM assets')}
        # No network requests needed to render with cached recent rates.
        for ccy in ('EUR','USD'):
            upsert_fx(conn, ccy, date.today().isoformat(), 4.0)
        if typ == 'STAKING_END':
            conn.execute('''INSERT INTO staking_plans(asset_id,source_account_id,staking_account_id,quantity,start_date,apy_pct,active)
                            VALUES(?,?,?,?,?,?,?)''', (assets['BTC'],acct,stake,1.0,date.today().isoformat(),3.0,1))
            conn.commit()
        ui = FakeUI(typ, assets[symbol])
        ui.session_state['entry_fx_attempted'] = {f'{ccy}:{date.today().isoformat()}' for ccy in ('EUR','USD')}
        ui.session_state['entry_metal_attempted'] = {f'{assets[symbol]}:{date.today().isoformat()}'}
        monkeypatch.setitem(sys.modules, 'streamlit', ui)
        import portfolio.transaction_form as mod
        monkeypatch.setattr(mod, 'st', ui)
        if symbol in ('GOLD','SILVER'):
            monkeypatch.setattr(mod, 'metal_reference_pln', lambda *a,**kw:{'price_pln':200.0,'price_date':date.today().isoformat(),'source':'nbp_gold','fx_date':date.today().isoformat()})
        account_opts = {acct:'Rachunek',stake:'Staking'}
        asset_opts = {v:k for k,v in assets.items()}
        mod.render_transaction_form(conn, account_opts, asset_opts, lambda conn, data:None)
        assert not any(kind == 'error' for kind,_ in ui.messages), ui.messages
