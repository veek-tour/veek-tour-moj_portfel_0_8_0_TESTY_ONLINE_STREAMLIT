"""Atomic bookkeeping for a user-requested cash deposit accompanying a BUY.

A linked deposit is a separate external contribution, never a part of the asset's
purchase cost. It is linked explicitly so edits/deletions cannot leave phantom cash.
"""
from __future__ import annotations

from math import isfinite
from .db import add_transaction


def purchase_settlement_total(purchase: dict) -> float:
    """Total amount expressed in the purchase settlement currency, fee included."""
    amount = float(purchase.get('cash_amount') or 0)
    fee = float(purchase.get('fee_amount') or 0)
    cash_fx = float(purchase.get('cash_fx_to_pln') or 0)
    if not isfinite(amount) or amount <= 0 or not isfinite(fee) or fee < 0:
        raise ValueError('Nieprawidłowy koszt zakupu.')
    if cash_fx <= 0 or not isfinite(cash_fx):
        raise ValueError('Brak kursu waluty rozliczenia zakupu.')
    fee_ccy = purchase.get('fee_currency') or purchase.get('cash_currency')
    if fee and fee_ccy != purchase.get('cash_currency'):
        fee_fx = float(purchase.get('fee_fx_to_pln') or 0)
        if fee_fx <= 0 or not isfinite(fee_fx):
            raise ValueError('Brak kursu waluty prowizji.')
        fee *= fee_fx / cash_fx
    return amount + fee


def _linked_deposit_fields(purchase: dict, amount: float, follows: bool) -> dict:
    if not isfinite(amount) or amount <= 0:
        raise ValueError('Kwota powiązanej wpłaty musi być dodatnia.')
    return {
        'occurred_at': purchase['occurred_at'], 'type': 'DEPOSIT',
        'account_id': purchase['account_id'], 'cash_amount': amount,
        'cash_currency': purchase['cash_currency'],
        'cash_fx_to_pln': purchase['cash_fx_to_pln'],
        'cash_fx_rate_date': purchase.get('cash_fx_rate_date'),
        'cash_fx_source': purchase.get('cash_fx_source'),
        'linked_purchase_id': purchase['id'],
        'linked_deposit_follows_purchase': int(bool(follows)),
        'notes': 'Wpłata powiązana z zakupem (automatyczna)',
    }


def attach_deposit(conn, purchase_id: int, amount: float, follows: bool) -> int:
    """Call inside the same database transaction/savepoint as the BUY."""
    row = conn.execute('SELECT * FROM transactions WHERE id=? AND type=\'BUY\'', (purchase_id,)).fetchone()
    if row is None:
        raise ValueError('Wpłatę można powiązać wyłącznie z istniejącym zakupem.')
    existing = conn.execute('SELECT id FROM transactions WHERE linked_purchase_id=?', (purchase_id,)).fetchone()
    if existing:
        raise ValueError('Ten zakup ma już powiązaną wpłatę.')
    return add_transaction(conn, _linked_deposit_fields(dict(row), float(amount), follows))


def sync_deposit_after_purchase_edit(conn, purchase_id: int) -> None:
    """Sync date/account/currency/FX, and amount only if user selected auto-follow.

    For custom deposits, preserve the custom amount. A linked deposit can also be
    edited independently; editing its amount switches off auto-follow.
    """
    purchase_row = conn.execute('SELECT * FROM transactions WHERE id=?', (purchase_id,)).fetchone()
    dep = conn.execute('SELECT * FROM transactions WHERE linked_purchase_id=?', (purchase_id,)).fetchone()
    if dep is None:
        return
    if purchase_row is None or purchase_row['type'] != 'BUY':
        raise ValueError('Nie można utrzymać wpłaty powiązanej z operacją inną niż zakup.')
    purchase = dict(purchase_row)
    amount = (purchase_settlement_total(purchase) if dep['linked_deposit_follows_purchase']
              else float(dep['cash_amount']))
    conn.execute('''UPDATE transactions SET occurred_at=?,account_id=?,cash_currency=?,
        cash_amount=?,cash_fx_to_pln=?,cash_fx_rate_date=?,cash_fx_source=? WHERE id=?''',
        (purchase['occurred_at'],purchase['account_id'],purchase['cash_currency'],amount,
         purchase['cash_fx_to_pln'],purchase['cash_fx_rate_date'],purchase['cash_fx_source'],dep['id']))


def unlink_deposit_on_manual_edit(conn, deposit_id: int, old_amount: float, new_amount: float) -> None:
    if abs(float(old_amount)-float(new_amount)) > 1e-8:
        conn.execute('UPDATE transactions SET linked_deposit_follows_purchase=0 WHERE id=? AND linked_purchase_id IS NOT NULL',
                     (deposit_id,))
