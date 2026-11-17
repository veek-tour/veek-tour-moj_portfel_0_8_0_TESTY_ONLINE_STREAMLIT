"""One dynamic, immediately updating entry area for all portfolio operations.

Only the selected operation/asset category determines visible fields. FX
references are dated NBP estimates; broker settlement remains authoritative.
"""
from __future__ import annotations

from datetime import date, timedelta

import streamlit as st

from .db import backup_db, get_setting, set_setting
from .fx_quotes import fx_quote
from .metals import metal_reference_pln
from .operations import (SUPPORTED_CURRENCIES, check_currency, metal_trade_values,
                         positive_number, settlement_amount)
from .staking import start_staking, end_staking
from .bonds import list_bond_tranches
from .linked_operations import purchase_settlement_total
from .ledger import calculate_ledger, latest_price_with_currency

TYPES = {
    "BUY": "Kupno aktywa", "SELL": "Sprzedaż / wykup aktywa",
    "DEPOSIT": "Wpłata zewnętrzna", "WITHDRAWAL": "Wypłata zewnętrzna",
    "FEE": "Opłata gotówkowa",
    "STAKING_START": "Rozpoczęcie stakingu", "STAKING_END": "Zakończenie stakingu",
    "REWARD": "Nagroda (staking / odsetki z obligacji)",
    "ASSET_TRANSFER": "Transfer aktywa między rachunkami",
    "CASH_TRANSFER": "Transfer gotówki między rachunkami",
    "FX_CONVERSION": "Wymiana waluty",
    "SWAP": "Wymiana krypto na krypto", "TOKEN_FEE": "Opłata pobrana w kryptowalucie",
}
NEED_ASSET = {"BUY", "SELL", "INTEREST", "REWARD", "STAKING_START",
              "ASSET_TRANSFER", "SWAP", "TOKEN_FEE"}


def _fx_reference(conn, currency: str, day: str) -> dict | None:
    """One network attempt per day+currency per browser session if missing."""
    currency = check_currency(currency)
    if currency == "PLN":
        return {"rate": 1.0, "date": day, "source": "PLN", "stale": False}
    attempts = st.session_state.setdefault("entry_fx_attempted", set())
    key = f"{currency}:{day}"
    if key not in attempts:
        attempts.add(key)
        fx_quote(conn, currency, day, fetch_missing=True)
    return fx_quote(conn, currency, day)


def _set_fx_data(data: dict, prefix: str, ccy: str, quote: dict | None,
                 *, advanced: bool, key: str) -> bool:
    """Return False only if a required historical rate is unavailable/too old."""
    rate_key = {"price": "price_fx_to_pln", "cash": "cash_fx_to_pln", "fee": "fee_fx_to_pln"}[prefix]
    cc = check_currency(ccy)
    if cc == "PLN":
        data[rate_key] = 1.0
        data[f"{prefix}_fx_rate_date"] = data["occurred_at"][:10]
        data[f"{prefix}_fx_source"] = "PLN"
        return True
    if advanced:
        label = {"price":"Kurs waluty instrumentu do PLN", "cash":"Kurs waluty rozliczenia do PLN",
                 "fee":"Kurs waluty opłaty do PLN"}[prefix]
        entered = st.number_input(label + f" ({cc})", min_value=0.0,
                                  value=float(quote['rate']) if quote else 0.0,
                                  format="%.6f", key=f"{key}_{prefix}_{cc}")
        rate = float(entered)
        if quote and abs(rate - float(quote['rate'])) < 1e-9:
            source, rate_date = quote['source'], quote['date']
        else:
            source, rate_date = "manual", data['occurred_at'][:10]
    else:
        rate = float(quote['rate']) if quote else 0.0
        source = quote['source'] if quote else None
        rate_date = quote['date'] if quote else None
    data[rate_key] = rate or None
    data[f"{prefix}_fx_rate_date"] = rate_date
    data[f"{prefix}_fx_source"] = source
    if rate <= 0:
        st.warning(f"Nie znaleziono kursu {cc}/PLN dla tej daty. Rozwiń szczegóły i wprowadź kurs z potwierdzenia transakcji.")
        return False
    if quote and quote.get('stale') and source != 'manual':
        st.warning(f"Ostatni kurs {cc}/PLN to {quote['date']} (za stary względem operacji). Wprowadź prawidłowy kurs ręcznie.")
        return False
    return True


def _data_template(day: date, typ: str, account_id: int, asset_id: int | None, notes: str) -> dict:
    return {"occurred_at": day.isoformat(), "type": typ,
            "account_id": account_id, "to_account_id": None, "asset_id": asset_id,
            "to_asset_id": None, "quantity": None, "to_quantity": None,
            "unit_price": None, "price_currency": None, "price_fx_to_pln": None,
            "cash_amount": None, "cash_currency": None, "cash_fx_to_pln": None,
            "to_cash_amount": None, "to_cash_currency": None, "to_cash_fx_to_pln": None,
            "fee_amount": 0.0, "fee_currency": None, "fee_fx_to_pln": None,
            "valuation_pln": None, "split_ratio": None, "bond_tranche_purchase_id": None,
            "metal_spot_pln_per_oz": None, "metal_adjustment_pln_per_oz": None,
            "price_fx_rate_date": None, "price_fx_source": None,
            "cash_fx_rate_date": None, "cash_fx_source": None,
            "fee_fx_rate_date": None, "fee_fx_source": None, "notes": notes}


def render_transaction_form(conn, account_opts: dict, asset_opts: dict, save_tx_checked) -> None:
    expanded = st.session_state.get('entry_keep_open', False) or not bool(
        conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0])
    with st.expander("➕ Dodaj operację", expanded=expanded):
        st.caption("Pola dostosowują się do operacji. Kwoty i kursy są liczone automatycznie, a szczegóły można poprawić.")
        chosen = st.selectbox("Rodzaj operacji", list(TYPES), format_func=lambda x: TYPES[x],
                              key="new_tx_type_v070")
        if not account_opts:
            st.warning("Najpierw dodaj rachunek w zakładce „Aktywa i rachunki”.")
            return
        account_ids = list(account_opts)
        remembered = st.session_state.get("last_account_id")
        selected_index = account_ids.index(remembered) if remembered in account_ids else 0
        c1, c2, c3 = st.columns(3)
        day = c1.date_input("Data operacji", value=date.today(), key="entry_day_v070")
        account = c2.selectbox("Rachunek", account_ids, index=selected_index,
                               format_func=lambda x: account_opts[x], key="entry_account_v070")
        notes = c3.text_input("Notatka (opcjonalnie)", max_chars=250, key="entry_notes_v070")
        if day > date.today():
            st.warning("Data operacji nie może być w przyszłości.")
            return
        day_str = day.isoformat()
        asset_id = None
        asset = None
        reward_kind = None
        if chosen == 'REWARD':
            reward_kind = st.radio('Rodzaj nagrody', ['staking', 'obligacje'], horizontal=True,
                                   format_func=lambda x: 'Nagroda ze stakingu' if x == 'staking' else 'Wypłacone odsetki obligacji',
                                   key='entry_reward_kind')
        if chosen in NEED_ASSET:
            if chosen in ("STAKING_START", "SWAP", "TOKEN_FEE") or (chosen == 'REWARD' and reward_kind == 'staking'):
                ids = [a for a in asset_opts if conn.execute("SELECT category FROM assets WHERE id=?", (a,)).fetchone()[0] == 'Kryptowaluty']
            elif chosen == 'INTEREST' or (chosen == 'REWARD' and reward_kind == 'obligacje'):
                ids = [a for a in asset_opts if conn.execute("SELECT category FROM assets WHERE id=?", (a,)).fetchone()[0] == 'Obligacje']
            else:
                ids = list(asset_opts)
            if not ids:
                st.warning("Brak instrumentów dostępnych dla tej operacji.")
                return
            asset_id = st.selectbox("Instrument", ids, format_func=lambda a: asset_opts[a],
                                    key=f"entry_asset_v070_{chosen}")
            asset = conn.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
        if chosen == 'STAKING_END':
            plans = [dict(r) for r in conn.execute("""SELECT p.*,a.symbol FROM staking_plans p
                JOIN assets a ON a.id=p.asset_id WHERE p.active=1 ORDER BY p.start_date DESC""")]
            if not plans:
                st.info("Nie masz aktywnego stakingu do zakończenia.")
                return
            opts = {int(p['id']): p for p in plans}
            plan_id = st.selectbox('Aktywny staking', list(opts),
                                  format_func=lambda i: f"#{i} · {opts[i]['symbol']} · {opts[i]['quantity']:.8f} szt.",
                                  key="entry_staking_end_plan")
            qty = st.number_input("Ilość do odblokowania", min_value=0.00000001,
                                  max_value=float(opts[plan_id]['quantity']),
                                  value=float(opts[plan_id]['quantity']), format="%.8f",
                                  key=f"entry_staking_end_qty_{plan_id}")
            st.caption("Odblokowanie jest transferem, nie sprzedażą. Średnia cena zakupu nie zmienia się.")
            if st.button("Zapisz odblokowanie", type="primary", key="entry_staking_end_save"):
                try:
                    if day > date.today():
                        raise ValueError("Data nie może być w przyszłości.")
                    backup_db()
                    end_staking(conn, plan_id, day_str, qty)
                    st.session_state['entry_keep_open'] = True
                    st.toast("Odblokowanie zapisane.", icon="✅")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Nie udało się zakończyć stakingu: {exc}")
            return
        if chosen == 'STAKING_START':
            staking_accounts = {int(r['id']): r['name'] for r in conn.execute("SELECT id,name FROM accounts WHERE active=1 AND kind='staking'")}
            if not staking_accounts:
                st.warning("Dodaj rachunek typu „Staking” w zakładce „Aktywa i rachunki”.")
                return
            if account in staking_accounts:
                st.warning("Jako rachunek źródłowy wybierz rachunek inny niż stakingowy.")
                return
            target = st.selectbox("Rachunek stakingowy", list(staking_accounts),
                                  format_func=lambda x: staking_accounts[x], key="entry_stake_target")
            c1, c2 = st.columns(2)
            qty = c1.number_input("Ilość blokowana", min_value=0.00000001, value=0.00000001,
                                  format="%.8f", key="entry_stake_qty")
            apy = c2.number_input("APY (%)", min_value=0.0, max_value=1000.0,
                                  value=3.0, format="%.3f", key="entry_stake_apy")
            has_end = st.checkbox("Ustaw planowaną datę zakończenia", value=False, key="entry_stake_has_end")
            planned_end = st.date_input("Planowany koniec", value=day + timedelta(days=365),
                                        key="entry_stake_date_end") if has_end else None
            st.caption("Blokada przenosi istniejącą kryptowalutę na rachunek stakingowy. Nie jest sprzedażą.")
            if st.button("Rozpocznij staking", type="primary", key="entry_staking_start_save"):
                try:
                    if day > date.today():
                        raise ValueError("Data nie może być w przyszłości.")
                    backup_db()
                    start_staking(conn, asset_id=asset_id, source_account_id=account,
                                  staking_account_id=target, quantity=qty, start_date=day_str,
                                  planned_end_date=planned_end.isoformat() if planned_end else None,
                                  apy_pct=apy, notes=notes)
                    st.session_state['entry_keep_open'] = True
                    st.toast("Rozpoczęcie stakingu zapisane.", icon="✅")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Nie udało się rozpocząć stakingu: {exc}")
            return

        data = _data_template(day, chosen, account, asset_id, notes)
        category = asset['category'] if asset else None
        currency = asset['currency'] if asset else 'PLN'
        if asset and currency not in SUPPORTED_CURRENCIES:
            st.error(f"Instrument ma walutę {currency}. Nowy formularz obsługuje PLN, EUR i USD.")
            return
        context = f"{chosen}_{asset_id or 0}_{day_str}"
        can_save = day <= date.today()

        # BUY/SELL differs for physical bullion, individual bond tranches, and
        # standard market-priced instruments. The outer window is always shared.
        if chosen in ('BUY', 'SELL') and asset:
            is_metal = category == 'Metale szlachetne'
            is_bond = category == 'Obligacje'
            st.markdown('**' + ('Metale szlachetne' if is_metal else 'Obligacje – osobna transza' if is_bond else 'Kupno / sprzedaż instrumentu') + '**')
            if is_metal:
                data['cash_currency'] = 'PLN'
                data['price_currency'] = 'PLN'  # Transaction reference quoted in PLN/oz, even if asset quotes USD.
                metal_attempts = st.session_state.setdefault('entry_metal_attempted', set())
                metal_key = f'{asset_id}:{day_str}'
                should_fetch = metal_key not in metal_attempts
                metal_attempts.add(metal_key)
                reference = metal_reference_pln(conn, int(asset_id), asset['symbol'], day_str,
                                               fetch=should_fetch)
                if reference:
                    st.caption(f"Propozycja ceny: {reference['source']}, {reference['price_date']}. Wartość kruszcu, nie cena monety.")
                    if 'futures' in reference['source'] or reference['source'] == 'yahoo':
                        st.warning('Cena pochodzi z kontraktu futures (przybliżenie), a nie ze źródła spot. Możesz ją poprawić.')
                else:
                    st.caption('Brak dostępnej ceny rynkowej z tej daty. Wpisz cenę za uncję z własnego źródła.')
                c1, c2, c3 = st.columns(3)
                qty = c1.number_input('Ilość (oz troy)', min_value=0.00000001, value=1.0,
                                      format='%.8f', key=f"entry_metal_qty_{chosen}_{asset_id}")
                spot = c2.number_input('Rynkowa cena metalu za 1 oz (PLN)', min_value=0.0,
                                       value=round(float(reference['price_pln']), 4) if reference else 0.0,
                                       format='%.4f', key=f"entry_metal_spot_{context}")
                adjustment = c3.number_input('Korekta ceny za 1 oz (PLN)', value=0.0,
                                             format='%.4f', key=f"entry_metal_adjust_{chosen}_{asset_id}",
                                             help='Wpisz +20 dla dopłaty ponad cenę metalu albo -10 dla ceny poniżej ceny metalu.')
                data['quantity'] = qty
                data['unit_price'] = spot
                data['metal_spot_pln_per_oz'] = spot
                data['metal_adjustment_pln_per_oz'] = adjustment
                data['price_fx_to_pln'] = 1.0
                data['cash_fx_to_pln'] = 1.0
                data['price_fx_rate_date'] = day_str
                data['cash_fx_rate_date'] = day_str
                data['price_fx_source'] = data['cash_fx_source'] = 'PLN'
                try:
                    computed = metal_trade_values(qty, spot, adjustment)
                    data['cash_amount'] = computed['cash_amount_pln']
                except ValueError as exc:
                    st.error(str(exc))
                    computed, can_save = None, False
                st.caption('Korekta obejmuje premię, marżę i podatki zawarte w cenie. Nie doliczaj ich ponownie jako prowizji.')
            else:
                c1, c2 = st.columns(2)
                qty = c1.number_input('Liczba obligacji' if is_bond else 'Ilość',
                                      min_value=0.00000001, value=1.0, format='%.8f',
                                      key=f"entry_quantity_{chosen}_{asset_id}")
                price = c2.number_input('Cena zakupu jednej obligacji (PLN)' if is_bond else f'Cena jednostkowa ({currency})',
                                        min_value=0.0, value=0.0, format='%.8f',
                                        key=f"entry_unit_price_{chosen}_{asset_id}")
                data['quantity'], data['unit_price'], data['price_currency'] = qty, price, currency
                computed = None
                if not is_bond:
                    ref = latest_price_with_currency(conn, int(asset_id), currency, day_str)
                    if ref['price'] is not None and ref['date'] and ref['date'] <= day_str:
                        gap_days = (day - date.fromisoformat(ref['date'])).days
                        st.caption(f"Informacyjna cena rynkowa: {ref['price']:,.6f} {ref['currency']} "
                                   f"· {ref['date']} · źródło: {ref['source']}. "
                                   "Nie uzupełnia ceny Twojej transakcji.".replace(',', ' '))
                        if gap_days > 7:
                            st.warning('Notowanie referencyjne jest starsze niż 7 dni. Nie traktuj go jako ceny z dnia operacji.')
                    else:
                        st.caption('Brak zapisanej ceny referencyjnej dla tej daty. Wpisz rzeczywistą cenę z potwierdzenia zakupu. '
                                   'Dane historyczne można pobrać w zakładce „Wyceny i historia”.')

            ccy_choices = list(SUPPORTED_CURRENCIES)
            advanced = st.toggle('Szczegóły rozliczenia i kursy walut', value=False,
                                 key=f"entry_advanced_{chosen}_{asset_id}")
            other_currency = False
            fee_other_currency = False
            actual_toggle = False
            if is_metal:
                cash_ccy = 'PLN'
                st.caption('Rozliczenie fizycznych monet: PLN. Kwota wynika z ceny metalu i korekty.')
            elif is_bond:
                cash_ccy = 'PLN'
            else:
                if advanced:
                    other_currency = st.checkbox('Waluta zapłaty różni się od waluty notowania',
                                                 key=f"entry_other_ccy_{context}")
                cash_ccy = st.selectbox('Waluta rozliczenia', [c for c in ccy_choices if c != currency],
                                        key=f"entry_settle_currency_{context}") if other_currency else currency
                if not advanced:
                    st.caption(f'Waluta notowania i domyślnego rozliczenia: {currency}')
            data['cash_currency'] = cash_ccy
            price_quote = _fx_reference(conn, data['price_currency'], day_str)
            cash_quote = _fx_reference(conn, cash_ccy, day_str)
            fx_override = st.checkbox('Ręcznie skoryguj kursy do PLN', value=False,
                                       key=f"entry_fx_override_{context}") if advanced and not is_metal else False
            ok_price = _set_fx_data(data, 'price', data['price_currency'], price_quote,
                                    advanced=fx_override, key=context)
            ok_cash = _set_fx_data(data, 'cash', cash_ccy, cash_quote,
                                   advanced=fx_override, key=context)
            can_save = can_save and ok_price and ok_cash
            if is_metal:
                estimated_cash = data['cash_amount']
            else:
                try:
                    estimated_cash = settlement_amount(qty, price, currency, cash_ccy,
                        reference_fx_price=data['price_fx_to_pln'],
                        reference_fx_settlement=data['cash_fx_to_pln'])
                except ValueError:
                    estimated_cash = 0.0
                    can_save = False
            if advanced and not is_metal:
                actual_toggle = st.checkbox('Podaj rzeczywistą kwotę rozliczenia (z brokera)',
                                            value=False, key=f"entry_manual_cash_{context}")
                if actual_toggle:
                    data['cash_amount'] = st.number_input(f'Kwota faktycznie zapłacona / otrzymana ({cash_ccy})',
                                                           min_value=0.0, value=round(estimated_cash, 6),
                                                           format='%.6f', key=f"entry_manual_cash_amount_{context}")
                else:
                    data['cash_amount'] = estimated_cash
            elif not is_metal:
                data['cash_amount'] = estimated_cash
            if cash_ccy != data['price_currency'] and not actual_toggle:
                st.info('Kwota jest oszacowana według kursów NBP. Jeśli broker zastosował inną kwotę, wpisz ją w szczegółach rozliczenia.')
            fee_col, _ = st.columns(2)
            data['fee_amount'] = fee_col.number_input('Dodatkowa prowizja / opłata', min_value=0.0,
                          value=0.0, format='%.4f', key=f"entry_fee_{chosen}_{asset_id}",
                          help='Nie wpisuj tutaj kosztów już zawartych w korekcie ceny metalu.')
            if advanced and data['fee_amount'] > 0:
                fee_other_currency = st.checkbox('Prowizja pobrana w innej walucie',
                                                  key=f"entry_fee_other_currency_{context}")
            fee_ccy = st.selectbox('Waluta prowizji', [c for c in ccy_choices if c != cash_ccy],
                                   key=f"entry_fee_currency_{context}") if fee_other_currency else cash_ccy
            data['fee_currency'] = fee_ccy
            if data['fee_amount']:
                fee_quote = _fx_reference(conn, fee_ccy, day_str)
                can_save = can_save and _set_fx_data(data, 'fee', fee_ccy, fee_quote,
                                                     advanced=fx_override, key=context)
            else:
                data['fee_fx_to_pln'] = 1.0 if fee_ccy == 'PLN' else None
            if is_bond:
                st.divider()
                if chosen == 'BUY':
                    fixed_default = asset['bond_type'] if asset['bond_type'] in ('fixed', 'inflation') else 'fixed'
                    kind = st.selectbox('Oprocentowanie transzy', ('fixed', 'inflation'),
                         index=(0 if fixed_default == 'fixed' else 1),
                         format_func=lambda x: 'Stałe' if x == 'fixed' else 'Indeksowane inflacją',
                         key=f"entry_bond_kind_{asset_id}")
                    c1, c2, c3 = st.columns(3)
                    if kind == 'fixed':
                        rate = c1.number_input('Oprocentowanie roczne (%)', min_value=0.0,
                            value=float(asset['annual_rate_pct'] or 5.0), format='%.3f', key=f"entry_bond_rate_{asset_id}")
                        margin, inflation = None, None
                    else:
                        rate = None
                        margin = c1.number_input('Marża ponad inflację (%)', value=float(asset['inflation_margin_pct'] or 1.0),
                                                  format='%.3f', key=f"entry_bond_margin_{asset_id}")
                        try:
                            inflation_default = float(get_setting(conn, 'bond_default_inflation_pct', '4.0'))
                        except ValueError:
                            inflation_default = 4.0
                        inflation = c2.number_input('Założona inflacja na CAŁY okres (%)',
                            value=inflation_default, min_value=-5.0, max_value=100.0,
                            format='%.2f', key=f"entry_bond_inflation_{asset_id}")
                    nominal = c2.number_input('Nominał jednej obligacji (PLN)', min_value=0.01,
                        value=float(asset['nominal_value'] or 100.0), format='%.2f', key=f"entry_bond_nominal_{asset_id}")
                    maturity = c3.date_input('Termin wykupu', value=day + timedelta(days=365),
                        min_value=day, key=f"entry_bond_maturity_{asset_id}_{day_str}")
                    compounding = False
                    st.caption('Szacowanie liniowe według jednej rocznej stopy. Bez kapitalizacji odsetek.')
                    data['bond_terms'] = {'bond_type':kind, 'annual_rate_pct':rate,
                        'inflation_margin_pct':margin, 'assumed_inflation_pct':inflation,
                        'nominal_value':nominal, 'maturity_date':maturity.isoformat(),
                        'compounding':compounding}
                else:
                    tranches = list_bond_tranches(conn, asset_id=asset_id, as_of=day_str)
                    tranches = [t for t in tranches if float(t.get('remaining_quantity') or 0) > 0]
                    if not tranches:
                        st.warning('Nie ma dostępnej transzy tej obligacji na wskazany dzień.')
                        can_save = False
                    else:
                        choice = {int(t['purchase_transaction_id']):t for t in tranches}
                        tranche_id = st.selectbox('Transza sprzedawana / wykupywana', list(choice),
                            format_func=lambda i:f"Zakup {choice[i]['purchase_date']} · wykup {choice[i]['maturity_date']} · pozostało {choice[i]['remaining_quantity']}",
                            key=f"entry_bond_tranche_{asset_id}_{day_str}")
                        data['bond_tranche_purchase_id'] = tranche_id
                        st.info('Przy wykupie wpisz łączną kwotę rzeczywiście otrzymaną (kapitał + odsetki). '
                                'Nie dodawaj tych samych odsetek drugi raz jako nagrody.')
                        if qty > float(choice[tranche_id]['remaining_quantity']) + 1e-8:
                            st.warning('Ilość większa niż pozostała liczba obligacji w tej transzy.')
                            can_save = False
            base_amount = float(data['cash_amount'] or 0)
            st.markdown(f"**{'Wpływy' if chosen == 'SELL' else 'Koszt'} przed prowizją: {base_amount:,.2f} {cash_ccy}**".replace(',', ' '))
            st.caption(f"Prowizja: {data['fee_amount']:.2f} {fee_ccy}. " +
                       (f"Szacunkowy wynik po zakupie względem wartości kruszcu: {(-adjustment*qty - data['fee_amount'] * (data['fee_fx_to_pln'] or 1.0)):+,.2f} PLN." if is_metal and chosen == 'BUY' else
                        "Rzeczywista wartość zostanie wyliczona po zapisie i uwzględnieniu historii."))
            funding_amount = None
            funding_follows = False
            if chosen == 'BUY' and can_save and float(data.get('cash_amount') or 0) > 0:
                add_funding = st.checkbox('Dodaj jednocześnie wpłatę gotówki na rachunek', value=False,
                    key=f'entry_add_funding_{asset_id}', help='Jedna wpłata zewnętrzna i jeden zakup, zapisane razem.')
                if add_funding:
                    suggested = purchase_settlement_total(data)
                    amount_key = f'entry_funding_amount_{asset_id}'
                    previous_key = f'entry_funding_previous_suggestion_{asset_id}'
                    old_suggested = st.session_state.get(previous_key)
                    if (old_suggested is not None and amount_key in st.session_state and
                            abs(float(st.session_state[amount_key])-float(old_suggested)) < 1e-5):
                        st.session_state[amount_key] = round(suggested, 6)
                    st.session_state[previous_key] = round(suggested, 6)
                    funding_amount = st.number_input(f'Kwota wpłaty ({cash_ccy})', min_value=0.01,
                        value=round(suggested, 6), format='%.6f', key=amount_key)
                    funding_follows = abs(funding_amount - suggested) < 1e-5
                    st.caption('Przy domyślnej kwocie wpłata dostosuje się do późniejszej edycji zakupu. '
                               'Własna kwota pozostanie niezmieniona.')
            if chosen == 'BUY' and not funding_amount:
                try:
                    balances = calculate_ledger(conn)['cash_balances']
                    current_cash = float(balances.get((account, cash_ccy), 0.0))
                    required_cash = float(data.get('cash_amount') or 0)
                    if fee_ccy == cash_ccy:
                        required_cash += float(data.get('fee_amount') or 0)
                    if current_cash + 1e-8 < required_cash:
                        st.warning(f'Na rachunku zapisano {current_cash:.2f} {cash_ccy}, a zakup wymaga '
                                   f'{required_cash:.2f} {cash_ccy}. Możesz zapisać zakup historyczny mimo ujemnego salda.')
                except Exception:
                    pass
        elif chosen in ('DEPOSIT', 'WITHDRAWAL', 'FEE', 'INTEREST'):
            c1, c2 = st.columns(2)
            amount = c1.number_input('Kwota', min_value=0.0, value=0.0,
                                     format='%.2f', key=f"entry_cash_{chosen}")
            cash_ccy = c2.selectbox('Waluta operacji', list(SUPPORTED_CURRENCIES),
                                     key=f"entry_cash_currency_{chosen}")
            data.update({'cash_amount':amount, 'cash_currency':cash_ccy})
            quote = _fx_reference(conn, cash_ccy, day_str)
            advanced = st.toggle('Szczegóły kursu', key=f"entry_cash_advanced_{chosen}")
            can_save = can_save and _set_fx_data(data, 'cash', cash_ccy, quote,
                                                 advanced=advanced, key=context)
            if chosen == 'INTEREST':
                fee = st.number_input('Dodatkowa potrącona opłata', min_value=0.0, value=0.0,
                                      format='%.2f', key='entry_interest_fee')
                data['fee_amount'], data['fee_currency'] = fee, cash_ccy
                data['fee_fx_to_pln'] = data['cash_fx_to_pln']
            st.caption(f'Kwota: {amount:,.2f} {cash_ccy}'.replace(',', ' '))
        elif chosen == 'REWARD':
            note_default = ('Nagroda za staking' if reward_kind == 'staking' else 'Odsetki od obligacji')
            reason = st.text_input('Notatka — obowiązkowa', value=notes or note_default,
                                   key=f'entry_reward_note_{reward_kind}')
            data['notes'] = reason.strip()
            if not data['notes']:
                can_save = False
                st.error('Nagroda wymaga notatki.')
            if reward_kind == 'staking':
                data['type'] = 'REWARD'
                data['quantity'] = st.number_input('Otrzymana ilość kryptowaluty (koszt 0 zł)',
                    min_value=0.00000001, value=0.00000001, format='%.8f', key='entry_reward_qty_v080')
                data['unit_price'] = 0.0
                data['price_currency'] = currency
                data['price_fx_to_pln'] = None  # no money was spent; no historical FX needed
                st.caption('Nagroda zwiększa liczbę jednostek przy zerowym koszcie nabycia. '
                           'Nie tworzy wpłaty i nie księguje przychodu drugi raz.')
            else:
                data['type'] = 'INTEREST'
                data['cash_currency'] = 'PLN'
                data['cash_fx_to_pln'] = 1.0
                data['cash_fx_rate_date'] = day_str
                data['cash_fx_source'] = 'PLN'
                open_tranches = list_bond_tranches(conn, asset_id=asset_id, as_of=day_str)
                open_tranches = [t for t in open_tranches if float(t.get('remaining_quantity', 0)) > 0]
                if not open_tranches:
                    st.warning('Brak otwartej transzy na ten dzień.')
                    can_save = False
                else:
                    tranche_map = {int(t['purchase_transaction_id']):t for t in open_tranches}
                    data['bond_tranche_purchase_id'] = st.selectbox('Transza, której dotyczą odsetki',
                        list(tranche_map), format_func=lambda i: f"Zakup {tranche_map[i]['purchase_date'][:10]} · "
                        f"wykup {tranche_map[i]['maturity_date']}", key='entry_reward_bond_tranche')
                data['cash_amount'] = st.number_input('Kwota odsetek rzeczywiście wypłaconych (PLN)',
                    min_value=0.0, value=0.0, format='%.2f', key='entry_bond_interest_amount')
                st.caption('Tylko rzeczywiście otrzymane odsetki. Wycena transzy zostanie skorygowana, '
                           'żeby nie liczyć ich dwa razy. Przy pełnym wykupie użyj sprzedaży obligacji.')
        elif chosen in ('ASSET_TRANSFER', 'CASH_TRANSFER'):
            destinations = [i for i in account_opts if i != account]
            if not destinations:
                st.warning('Dodaj drugi rachunek, żeby wykonać transfer.')
                return
            data['to_account_id'] = st.selectbox('Rachunek docelowy', destinations,
                         format_func=lambda x:account_opts[x], key='entry_transfer_target')
            if chosen == 'ASSET_TRANSFER':
                data['quantity'] = st.number_input('Ilość transferowanego aktywa', min_value=0.00000001,
                                     value=0.00000001, format='%.8f', key='entry_transfer_qty')
            else:
                c1, c2 = st.columns(2)
                data['cash_amount'] = c1.number_input('Kwota transferu', min_value=0.0,
                             value=0.0, format='%.2f', key='entry_transfer_cash')
                data['cash_currency'] = c2.selectbox('Waluta', list(SUPPORTED_CURRENCIES),
                                                    key='entry_transfer_ccy')
                data['to_cash_amount'] = data['cash_amount']
            st.caption('Transfer własny nie zmienia średniego kosztu ani wyniku inwestycyjnego.')
        elif chosen == 'FX_CONVERSION':
            c1,c2 = st.columns(2)
            source_ccy = c1.selectbox('Waluta oddawana', list(SUPPORTED_CURRENCIES),
                                     key='entry_fx_from')
            target_ccy = c2.selectbox('Waluta otrzymywana', [x for x in SUPPORTED_CURRENCIES if x != source_ccy],
                                     key=f'entry_fx_to_{source_ccy}')
            c1,c2 = st.columns(2)
            data['cash_amount'] = c1.number_input(f'Kwota oddawana ({source_ccy})',
                min_value=0.0, value=0.0, format='%.2f', key='entry_fx_amount_from')
            data['to_cash_amount'] = c2.number_input(f'Kwota faktycznie otrzymana ({target_ccy})',
                min_value=0.0, value=0.0, format='%.2f', key='entry_fx_amount_to')
            data['cash_currency'], data['to_cash_currency'] = source_ccy, target_ccy
            st.caption('Wpisz rzeczywiste kwoty z wymiany. Spread brokera jest już odzwierciedlony w ich różnicy.')
        elif chosen == 'SWAP':
            crypto_ids = [a for a in asset_opts if a != asset_id and conn.execute(
                'SELECT category FROM assets WHERE id=?', (a,)).fetchone()[0]=='Kryptowaluty']
            if not crypto_ids:
                st.warning('Brak drugiej kryptowaluty do zamiany.')
                return
            data['to_asset_id'] = st.selectbox('Kryptowaluta otrzymywana', crypto_ids,
                 format_func=lambda a:asset_opts[a], key='entry_swap_target')
            c1,c2,c3 = st.columns(3)
            data['quantity'] = c1.number_input('Ilość oddawana', min_value=0.00000001,
                value=0.00000001, format='%.8f', key='entry_swap_out')
            data['to_quantity'] = c2.number_input('Ilość otrzymana', min_value=0.00000001,
                value=0.00000001, format='%.8f', key='entry_swap_in')
            data['valuation_pln'] = c3.number_input('Wartość wymiany (PLN)', min_value=0.0,
                value=0.0, format='%.2f', key='entry_swap_value')
            data['fee_amount'] = st.number_input('Opłata za wymianę', min_value=0.0,
                value=0.0, format='%.6f', key='entry_swap_fee')
            data['fee_currency'] = st.selectbox('Waluta opłaty', list(SUPPORTED_CURRENCIES),
                                                key='entry_swap_fee_ccy')
            if data['fee_amount']:
                q = _fx_reference(conn, data['fee_currency'], day_str)
                can_save = can_save and _set_fx_data(data, 'fee', data['fee_currency'], q,
                                                     advanced=False, key=context)
            st.caption('Swap to jedna operacja bez pośredniego wpływu gotówki.')
        elif chosen == 'TOKEN_FEE':
            data['quantity'] = st.number_input('Ilość pobrana jako opłata',
                             min_value=0.00000001, value=0.00000001, format='%.8f',
                             key='entry_token_fee_qty')

        if st.button('Zapisz operację', type='primary', key='entry_save_v070'):
            try:
                if not can_save:
                    raise ValueError('Uzupełnij brakujące kwoty lub kursy historyczne przed zapisem.')
                if chosen in ('BUY','SELL'):
                    positive_number(data['cash_amount'], 'Kwota rozliczenia')
                    positive_number(data['unit_price'], 'Cena jednostkowa')
                    if chosen == 'SELL' and category=='Obligacje' and not data['bond_tranche_purchase_id']:
                        raise ValueError('Wybierz transzę do wykupu.')
                if chosen in ('DEPOSIT','WITHDRAWAL','INTEREST','FEE','CASH_TRANSFER','FX_CONVERSION'):
                    positive_number(data['cash_amount'], 'Kwota operacji')
                if chosen == 'FX_CONVERSION':
                    positive_number(data['to_cash_amount'], 'Kwota otrzymana')
                if chosen == 'SWAP':
                    positive_number(data['valuation_pln'], 'Wartość wymiany')
                if chosen == 'REWARD' and reward_kind == 'obligacje':
                    positive_number(data['cash_amount'], 'Wypłacone odsetki')
                funding = funding_amount if chosen == 'BUY' else None
                follows = funding_follows if chosen == 'BUY' else False
                save_tx_checked(conn, data, funding_deposit_amount=funding, funding_follows=follows)
                if data.get('bond_terms', {}).get('assumed_inflation_pct') is not None:
                    set_setting(conn, 'bond_default_inflation_pct', str(data['bond_terms']['assumed_inflation_pct']))
                st.session_state['last_account_id'] = int(account)
                st.session_state['entry_keep_open'] = True
                st.toast('Operacja została zapisana i portfel przeliczony.', icon='✅')
                st.rerun()
            except Exception as exc:
                st.error(f'Nie udało się zapisać operacji: {exc}')
