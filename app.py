from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import plotly.express as px
import streamlit as st

from portfolio.db import (DB_PATH, add_account, add_transaction, backup_db, backup_db_if_due, connect,
                          delete_transaction, get_setting, init_db, rows, set_setting, upsert_fx)
from portfolio.finance import external_cashflows, xirr
from portfolio.ledger import LedgerError, calculate_ledger, latest_fx, latest_price_with_currency, value_portfolio
from portfolio.market import sync_market_data
from portfolio.snapshots import rebuild_snapshots, save_snapshot, set_snapshot_override, fill_missing_snapshots
from portfolio.benchmark import benchmark_value, benchmark_series, sync_spy
from portfolio.staking import start_staking, end_staking
from portfolio.bonds import list_bond_tranches, estimate_tranche, upsert_bond_tranche
from portfolio.data_io import import_transactions_csv, restore_database_from_bytes, database_bytes
from portfolio.diagnostics import run_diagnostics
from portfolio.analytics import portfolio_history_series
from portfolio.linked_operations import attach_deposit, sync_deposit_after_purchase_edit, unlink_deposit_on_manual_edit

st.set_page_config(page_title="Mój Portfel 0.8.0 — TESTY ONLINE", page_icon="🧪", layout="wide")
st.warning(
    "🧪 **TESTY ONLINE — WYŁĄCZNIE FIKCYJNE DANE.** "
    "Nie wpisuj prawdziwych informacji o swoich inwestycjach i nie wgrywaj bazy z komputera. "
    "Dane aplikacji na hostingu mogą być widoczne dla innych odwiedzających oraz zniknąć po restarcie."
)
init_db()
backup_db_if_due()


def money(v, currency="PLN"):
    if v is None:
        return "—"
    try:
        return f"{float(v):,.2f} {currency}".replace(",", " ").replace(".", ",")
    except (TypeError, ValueError):
        return "—"


def options_accounts(conn, exclude=None):
    data = rows(conn, "SELECT id,name,kind FROM accounts WHERE active=1 ORDER BY name")
    return {int(r["id"]): f"{r['name']} ({r['kind']})" for r in data if int(r["id"]) != exclude}


def options_assets(conn, category=None):
    sql = "SELECT id,symbol,name,category,currency FROM assets WHERE active=1"
    params = ()
    if category:
        sql += " AND category=?"
        params = (category,)
    sql += " ORDER BY symbol"
    data = rows(conn, sql, params)
    return {int(r["id"]): f"{r['symbol']} — {r['name']} ({r['currency']})" for r in data}


def fx_default(conn, currency, as_of=None):
    if currency == "PLN":
        return 1.0
    # A historical operation must not silently inherit today's FX rate.
    return latest_fx(conn, currency, as_of) or 0.0


def save_tx_checked(conn, data, tx_id=None, *, funding_deposit_amount=None, funding_follows=False):
    """One atomic write: operation, optional linked deposit, ledger and snapshots."""
    backup_db()
    previous = (conn.execute('SELECT * FROM transactions WHERE id=?', (tx_id,)).fetchone()
                if tx_id is not None else None)
    old_date = previous['occurred_at'][:10] if previous else None
    conn.execute('SAVEPOINT transaction_check')
    try:
        new_id = add_transaction(conn, data, tx_id)
        if tx_id is None and funding_deposit_amount is not None:
            if data.get('type') != 'BUY':
                raise ValueError('Powiązana wpłata jest dostępna tylko przy zakupie.')
            attach_deposit(conn, new_id, float(funding_deposit_amount), funding_follows)
        elif previous is not None:
            if previous['type'] == 'BUY':
                sync_deposit_after_purchase_edit(conn, new_id)
            elif previous['type'] == 'DEPOSIT' and previous['linked_purchase_id'] is not None:
                unlink_deposit_on_manual_edit(conn, new_id,
                    float(previous['cash_amount'] or 0), float(data.get('cash_amount') or 0))
        calculate_ledger(conn)
        days = [d for d in (old_date, str(data['occurred_at'])[:10]) if d]
        rebuild_snapshots(conn, min(days) if days else None, commit=False)
        conn.execute('RELEASE SAVEPOINT transaction_check')
        conn.commit()
        return new_id
    except Exception:
        try:
            conn.execute('ROLLBACK TO SAVEPOINT transaction_check')
            conn.execute('RELEASE SAVEPOINT transaction_check')
        finally:
            conn.rollback()
        raise


with connect() as conn:
    overview_error = None
    try:
        overview = value_portfolio(conn)
    except LedgerError as exc:
        overview = None
        overview_error = str(exc)
    pages = ["Pulpit", "Operacje", "Aktywa i rachunki", "Staking", "Wyceny i historia", "Wyniki i benchmark", "Notatki i kalendarz", "Kontrola danych", "Ustawienia i kopie"]
    page = st.sidebar.radio("Nawigacja", pages)
    st.sidebar.divider()
    st.sidebar.caption(f"Baza danych: {DB_PATH.name}")
    st.sidebar.caption(f"Ostatnia synchronizacja: {get_setting(conn, 'last_sync_date', 'brak') or 'brak'}")
    st.sidebar.divider()
    st.sidebar.subheader("Kursy walut NBP")
    # This panel deliberately displays *official NBP* observations only; user
    # adjustments to a single transaction should never masquerade as NBP data.
    from portfolio.market import fetch_nbp_rates
    attempts = st.session_state.setdefault("sidebar_nbp_tried", set())
    for ccy in ("EUR", "USD"):
        fx_row = conn.execute("""SELECT rate_date,rate_to_pln FROM fx_history
            WHERE currency=? AND source='NBP' AND rate_date<=?
            ORDER BY rate_date DESC LIMIT 1""", (ccy, date.today().isoformat())).fetchone()
        if fx_row is None and ccy not in attempts:
            attempts.add(ccy)
            try:
                recent = fetch_nbp_rates(ccy, date.today() - timedelta(days=14), date.today())
                for d, rate in recent:
                    conn.execute("""INSERT INTO fx_history(currency,rate_date,rate_to_pln,source)
                        VALUES(?,?,?,'NBP') ON CONFLICT(currency,rate_date,source)
                        DO UPDATE SET rate_to_pln=excluded.rate_to_pln,updated_at=CURRENT_TIMESTAMP""",
                        (ccy, d, rate))
                conn.commit()
                fx_row = conn.execute("""SELECT rate_date,rate_to_pln FROM fx_history
                    WHERE currency=? AND source='NBP' ORDER BY rate_date DESC LIMIT 1""", (ccy,)).fetchone()
            except Exception:
                pass
        if fx_row:
            rate = f"{float(fx_row['rate_to_pln']):.4f}".replace('.', ',')
            st.sidebar.metric(f"{ccy}/PLN", f"{rate} zł")
            st.sidebar.caption(f"Kurs z dnia {fx_row['rate_date']}")
        else:
            st.sidebar.caption(f"{ccy}/PLN: brak dostępnego kursu NBP")
    if st.sidebar.button("Odśwież kursy NBP", key="sidebar_refresh_fx"):
        try:
            saved = 0
            for ccy in ("EUR", "USD"):
                recent = fetch_nbp_rates(ccy, date.today() - timedelta(days=14), date.today())
                for d, rate in recent:
                    conn.execute("""INSERT INTO fx_history(currency,rate_date,rate_to_pln,source)
                        VALUES(?,?,?,'NBP') ON CONFLICT(currency,rate_date,source)
                        DO UPDATE SET rate_to_pln=excluded.rate_to_pln,updated_at=CURRENT_TIMESTAMP""",
                        (ccy, d, rate))
                    saved += 1
            conn.commit()
            st.sidebar.success(f"Sprawdzono dane NBP ({saved} notowań).")
            st.rerun()
        except Exception as exc:
            st.sidebar.warning(f"Nie udało się pobrać kursów: {exc}")
    st.sidebar.caption("Źródło: NBP, tabela A · waluta bazowa PLN")

    if page == "Pulpit":
        st.title("Mój Portfel")
        if overview is None:
            st.error("Nie można bezpiecznie obliczyć portfela. Nieprawidłowa operacja: " + (overview_error or "nieznany błąd"))
            st.warning("Wyniki nie są wyświetlane, aby nie pokazywać błędnych wartości. Przejdź do zakładki Operacje, popraw lub usuń wskazany wpis, a następnie wróć na pulpit.")
            recent = rows(conn, "SELECT id,occurred_at,type,notes FROM transactions ORDER BY occurred_at DESC,id DESC LIMIT 20")
            if recent:
                st.dataframe(pd.DataFrame([dict(r) for r in recent]), hide_index=True, use_container_width=True)
        else:
            st.caption("Wersja demonstracyjna online · wyłącznie dane fikcyjne · waluta bazowa PLN")
            total = overview["total_value_pln"]
            k1, k2, k3, k4 = st.columns(4)
            k1.metric("Wartość portfela", money(total), help="Wycena aktywów i gotówki na podstawie ostatnich dostępnych cen i kursów.")
            k2.metric("Łączny wynik", money(overview["total_pnl_pln"]), help="Wartość netto portfela pomniejszona o wpłaty zewnętrzne i powiększona o wypłaty. Obejmuje też wycenę gotówki w walutach obcych.")
            k3.metric("Wynik niezrealizowany", money(overview["unrealized_pln"]))
            k4.metric("Wynik zrealizowany", money(overview["realized_pln"] + overview["income_pln"]),
                      help="Wynik ze sprzedaży plus faktycznie wypłacone odsetki (bez fikcyjnej wyceny nagród kryptowalutowych).")
            if not overview["valuation_complete"]:
                st.warning("Wycena jest niekompletna. Suma składników z dostępną ceną: " + money(overview["partial_total_value_pln"]) + ". Wynik całkowity nie jest pokazywany jako kompletna wartość. Brakujące wyceny/kursy: " + ", ".join(overview["missing_prices"]))
            if not overview["positions"] and not overview["cash"]:
                st.info("Portfel jest pusty. Zacznij od dodania rachunku, aktywa i pierwszej operacji.")
            col1, col2 = st.columns([1.1, 1])
            with col1:
                st.subheader("Struktura portfela")
                alloc = []
                for p in overview["valued_positions"]:
                    if p["market_value_pln"] is not None and p["market_value_pln"] > 0:
                        alloc.append({"Składnik": p["symbol"], "Wartość PLN": p["market_value_pln"], "Rodzaj": p["category"]})
                for c in overview["valued_cash"]:
                    if c["value_pln"] is not None and c["value_pln"] > 0:
                        alloc.append({"Składnik": f"Gotówka {c['currency']} · {c['account']}", "Wartość PLN": c["value_pln"], "Rodzaj": "Gotówka"})
                if alloc:
                    df = pd.DataFrame(alloc)
                    fig = px.pie(df, names="Składnik", values="Wartość PLN", hole=0.45)
                    st.plotly_chart(fig, use_container_width=True)
                else:
                    st.caption("Brak wycenionych składników portfela.")
            with col2:
                st.subheader("Wartość według kategorii")
                cat_data = {}
                for p in overview["valued_positions"]:
                    if p["market_value_pln"] is not None:
                        cat_data[p["category"]] = cat_data.get(p["category"], 0) + p["market_value_pln"]
                for c in overview["valued_cash"]:
                    if c["value_pln"] is not None:
                        cat_data["Gotówka"] = cat_data.get("Gotówka", 0) + c["value_pln"]
                if cat_data:
                    st.dataframe(pd.DataFrame([{"Kategoria": k, "Wartość": money(v)} for k, v in cat_data.items()]), hide_index=True, use_container_width=True)
                st.metric("Wartość netto wpłat zewnętrznych", money(overview["net_contributions_pln"]))
                st.metric("Koszt aktualnie posiadanych aktywów", money(overview["cost_total_pln"]))
                st.metric("Zarejestrowane opłaty", money(overview["fees_pln"]))
            st.subheader("Posiadane aktywa")
            if overview["valued_positions"]:
                pos = pd.DataFrame([{"Ticker": p["symbol"], "Nazwa": p["name"], "Kategoria": p["category"],
                                     "Ilość": p["quantity"], "Jednostka": p["unit"], "Średni koszt (PLN)": p["avg_cost_pln"],
                                     "Średni koszt ({})".format(p["currency"]): p["avg_cost_asset_currency"], "Cena": p["price"], "Waluta ceny": p["price_currency"], "Data ceny": p["price_date"] or "—",
                                     "Wartość (PLN)": p["market_value_pln"], "Wynik (PLN)": p["unrealized_pln"], "Odsetki i nagrody (PLN)": p.get("income_pln", 0.0)}
                                    for p in overview["valued_positions"]])
                st.dataframe(pos, hide_index=True, use_container_width=True)
            st.subheader("Gotówka na rachunkach")
            if overview["valued_cash"]:
                cash_df = pd.DataFrame([{"Rachunek": c["account"], "Waluta": c["currency"], "Saldo": c["balance"], "Wartość PLN": c["value_pln"]} for c in overview["valued_cash"]])
                st.dataframe(cash_df, hide_index=True, use_container_width=True)
            st.subheader("Historia wartości i wpłaconego kapitału")
            if st.button("Odśwież historię portfela", key="refresh_portfolio_history"):
                try:
                    with st.spinner("Przeliczanie historii z zapisanych transakcji i dostępnych notowań…"):
                        count = rebuild_snapshots(conn)
                    st.success(f"Przeliczono {count} dni, w tym bieżący dzień. Dni bez kompletnych cen pozostają oznaczone jako niekompletne.")
                except Exception as exc:
                    st.error(f"Nie udało się przeliczyć historii: {exc}")
            history = portfolio_history_series(conn)
            if history["series"]:
                hdf = pd.DataFrame(history["series"])
                period = st.selectbox("Zakres historii", ["1 miesiąc", "3 miesiące", "6 miesięcy", "1 rok", "Całość"], index=4)
                period_days = {"1 miesiąc":30, "3 miesiące":90, "6 miesięcy":180, "1 rok":365}.get(period)
                if period_days:
                    hdf = hdf[hdf["snapshot_date"] >= (date.today() - timedelta(days=period_days)).isoformat()]
                chart = hdf.melt(id_vars=["snapshot_date"], value_vars=["portfolio_value_pln", "net_contributions_pln"],
                                 var_name="Seria", value_name="Wartość PLN")
                chart["Seria"] = chart["Seria"].replace({"portfolio_value_pln": "Wartość portfela", "net_contributions_pln": "Kapitał wpłacony netto"})
                chart["snapshot_date"] = pd.to_datetime(chart["snapshot_date"])
                fig = px.line(chart, x="snapshot_date", y="Wartość PLN", color="Seria",
                    markers=True, labels={"snapshot_date": "Data"})
                fig.update_xaxes(tickformat="%d.%m.%Y")
                st.plotly_chart(fig, use_container_width=True)
                st.caption("Kapitał netto to wpłaty zewnętrzne pomniejszone o wypłaty. Transfery między własnymi rachunkami nie zmieniają tej linii.")
            elif not history["complete"]:
                st.warning(history["message"])
            else:
                st.caption("Wykres pojawi się po zapisaniu pierwszych migawek wyceny.")
            with st.expander("Status, alerty i kontrola danych"):
                if overview["warnings"]:
                    for warning in overview["warnings"]:
                        st.warning(warning)
                stale = []
                for p in overview["valued_positions"]:
                    if p["price_date"]:
                        age = (date.today() - date.fromisoformat(p["price_date"])).days
                        threshold = 1 if p["category"] == "Kryptowaluty" else 7
                        if age > threshold:
                            stale.append(p)
                for p in stale:
                    st.warning(f"Cena {p['symbol']} jest nieaktualna (ostatnia wycena: {p['price_date']}).")
                for tranche in list_bond_tranches(conn):
                    try:
                        days = (date.fromisoformat(tranche["maturity_date"]) - date.today()).days
                        if 0 <= days <= 90:
                            st.warning(f"Transza {tranche['symbol']} (zakup {tranche['purchase_date']}) zbliża się do wykupu za {days} dni ({tranche['maturity_date']}).")
                        elif days < 0:
                            st.warning(f"Termin wykupu transzy {tranche['symbol']} (zakup {tranche['purchase_date']}) minął {-days} dni temu. Sprawdź i zarejestruj wykup.")
                    except ValueError:
                        pass
                if not overview["warnings"] and not stale:
                    st.success("Rejestr transakcji przeszedł kontrolę spójności.")
    elif page == "Operacje":
        st.title("Rejestr operacji")
        st.caption("Operacje są zapisywane w historii. Zmiana wcześniejszej operacji powoduje ponowne przeliczenie portfela.")
        account_opts = options_accounts(conn)
        asset_opts = options_assets(conn)
        from portfolio.transaction_form import render_transaction_form
        render_transaction_form(conn, account_opts, asset_opts, save_tx_checked)

        st.subheader("Historia transakcji")
        txs = rows(conn, """SELECT t.*, a.name account_name, a2.name to_account_name, x.symbol asset_symbol, x2.symbol to_asset_symbol
            FROM transactions t LEFT JOIN accounts a ON a.id=t.account_id LEFT JOIN accounts a2 ON a2.id=t.to_account_id
            LEFT JOIN assets x ON x.id=t.asset_id LEFT JOIN assets x2 ON x2.id=t.to_asset_id ORDER BY occurred_at DESC,id DESC""")
        if txs:
            labels = {"BUY":"Kupno", "SELL":"Sprzedaż / wykup", "DEPOSIT":"Wpłata", "WITHDRAWAL":"Wypłata",
                "DIVIDEND":"Dywidenda", "INTEREST":"Nagroda — odsetki", "FEE":"Opłata gotówkowa",
                "REWARD":"Nagroda — staking", "STAKING_REWARD":"Nagroda stakingowa (starszy zapis)",
                "ASSET_TRANSFER":"Transfer aktywa", "CASH_TRANSFER":"Transfer gotówki",
                "FX_CONVERSION":"Wymiana waluty", "SWAP":"Swap", "TOKEN_FEE":"Opłata tokenowa"}
            cfilter1, cfilter2, cfilter3 = st.columns(3)
            symbol_filter = cfilter1.selectbox('Instrument w historii', ['Wszystkie'] + sorted(
                {str(r['asset_symbol']) for r in txs if r['asset_symbol']}), key='history_filter_symbol')
            type_filter = cfilter2.selectbox('Rodzaj operacji w historii', ['Wszystkie'] + sorted(
                {str(r['type']) for r in txs}), format_func=lambda x: labels.get(x,x), key='history_filter_type')
            date_filter = cfilter3.selectbox('Zakres historii', ['Całość','30 dni','90 dni','365 dni'],
                                           key='history_filter_period')
            days_limit = {'30 dni':30,'90 dni':90,'365 dni':365}.get(date_filter)
            start_date = (date.today()-timedelta(days=days_limit)).isoformat() if days_limit else None
            visible_txs = [r for r in txs if
                (symbol_filter == 'Wszystkie' or r['asset_symbol'] == symbol_filter) and
                (type_filter == 'Wszystkie' or r['type'] == type_filter) and
                (start_date is None or str(r['occurred_at'])[:10] >= start_date)]
            if visible_txs:
                tx_df = pd.DataFrame([{'Lp.':i, 'Data': r['occurred_at'][:10],
                    'Operacja':labels.get(r['type'],r['type']), 'Aktywo':r['asset_symbol'] or '—',
                    'Ilość':r['quantity'], 'Kwota':r['cash_amount'], 'Waluta':r['cash_currency'] or '—',
                    'Rachunek':r['account_name'] or '—', 'Notatka':r['notes'] or ''}
                    for i,r in enumerate(visible_txs,1)])
                st.dataframe(tx_df, hide_index=True, use_container_width=True)
                st.markdown('**Szczegóły operacji — rozwiń wybrane wpisy**')
                for i, r in enumerate(visible_txs[:30],1):
                    with st.expander(f"Lp. {i} · {r['occurred_at'][:10]} · {labels.get(r['type'],r['type'])} · {r['asset_symbol'] or 'gotówka'}"):
                        st.write(f"Rachunek: {r['account_name'] or '—'} · instrument: {r['asset_symbol'] or '—'}")
                        st.write(f"Ilość: {r['quantity'] if r['quantity'] is not None else '—'} · "
                                 f"cena jednostkowa: {r['unit_price'] if r['unit_price'] is not None else '—'} "
                                 f"{r['price_currency'] or ''}")
                        st.write(f"Kwota rozliczenia: {r['cash_amount'] if r['cash_amount'] is not None else '—'} "
                                 f"{r['cash_currency'] or ''} · prowizja: {r['fee_amount'] or 0} {r['fee_currency'] or ''}")
                        st.write(f"Kurs kwoty rozliczenia do PLN: {r['cash_fx_to_pln'] or '—'}")
                        st.write(f"Notatka: {r['notes'] or '—'}")
                        if r['linked_purchase_id'] is not None:
                            st.caption('Wpłata powiązana z zakupem; techniczne ID zakupu dostępne dla diagnostyki: '
                                       f"{r['linked_purchase_id']}.")
                if len(visible_txs) > 30:
                    st.caption('Wyświetlono pierwszych 30 szczegółów. Zawęź listę filtrami, aby zobaczyć kolejne.')
            else:
                st.info('Brak operacji odpowiadających filtrom.')
            st.subheader("Popraw lub usuń operację")
            selected_tx = st.selectbox("Operacja do edycji lub usunięcia", [int(r['id']) for r in (visible_txs or txs)],
                format_func=lambda x: next(r['occurred_at'][:10]+' · '+labels.get(r['type'],r['type'])+
                    ' · '+(r['asset_symbol'] or 'gotówka') for r in txs if int(r['id'])==x))
            current = next(dict(r) for r in txs if int(r["id"]) == selected_tx)
            edit_bond_tranche_purchase_id = current.get("bond_tranche_purchase_id")
            if current["type"] == "SELL" and current.get("asset_id"):
                edited_asset = conn.execute("SELECT category FROM assets WHERE id=?", (current["asset_id"],)).fetchone()
                if edited_asset and edited_asset["category"] == "Obligacje":
                    tranche_rows = rows(conn, """SELECT bt.purchase_transaction_id,t.occurred_at,bt.maturity_date
                        FROM bond_tranches bt JOIN transactions t ON t.id=bt.purchase_transaction_id
                        WHERE t.asset_id=? ORDER BY t.occurred_at,t.id""", (current["asset_id"],))
                    tranche_edit_options = [None] + [int(r["purchase_transaction_id"]) for r in tranche_rows]
                    tranche_edit_labels = {None: "Podział proporcjonalny (stara operacja)"}
                    tranche_edit_labels.update({int(r["purchase_transaction_id"]):
                        f"Zakup {r['occurred_at'][:10]} · wykup {r['maturity_date'] or 'brak terminu'}" for r in tranche_rows})
                    current_choice = edit_bond_tranche_purchase_id if edit_bond_tranche_purchase_id in tranche_edit_options else None
                    edit_bond_tranche_purchase_id = st.selectbox("Transza przypisana do sprzedaży / wykupu",
                        tranche_edit_options, index=tranche_edit_options.index(current_choice),
                        format_func=lambda x: tranche_edit_labels[x], key=f"edit_sale_bond_tranche_{selected_tx}")
            with st.form("edit_transaction"):
                st.caption("Możesz poprawić datę, ilość, kwoty, kursy, prowizję i notatkę. Rodzaj operacji, aktywa i rachunki pozostają bez zmian.")
                c1, c2, c3 = st.columns(3)
                edit_date = c1.text_input("Data (RRRR-MM-DD)", value=current["occurred_at"][:10])
                edit_quantity = c2.number_input("Ilość", value=float(current["quantity"] or 0), format="%.8f")
                edit_unit_price = c3.number_input("Cena jednostkowa", value=float(current["unit_price"] or 0), format="%.8f")
                c1, c2, c3 = st.columns(3)
                edit_cash = c1.number_input("Kwota gotówki", value=float(current["cash_amount"] or 0), format="%.8f")
                edit_cash_fx = c2.number_input("Kurs gotówki do PLN", value=float(current["cash_fx_to_pln"] or (1 if current["cash_currency"] == "PLN" else 0)), format="%.8f")
                edit_fee = c3.number_input("Opłata", value=float(current["fee_amount"] or 0), format="%.8f")
                c1, c2, c3 = st.columns(3)
                edit_price_fx = c1.number_input("Kurs ceny do PLN", value=float(current["price_fx_to_pln"] or (1 if current["price_currency"] == "PLN" else 0)), format="%.8f")
                edit_fee_fx = c2.number_input("Kurs opłaty do PLN", value=float(current["fee_fx_to_pln"] or (1 if current["fee_currency"] == "PLN" else 0)), format="%.8f")
                edit_to_qty = c3.number_input("Ilość otrzymana / transfer", value=float(current["to_quantity"] or 0), format="%.8f")
                c1, c2 = st.columns(2)
                edit_to_cash = c1.number_input("Kwota otrzymana / docelowa", value=float(current["to_cash_amount"] or 0), format="%.8f")
                edit_to_cash_fx = c2.number_input("Kurs kwoty docelowej do PLN", value=float(current["to_cash_fx_to_pln"] or (1 if current["to_cash_currency"] == "PLN" else 0)), format="%.8f")
                edit_valuation = st.number_input("Wartość wymiany PLN", value=float(current["valuation_pln"] or 0), format="%.2f")
                edit_notes = st.text_input("Notatka", value=current["notes"] or "")
                has_metal_fields = current.get('metal_spot_pln_per_oz') is not None and current.get('metal_adjustment_pln_per_oz') is not None
                edit_metal_spot = edit_metal_adjust = None
                if has_metal_fields:
                    st.caption('Dla metalu cena brutto jest wyliczana z ceny kruszcu i korekty; pozostałe pola kwoty są poglądowe.')
                    m1, m2 = st.columns(2)
                    edit_metal_spot = m1.number_input('Rynkowa cena metalu za 1 oz (PLN)',
                        min_value=0.0, value=float(current['metal_spot_pln_per_oz']), format='%.4f')
                    edit_metal_adjust = m2.number_input('Korekta ceny za 1 oz (PLN)',
                        value=float(current['metal_adjustment_pln_per_oz']), format='%.4f')
                update_btn = st.form_submit_button("Zapisz poprawki", type="primary")
            if update_btn:
                try:
                    updated = {k: current.get(k) for k in ["occurred_at","type","account_id","to_account_id","asset_id","to_asset_id","quantity","to_quantity","unit_price","price_currency","price_fx_to_pln","cash_amount","cash_currency","cash_fx_to_pln","to_cash_amount","to_cash_currency","to_cash_fx_to_pln","fee_amount","fee_currency","fee_fx_to_pln","valuation_pln","bond_tranche_purchase_id",
                        "metal_spot_pln_per_oz","metal_adjustment_pln_per_oz",
                        "linked_purchase_id", "linked_deposit_follows_purchase",
                        "price_fx_rate_date","price_fx_source","cash_fx_rate_date","cash_fx_source",
                        "fee_fx_rate_date","fee_fx_source","notes"]}
                    updated["bond_tranche_purchase_id"] = edit_bond_tranche_purchase_id
                    updated.update({"occurred_at": edit_date, "quantity": edit_quantity or None, "unit_price": edit_unit_price or None,
                                    "price_fx_to_pln": edit_price_fx or None, "cash_amount": edit_cash or None, "cash_fx_to_pln": edit_cash_fx or None,
                                    "fee_amount": edit_fee, "fee_fx_to_pln": edit_fee_fx or None,
                                    "to_quantity": edit_to_qty or None, "to_cash_amount": edit_to_cash or None,
                                    "to_cash_fx_to_pln": edit_to_cash_fx or None, "valuation_pln": edit_valuation or None,
                                    "notes": edit_notes})
                    date.fromisoformat(edit_date)
                    # Retain provenance when unchanged; edited values become manual
                    # transaction rates, never falsely labelled as NBP.
                    for prefix, old_rate, new_rate in (
                        ('price', current.get('price_fx_to_pln'), updated.get('price_fx_to_pln')),
                        ('cash', current.get('cash_fx_to_pln'), updated.get('cash_fx_to_pln')),
                        ('fee', current.get('fee_fx_to_pln'), updated.get('fee_fx_to_pln')),
                    ):
                        if old_rate != new_rate and new_rate is not None:
                            updated[f'{prefix}_fx_source'] = 'manual'
                            updated[f'{prefix}_fx_rate_date'] = edit_date
                    if has_metal_fields:
                        from portfolio.operations import metal_trade_values
                        metal_update = metal_trade_values(edit_quantity, edit_metal_spot, edit_metal_adjust)
                        updated['metal_spot_pln_per_oz'] = metal_update['spot_pln_per_oz']
                        updated['metal_adjustment_pln_per_oz'] = metal_update['adjustment_pln_per_oz']
                        updated['unit_price'] = metal_update['spot_pln_per_oz']
                        updated['cash_amount'] = metal_update['cash_amount_pln']
                        updated['price_currency'] = updated['cash_currency'] = 'PLN'
                        updated['price_fx_to_pln'] = updated['cash_fx_to_pln'] = 1.0
                        updated['price_fx_rate_date'] = updated['cash_fx_rate_date'] = edit_date
                        updated['price_fx_source'] = updated['cash_fx_source'] = 'PLN'
                    save_tx_checked(conn, updated, selected_tx)
                    st.success("Operacja poprawiona, wyniki zostały przeliczone.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Nie udało się poprawić operacji: {exc}")
            if st.button("Usuń wybraną operację", type="secondary", key="request_delete_tx"):
                st.session_state["confirm_delete_tx"] = int(selected_tx)
            if st.session_state.get("confirm_delete_tx") == int(selected_tx):
                linked_deposit = conn.execute('SELECT id FROM transactions WHERE linked_purchase_id=?', (selected_tx,)).fetchone()
                st.warning(f"Czy na pewno usunąć operację z dnia {current['occurred_at']}? Ta zmiana wpłynie na późniejsze wyliczenia portfela.")
                if linked_deposit:
                    st.warning('Ten zakup ma automatycznie powiązaną wpłatę. Usunięcie zakupu usunie także tę wpłatę. Inne wpłaty pozostaną nietknięte.')
                confirm_col, cancel_col = st.columns(2)
                if confirm_col.button("Tak, usuń operację", type="primary", key="confirm_delete_tx_button"):
                    backup_db()
                    old_tx_date = current["occurred_at"][:10]
                    conn.execute("SAVEPOINT delete_check")
                    try:
                        delete_transaction(conn, selected_tx)
                        calculate_ledger(conn)
                        rebuild_snapshots(conn, old_tx_date, commit=False)
                        conn.execute("RELEASE SAVEPOINT delete_check")
                        conn.commit()
                        st.session_state.pop("confirm_delete_tx", None)
                        st.success("Operacja usunięta.")
                        st.rerun()
                    except Exception as exc:
                        try:
                            conn.execute("ROLLBACK TO SAVEPOINT delete_check")
                            conn.execute("RELEASE SAVEPOINT delete_check")
                        finally:
                            conn.rollback()
                        st.error(f"Nie można usunąć operacji: {exc}")
                if cancel_col.button("Anuluj", key="cancel_delete_tx_button"):
                    st.session_state.pop("confirm_delete_tx", None)
                    st.rerun()
        else:
            st.info("Nie ma jeszcze żadnych operacji.")

    elif page == "Aktywa i rachunki":
        st.title("Aktywa i rachunki")
        c1, c2 = st.columns(2)
        with c1:
            st.subheader("Dodaj rachunek / platformę")
            with st.form("add_account"):
                name = st.text_input("Nazwa", placeholder="np. XTB, Binance, Ledger, Binance Staking")
                kind = st.selectbox("Typ", ["broker", "crypto_exchange", "wallet", "staking", "cash", "other"], format_func=lambda x: {"broker":"Dom maklerski", "crypto_exchange":"Giełda kryptowalut", "wallet":"Portfel krypto", "staking":"Staking", "cash":"Gotówka", "other":"Inne"}[x])
                notes = st.text_input("Notatka")
                if st.form_submit_button("Dodaj rachunek"):
                    try:
                        if not name.strip(): raise ValueError("Podaj nazwę rachunku.")
                        backup_db(); add_account(conn, name, kind, notes); st.success("Dodano rachunek."); st.rerun()
                    except Exception as exc:
                        st.error(str(exc))
            st.subheader("Lista rachunków")
            accts = rows(conn, "SELECT * FROM accounts ORDER BY name")
            if accts:
                st.dataframe(pd.DataFrame([{"Nazwa": r["name"], "Typ": r["kind"], "Aktywny": bool(r["active"]), "Notatka": r["notes"]} for r in accts]), hide_index=True, use_container_width=True)
        with c2:
            st.subheader("Lista instrumentów")
            assets = rows(conn, """SELECT * FROM assets WHERE active=1 OR
                EXISTS(SELECT 1 FROM transactions WHERE asset_id=assets.id OR to_asset_id=assets.id)
                ORDER BY category,symbol""")
            if assets:
                categories = sorted({r['category'] for r in assets})
                cat_selected = st.selectbox('Pokaż kategorię', ['Wszystkie'] + categories, key='asset_list_category')
                displayed_assets = [a for a in assets if cat_selected=='Wszystkie' or a['category']==cat_selected]
                asset_records = []
                for asset in displayed_assets:
                    quote = latest_price_with_currency(conn, int(asset['id']), asset['currency'], date.today().isoformat())
                    prev_date = ((date.fromisoformat(quote['date']) - timedelta(days=1)).isoformat()
                                 if quote['date'] else None)
                    old = (latest_price_with_currency(conn, int(asset['id']), asset['currency'], prev_date)
                           if prev_date else None)
                    change = None
                    if (old and old['price'] and quote['price'] and old['currency']==quote['currency']
                            and old['source']==quote['source']):
                        change = (float(quote['price']) / float(old['price']) - 1) * 100
                    asset_records.append({'Instrument':asset['symbol'], 'Nazwa':asset['name'],
                        'Kategoria':asset['category'], 'Waluta notowania': quote['currency'],
                        'Ostatnia cena':quote['price'], 'Zmiana względem poprzedniego notowania (%)':change,
                        'Data ceny':quote['date'] or 'Brak', 'Źródło':quote['source'] or 'Brak',
                        'Giełda':asset['listing_exchange'], 'Symbol źródła':asset['price_symbol'],
                        'Jednostka':asset['unit'], 'Wpis aktywny':bool(asset['active'])})
                st.dataframe(pd.DataFrame(asset_records), hide_index=True, use_container_width=True)
                st.caption('Zmiana procentowa jest dostępna wyłącznie, gdy dwa kolejne notowania pochodzą z tego samego źródła i mają tę samą walutę. '
                           'Brak danych nie jest zastępowany zerem. Historyczne wpisy EUR pozostają widoczne, jeśli mają transakcje.')
                st.caption("Lista zawiera przygotowane instrumenty. Same wpisy w tej tabeli nie oznaczają, że posiadasz dane aktywo — stan portfela zmieniają dopiero zapisane operacje.")
                st.subheader("Szczegóły aktywa")
                detail_ids = [int(r["id"]) for r in assets]
                detail_asset_id = st.selectbox("Wybierz instrument", detail_ids,
                    format_func=lambda x: next(f"{r['symbol']} — {r['name']}" for r in assets if int(r["id"]) == x), key="asset_detail_id")
                detail_asset = next(r for r in assets if int(r["id"]) == detail_asset_id)
                if detail_asset["notes"]:
                    st.caption(f"Informacje o instrumencie: {detail_asset['notes']}")
                try:
                    detail_value = value_portfolio(conn)
                    detail_position = next((p for p in detail_value["valued_positions"] if int(p["asset_id"]) == detail_asset_id), None)
                except LedgerError as exc:
                    detail_position = None
                    st.warning(f"Nie można teraz obliczyć szczegółów aktywa: {exc}")
                if detail_position:
                    d1, d2, d3, d4 = st.columns(4)
                    d1.metric("Ilość", f"{detail_position['quantity']:.8f} {detail_position['unit']}")
                    d2.metric("Średni koszt ({})".format(detail_position["currency"]), money(detail_position["avg_cost_asset_currency"], detail_position["currency"]))
                    d3.metric("Średni koszt w PLN", money(detail_position["avg_cost_pln"]))
                    d4.metric("Aktualna wartość", money(detail_position["market_value_pln"]))
                    d5, d6, d7 = st.columns(3)
                    d5.metric("Wynik niezrealizowany", money(detail_position["unrealized_pln"]))
                    d6.metric("Przychody z tego aktywa", money(detail_position.get("income_pln", 0.0)))
                    d7.metric("Ostatnia cena", f"{detail_position['price']} {detail_position['price_currency']}" if detail_position["price"] is not None else "Brak ceny")
                    st.caption(f"Data ostatniej ceny: {detail_position['price_date'] or '—'} · Źródło: {detail_position['price_source'] or '—'}")
                else:
                    st.info("Nie masz obecnie otwartej pozycji w tym aktywie. Średni koszt pojawi się po zarejestrowaniu zakupu.")
                detail_transactions = rows(conn, """SELECT occurred_at,type,quantity,cash_amount,cash_currency,unit_price,price_currency,notes
                    FROM transactions WHERE asset_id=? OR to_asset_id=? ORDER BY occurred_at DESC,id DESC LIMIT 20""",
                    (detail_asset_id, detail_asset_id))
                if detail_transactions:
                    st.caption("Ostatnie operacje dotyczące instrumentu")
                    st.dataframe(pd.DataFrame([{"Data": r["occurred_at"], "Typ": r["type"], "Ilość": r["quantity"],
                        "Kwota": r["cash_amount"], "Waluta": r["cash_currency"] or r["price_currency"] or "—", "Cena": r["unit_price"], "Notatka": r["notes"]}
                        for r in detail_transactions]), hide_index=True, use_container_width=True)
        st.info("Rachunek/platforma (np. XTB lub Binance) jest niezależny od giełdy notowań instrumentu. Aktywo można przenosić między rachunkami bez traktowania transferu jako sprzedaży.")

    elif page == "Staking":
        st.title("Staking kryptowalut")
        st.caption("Blokowanie i odblokowywanie jest zapisywane jako transfer pomiędzy rachunkami. Pokazujemy zapisane APY, ale nagrody zwiększają portfel dopiero po ich rzeczywistym otrzymaniu i zarejestrowaniu.")
        st.info("Blokowanie, odblokowanie i nagrody dodajesz teraz w jednym formularzu: Operacje → Dodaj operację. Tutaj przeglądasz swoje pozycje stakingowe.")
        st.subheader("Pozycje stakingowe")
        plans = rows(conn, """SELECT p.*,a.symbol,a.name asset_name,a.currency,src.name source_name,dst.name staking_name
            FROM staking_plans p JOIN assets a ON a.id=p.asset_id JOIN accounts src ON src.id=p.source_account_id
            JOIN accounts dst ON dst.id=p.staking_account_id ORDER BY p.start_date DESC,p.id DESC""")
        if plans:
            plan_rows=[]
            for raw in plans:
                plan=dict(raw)
                plan_rows.append({"ID":plan['id'],"Status":"Aktywny" if plan['active'] else "Zakończony","Aktywo":plan['symbol'],
                    "Ilość zablokowana":plan['quantity'],"Rachunek":plan['staking_name'],"Od":plan['start_date'],
                    "Planowany koniec":plan['planned_end_date'] or "Bez terminu","APY (%)":plan['apy_pct']})
            st.dataframe(pd.DataFrame(plan_rows), hide_index=True, use_container_width=True)
        else:
            st.info("Nie masz jeszcze zarejestrowanych planów stakingowych.")
        st.subheader("Rzeczywiście otrzymane nagrody")
        rewards=rows(conn, """SELECT t.occurred_at,a.symbol,t.quantity,t.unit_price,t.price_currency,ac.name account_name,t.notes
            FROM transactions t JOIN assets a ON a.id=t.asset_id JOIN accounts ac ON ac.id=t.account_id
            WHERE t.type IN ('STAKING_REWARD','REWARD') ORDER BY t.occurred_at DESC,t.id DESC""")
        if rewards:
            st.dataframe(pd.DataFrame([dict(r) for r in rewards]), hide_index=True, use_container_width=True)
        else:
            st.caption("Nagrody pojawią się po zapisaniu rzeczywistego wpływu w zakładce Operacje.")

    elif page == "Wyceny i historia":
        st.title("Wyceny i historia")
        st.caption("Ceny instrumentów są pobierane automatycznie. W każdej chwili możesz ręcznie uruchomić odświeżenie; nie ma ręcznego wpisywania bieżących cen aktywów.")
        c1, c2 = st.columns([1, 1])
        with c1:
            if st.button("Pobierz najnowsze ceny i kursy", type="primary"):
                with st.spinner("Pobieranie dostępnych danych rynkowych…"):
                    result = sync_market_data(conn, days_back=14, full_history=False, force=True)
                    try:
                        sync_spy(conn)
                    except Exception as exc:
                        result["errors"].append(f"Benchmark SPY: {exc}")
                    try:
                        save_snapshot(conn)
                        fill_missing_snapshots(conn)
                    except LedgerError as exc:
                        result["errors"].append(f"Nie zapisano migawki z powodu błędu w historii: {exc}")
                st.success(f"Zapisano {result['prices_saved']} cen i {result['fx_saved']} kursów walut.")
                if result["fallbacks_used"]:
                    st.info("Użyto zapasowych notowań: " + "; ".join(result["fallbacks_used"]))
                if result["errors"]:
                    st.warning("Nie wszystkie źródła odpowiedziały: " + "; ".join(result["errors"]))
                st.rerun()
        with c2:
            if st.button("Pobierz pełną dostępną historię"):
                with st.spinner("Uzupełnianie historii cen i kursów. Może to chwilę potrwać…"):
                    result = sync_market_data(conn, full_history=True)
                    try:
                        sync_spy(conn, full_history=True)
                    except Exception as exc:
                        result["errors"].append(f"Benchmark SPY: {exc}")
                    try:
                        fill_missing_snapshots(conn)
                    except LedgerError as exc:
                        result["errors"].append(f"Nie przeliczono migawek z powodu błędu w historii: {exc}")
                    st.info(f"Zapisano {result['prices_saved']} cen i {result['fx_saved']} kursów.")
                    if result["errors"]:
                        st.warning("Błędy źródeł: " + "; ".join(result["errors"]))
                st.rerun()
        st.info("Bieżące ceny aktywów są pobierane automatycznie. Obligacje są wyceniane uproszczonym modelem na podstawie nominału i oprocentowania; nie wymagają notowania rynkowego.")
        st.subheader("Ręczna korekta kursu walutowego")
        with st.form("manual_fx"):
            f1, f2, f3 = st.columns(3)
            fx_currency = f1.selectbox("Waluta", ["USD", "EUR"])
            fx_date = f2.date_input("Data kursu", value=date.today())
            fx_rate = f3.number_input("Kurs do PLN", min_value=0.000001, value=1.0, format="%.6f")
            if st.form_submit_button("Zapisz kurs ręczny"):
                try:
                    backup_db(); upsert_fx(conn, fx_currency, fx_date.isoformat(), fx_rate, "manual"); rebuild_snapshots(conn, fx_date.isoformat()); st.success("Kurs zapisany."); st.rerun()
                except Exception as exc: st.error(str(exc))
        st.subheader("Ostatnie wyceny")
        prices = rows(conn, "SELECT p.price_date,a.symbol,p.price,p.currency,p.source FROM price_history p JOIN assets a ON a.id=p.asset_id ORDER BY p.price_date DESC LIMIT 100")
        if prices:
            st.dataframe(pd.DataFrame([dict(r) for r in prices]), hide_index=True, use_container_width=True)
        st.subheader("Migawki portfela")
        snaps = rows(conn, "SELECT snapshot_date,total_value_pln,COALESCE(manual_total_value_pln,total_value_pln) AS wartosc_efektywna,manual_notes,assets_value_pln,cash_value_pln FROM portfolio_snapshots ORDER BY snapshot_date DESC LIMIT 100")
        if snaps:
            st.dataframe(pd.DataFrame([dict(r) for r in snaps]), hide_index=True, use_container_width=True)
            st.subheader("Ręczna korekta wartości migawki")
            snap_dates = [r["snapshot_date"] for r in snaps]
            selected_day = st.selectbox("Dzień migawki", snap_dates)
            selected_row = next(r for r in snaps if r["snapshot_date"] == selected_day)
            with st.form("snapshot_override"):
                override_value = st.number_input("Wartość portfela w tym dniu (PLN)", min_value=0.0, value=float(selected_row["wartosc_efektywna"] or 0), format="%.2f")
                override_note = st.text_input("Powód / notatka korekty", value=selected_row["manual_notes"] or "")
                save_override = st.form_submit_button("Zapisz ręczną wartość migawki")
            if save_override:
                backup_db(); set_snapshot_override(conn, selected_day, override_value, override_note); st.success("Korekta migawki zapisana. Ponowne przeliczenie historii jej nie nadpisze."); st.rerun()
            if st.button("Usuń korektę wybranej migawki"):
                backup_db(); set_snapshot_override(conn, selected_day, None, ""); st.success("Usunięto korektę."); st.rerun()
            if st.button("Przelicz ponownie wszystkie zapisane migawki"):
                with st.spinner("Przeliczanie historii…"):
                    count = rebuild_snapshots(conn)
                st.success(f"Przeliczono {count} dni. Ręczne korekty pozostały zachowane.")

    elif page == "Wyniki i benchmark":
        st.title("Wyniki portfela")
        try:
            current = value_portfolio(conn)
        except LedgerError as exc:
            current = None
            st.error(f"Nie można obliczyć wyników z powodu błędu w rejestrze: {exc}")
            st.info("Popraw lub usuń wskazaną operację w zakładce Operacje. Do tego czasu aplikacja nie pokazuje wyników, których nie może zweryfikować.")
        if current is not None:
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Wynik łączny", money(current["total_pnl_pln"]))
            m2.metric("Zrealizowany", money(current["realized_pln"] + current["income_pln"]))
            m3.metric("Niezrealizowany", money(current["unrealized_pln"]))
            if not current["valuation_complete"]:
                st.warning("Wyniki całkowite i XIRR są niedostępne, ponieważ brakuje wycen: " + ", ".join(current["missing_prices"]))
                st.metric("Suma częściowo wycenionych składników", money(current["partial_total_value_pln"]))
            flows = external_cashflows(conn)
            xflows = list(flows)
            if current["total_value_pln"] is not None:
                xflows.append((date.today().isoformat(), current["total_value_pln"]))
                irr = xirr(xflows)
                st.metric("XIRR — roczna stopa zwrotu", f"{irr*100:.2f}%" if irr is not None else "Brak danych", help="Uwzględnia daty i kwoty wpłat oraz wypłat zewnętrznych. Wynik ma sens, gdy rejestr obejmuje wszystkie przepływy.")
            else:
                st.metric("XIRR — roczna stopa zwrotu", "Niedostępna", help="Najpierw uzupełnij brakujące wyceny aktywów i kursy walut.")
            st.caption("Wynik łączny jest liczony jako wartość netto portfela minus wpłaty zewnętrzne netto. Nie wydzielamy osobnego wyniku walutowego — zmiany wartości gotówki w obcych walutach są częścią łącznego wyniku w PLN.")
            st.subheader("Benchmark S&P 500 — SPY")
            bm = benchmark_value(conn)
            if bm["value_pln"] is None:
                st.info("Benchmark pojawi się po pobraniu historii SPY oraz kursów USD/PLN i zarejestrowaniu wpłat/wypłat zewnętrznych.")
                if st.button("Pobierz dane benchmarku SPY"):
                    try:
                        market_result = sync_market_data(conn, days_back=14, full_history=True)
                        n = sync_spy(conn, full_history=True)
                        save_snapshot(conn)
                        fill_missing_snapshots(conn)
                        st.success(f"Zapisano {n} notowań SPY, {market_result['fx_saved']} kursów walut i uzupełniono historię migawek.")
                        if market_result["errors"]:
                            st.warning("Część źródeł nie odpowiedziała: " + "; ".join(market_result["errors"]))
                        st.rerun()
                    except Exception as exc:
                        st.error(str(exc))
            else:
                col1, col2, col3 = st.columns(3)
                col1.metric("Twój portfel", money(current["total_value_pln"]))
                col2.metric("Symulacja SPY", money(bm["value_pln"]))
                if current["total_value_pln"] is not None:
                    col3.metric("Różnica", money(current["total_value_pln"] - bm["value_pln"]))
                else:
                    col3.metric("Różnica", "Niedostępna")
                st.caption(f"Jednostki SPY w symulacji: {bm['units']:.6f}. Symulacja inwestuje każdą wpłatę zewnętrzną w SPY i finansuje wypłaty przez proporcjonalną sprzedaż. Nie uwzględnia prowizji ani podatków i zakłada inwestowanie całej wpłaty.")
            series = benchmark_series(conn)
            if series:
                chart_df = pd.DataFrame(series).rename(columns={"portfolio_value_pln": "Mój portfel", "spy_value_pln": "Symulacja SPY"})
                chart_long = chart_df.melt(id_vars=["date"], value_vars=["Mój portfel", "Symulacja SPY"], var_name="Seria", value_name="Wartość PLN")
                fig = px.line(chart_long, x="date", y="Wartość PLN", color="Seria", labels={"date": "Data"}, title="Wartość portfela na tle symulacji SPY")
                st.plotly_chart(fig, use_container_width=True)
            else:
                st.caption("Wspólny wykres pojawi się, gdy będą dostępne kompletne migawki portfela oraz historyczne notowania SPY i kursy USD/PLN.")
            st.subheader("Wyniki według kategorii")
            category_rows = {}
            for position in current["valued_positions"]:
                cat = position["category"]
                bucket = category_rows.setdefault(cat, {"wartosc": 0.0, "koszt": 0.0, "wynik": 0.0})
                if position["market_value_pln"] is not None:
                    bucket["wartosc"] += float(position["market_value_pln"])
                bucket["koszt"] += float(position["cost_pln"])
                if position["unrealized_pln"] is not None:
                    bucket["wynik"] += float(position["unrealized_pln"])
            for cash_item in current["valued_cash"]:
                bucket = category_rows.setdefault("Gotówka", {"wartosc": 0.0, "koszt": 0.0, "wynik": 0.0})
                if cash_item["value_pln"] is not None:
                    bucket["wartosc"] += float(cash_item["value_pln"])
            if category_rows:
                category_df = pd.DataFrame([{"Kategoria": name, "Wartość (PLN)": values["wartosc"],
                    "Koszt pozycji (PLN)": values["koszt"], "Wynik niezrealizowany (PLN)": values["wynik"]}
                    for name, values in category_rows.items()])
                st.dataframe(category_df, hide_index=True, use_container_width=True)
            st.caption("Analiza wyników jest prowadzona dla całego portfela i kategorii aktywów. Rachunki i giełdy służą do ewidencji operacji, nie do osobnych rankingów wyników.")
            st.subheader('Docelowe proporcje portfela')
            st.caption('Ustal własny udział kategorii. To informacja do planowania DCA, bez automatycznego rebalancingu i handlu.')
            target_rows = conn.execute('SELECT category,target_pct FROM allocation_targets').fetchall()
            old_targets = {r['category']:float(r['target_pct']) for r in target_rows}
            target_categories = sorted({r['category'] for r in conn.execute('SELECT category FROM assets WHERE active=1')}
                                       | {'Gotówka'} | set(old_targets))
            with st.form('allocation_targets_form'):
                target_inputs = {}
                target_cols = st.columns(2)
                for ix,cat in enumerate(target_categories):
                    with target_cols[ix % 2]:
                        target_inputs[cat] = st.number_input(f'{cat} (%)',min_value=0.0,max_value=100.0,
                            value=float(old_targets.get(cat,0.0)),step=1.0,format='%.1f',key=f'target_{cat}')
                total_target = sum(target_inputs.values())
                st.caption(f'Suma docelowych udziałów: {total_target:.1f}% (wymagane 100%).')
                submit_targets = st.form_submit_button('Zapisz docelowe proporcje',type='primary')
            if submit_targets:
                if abs(total_target - 100.0)>0.00001:
                    st.error('Suma docelowych udziałów musi wynosić dokładnie 100%.')
                else:
                    backup_db()
                    conn.executemany("INSERT INTO allocation_targets(category,target_pct) VALUES(?,?) ON CONFLICT(category) DO UPDATE SET target_pct=excluded.target_pct",
                        [(name,share) for name,share in target_inputs.items()])
                    conn.commit()
                    st.success('Zapisano docelowe proporcje.')
                    st.rerun()
            if old_targets:
                if not current['valuation_complete'] or not current['total_value_pln'] or current['total_value_pln'] <= 0:
                    st.warning('Nie można rzetelnie policzyć aktualnych udziałów: brakuje kompletnej wyceny lub portfel nie ma dodatniej wartości.')
                else:
                    total = float(current['total_value_pln'])
                    out = []
                    for category in target_categories:
                        current_value = category_rows.get(category,{}).get('wartosc',0.0)
                        pct = current_value / total * 100
                        goal = old_targets.get(category,0.0)
                        out.append({'Kategoria':category,'Aktualny udział (%)':round(pct,2),
                            'Cel (%)':round(goal,2),'Odchylenie (p.p.)':round(pct-goal,2)})
                    st.dataframe(pd.DataFrame(out),hide_index=True,use_container_width=True)
    elif page == "Notatki i kalendarz":
        st.title("Notatki i kalendarz")
        st.caption("Notatki mogą być ogólne albo przypisane do konkretnego aktywa. Kalendarz zawiera także automatyczne przypomnienia o wykupach obligacji i planowanych końcach stakingu.")
        asset_opts = options_assets(conn)
        st.subheader("Notatki")
        with st.form("add_user_note"):
            note_title = st.text_input("Tytuł notatki")
            note_asset_options = [0] + list(asset_opts)
            note_asset = st.selectbox("Powiązane aktywo (opcjonalnie)", note_asset_options,
                                      format_func=lambda x: "Cały portfel" if x == 0 else asset_opts[x])
            note_body = st.text_area("Treść", height=120)
            if st.form_submit_button("Zapisz notatkę", type="primary"):
                if not note_title.strip():
                    st.error("Podaj tytuł notatki.")
                else:
                    backup_db()
                    conn.execute("INSERT INTO user_notes(title,body,asset_id) VALUES(?,?,?)",
                                 (note_title.strip(), note_body.strip(), note_asset or None))
                    conn.commit()
                    st.success("Notatka została zapisana.")
                    st.rerun()
        notes = rows(conn, """SELECT n.*,a.symbol AS asset_symbol FROM user_notes n
            LEFT JOIN assets a ON a.id=n.asset_id ORDER BY n.updated_at DESC,n.id DESC""")
        if notes:
            for item in notes:
                title = item["title"] + (f" · {item['asset_symbol']}" if item["asset_symbol"] else " · cały portfel")
                with st.expander(title):
                    st.write(item["body"] or "(Pusta notatka)")
                    st.caption(f"Dodano: {item['created_at']} · ID: {item['id']}")
                    with st.form(f"edit_note_{item['id']}"):
                        revised_title = st.text_input("Tytuł notatki", value=item["title"])
                        revised_asset_options = [0] + list(asset_opts)
                        old_asset_value = int(item["asset_id"] or 0)
                        old_asset_index = revised_asset_options.index(old_asset_value) if old_asset_value in revised_asset_options else 0
                        revised_asset = st.selectbox("Powiązane aktywo", revised_asset_options, index=old_asset_index,
                            format_func=lambda x: "Cały portfel" if x == 0 else asset_opts[x])
                        revised_body = st.text_area("Treść", value=item["body"] or "", height=120)
                        if st.form_submit_button("Zapisz zmiany"):
                            if not revised_title.strip():
                                st.error("Tytuł notatki nie może być pusty.")
                            else:
                                backup_db()
                                conn.execute("UPDATE user_notes SET title=?,body=?,asset_id=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                                    (revised_title.strip(), revised_body.strip(), revised_asset or None, item["id"]))
                                conn.commit()
                                st.success("Notatka została zaktualizowana.")
                                st.rerun()
                    if st.button("Usuń notatkę", key=f"delete_note_{item['id']}"):
                        st.session_state["confirm_note_delete"] = int(item["id"])
                    if st.session_state.get("confirm_note_delete") == int(item["id"]):
                        st.warning("Czy na pewno usunąć tę notatkę?")
                        c_yes, c_no = st.columns(2)
                        if c_yes.button("Tak, usuń", key=f"confirm_note_{item['id']}"):
                            backup_db()
                            conn.execute("DELETE FROM user_notes WHERE id=?", (item["id"],))
                            conn.commit()
                            st.session_state.pop("confirm_note_delete", None)
                            st.rerun()
                        if c_no.button("Anuluj", key=f"cancel_note_{item['id']}"):
                            st.session_state.pop("confirm_note_delete", None)
                            st.rerun()
        else:
            st.info("Nie ma jeszcze zapisanych notatek.")

        st.divider()
        st.subheader("Kalendarz")
        with st.form("add_calendar_event"):
            c1, c2, c3 = st.columns(3)
            event_title = c1.text_input("Nazwa wydarzenia")
            event_date = c2.date_input("Data", value=date.today())
            event_type = c3.selectbox("Rodzaj", ["Przypomnienie", "Wykup obligacji", "Koniec stakingu", "Odsetki / nagroda", "Inne"])
            event_asset_options = [0] + list(asset_opts)
            event_asset = st.selectbox("Powiązane aktywo (opcjonalnie)", event_asset_options,
                                       format_func=lambda x: "Bez przypisania" if x == 0 else asset_opts[x])
            event_notes = st.text_input("Dodatkowe informacje")
            if st.form_submit_button("Dodaj wydarzenie", type="primary"):
                if not event_title.strip():
                    st.error("Podaj nazwę wydarzenia.")
                else:
                    backup_db()
                    conn.execute("INSERT INTO calendar_events(title,event_date,event_type,asset_id,notes) VALUES(?,?,?,?,?)",
                                 (event_title.strip(), event_date.isoformat(), event_type, event_asset or None, event_notes.strip()))
                    conn.commit()
                    st.success("Wydarzenie dodane do kalendarza.")
                    st.rerun()

        manual_events = rows(conn, """SELECT e.*,a.symbol AS asset_symbol FROM calendar_events e
            LEFT JOIN assets a ON a.id=e.asset_id ORDER BY e.event_date,e.id""")
        automatic_events = []
        try:
            ledger_for_calendar = calculate_ledger(conn)
            for tranche in list_bond_tranches(conn):
                if float(tranche.get("remaining_quantity", 0.0)) > 1e-9 and tranche.get("maturity_date"):
                    automatic_events.append({"Data": tranche["maturity_date"],
                        "Wydarzenie": f"Wykup transzy · {tranche['symbol']} · zakup {tranche['purchase_date']}",
                        "Rodzaj": "Automatyczne", "Status": "Otwarte"})
            for plan in rows(conn, """SELECT p.id,p.planned_end_date,a.symbol FROM staking_plans p
                JOIN assets a ON a.id=p.asset_id WHERE p.active=1 AND p.planned_end_date IS NOT NULL"""):
                automatic_events.append({"Data": plan["planned_end_date"], "Wydarzenie": f"Planowany koniec stakingu · {plan['symbol']}", "Rodzaj": "Automatyczne", "Status": "Otwarte"})
        except LedgerError as exc:
            st.warning(f"Nie udało się odczytać automatycznych przypomnień z powodu błędu księgi: {exc}")
        if automatic_events:
            st.markdown("**Automatyczne przypomnienia z portfela**")
            auto_df = pd.DataFrame(automatic_events).sort_values("Data")
            st.dataframe(auto_df, hide_index=True, use_container_width=True)
        if manual_events:
            st.markdown("**Własne wydarzenia**")
            for event in manual_events:
                status = "Zrealizowane" if event["completed"] else "Do zrobienia"
                label = f"{event['event_date']} · {event['title']} · {status}"
                with st.expander(label):
                    st.write(event["notes"] or "(Brak dodatkowych informacji)")
                    if event["asset_symbol"]:
                        st.caption(f"Aktywo: {event['asset_symbol']}")
                    c_done, c_delete = st.columns(2)
                    if not event["completed"] and c_done.button("Oznacz jako zrealizowane", key=f"complete_event_{event['id']}"):
                        conn.execute("UPDATE calendar_events SET completed=1 WHERE id=?", (event["id"],))
                        conn.commit()
                        st.rerun()
                    if c_delete.button("Usuń wydarzenie", key=f"delete_event_{event['id']}"):
                        st.session_state["confirm_event_delete"] = int(event["id"])
                    if st.session_state.get("confirm_event_delete") == int(event["id"]):
                        st.warning("Czy na pewno usunąć wydarzenie?")
                        y, n = st.columns(2)
                        if y.button("Tak, usuń", key=f"confirm_event_{event['id']}"):
                            backup_db()
                            conn.execute("DELETE FROM calendar_events WHERE id=?", (event["id"],))
                            conn.commit()
                            st.session_state.pop("confirm_event_delete", None)
                            st.rerun()
                        if n.button("Anuluj", key=f"cancel_event_{event['id']}"):
                            st.session_state.pop("confirm_event_delete", None)
                            st.rerun()
        elif not automatic_events:
            st.info("Kalendarz jest pusty. Możesz dodać własne przypomnienie.")

    elif page == "Kontrola danych":
        st.title("Kontrola poprawności danych")
        st.caption("Pełny, tylko do odczytu przegląd bazy, powiązań, księgowania, wycen i historii portfela.")
        if st.button("Uruchom pełną kontrolę ponownie", type="primary"):
            st.rerun()
        audit = run_diagnostics(conn)
        c1, c2, c3 = st.columns(3)
        c1.metric("Kontrole zakończone", len(audit["checks"]))
        c2.metric("Błędy", len(audit["errors"]))
        c3.metric("Ostrzeżenia", len(audit["warnings"]))
        if audit["ok"]:
            st.success("Nie wykryto błędów blokujących poprawne księgowanie.")
        else:
            st.error("Wykryto problemy wymagające poprawy. Do czasu ich usunięcia część wyników może być niedostępna.")
        if audit["checks"]:
            st.subheader("Wykonane kontrole")
            st.dataframe(pd.DataFrame(audit["checks"]), hide_index=True, use_container_width=True)
        if audit["errors"]:
            st.subheader("Błędy")
            for item in audit["errors"]:
                st.error(item)
        if audit["warnings"]:
            st.subheader("Ostrzeżenia i zalecenia")
            for item in audit["warnings"]:
                st.warning(item)
        if not audit["errors"] and not audit["warnings"]:
            st.info("Nie znaleziono braków ani ostrzeżeń.")

    elif page == "Ustawienia i kopie":
        st.title("Ustawienia i bezpieczeństwo danych")
        st.subheader("Obligacje — transze i założenia")
        st.caption("Każdy zakup obligacji jest osobną transzą. Zmiana przyjętej inflacji przelicza całą historię wybranej transzy, także wstecz; pozostałe transze pozostają bez zmian.")
        tranches = list_bond_tranches(conn)
        if tranches:
            bond_data = []
            for tranche in tranches:
                try:
                    forecast = estimate_tranche(tranche)
                    bond_data.append({"ID zakupu": tranche["purchase_transaction_id"], "Obligacja": tranche["symbol"],
                        "Data zakupu": tranche["purchase_date"], "Pozostała ilość": tranche.get("remaining_quantity", 0.0),
                        "Nominał": tranche["nominal_value"], "Typ": "Stałe" if tranche["bond_type"] == "fixed" else "Indeksowane inflacją",
                        "Oprocentowanie / marża (%)": forecast["assumed_rate_pct"],
                        "Założona inflacja (%)": tranche.get("assumed_inflation_pct"),
                        "Wartość dziś": forecast["current_value"], "Wartość przy wykupie": forecast["maturity_value"],
                        "Szacowane odsetki": forecast["estimated_interest"], "Termin wykupu": forecast["maturity_date"],
                        "Dni do wykupu": forecast["days_to_maturity"],
                        "Model": "Bez kapitalizacji"})
                except (TypeError, ValueError) as exc:
                    bond_data.append({"ID zakupu": tranche["purchase_transaction_id"], "Obligacja": tranche["symbol"],
                        "Data zakupu": tranche["purchase_date"], "Pozostała ilość": tranche.get("remaining_quantity", 0.0),
                        "Nominał": tranche["nominal_value"], "Typ": "Indeksowane inflacją" if tranche["bond_type"] == "inflation" else "Stałe",
                        "Założona inflacja (%)": tranche.get("assumed_inflation_pct"), "Wartość dziś": None,
                        "Wartość przy wykupie": None, "Szacowane odsetki": None,
                        "Termin wykupu": tranche["maturity_date"], "Status": str(exc)})
            st.dataframe(pd.DataFrame(bond_data), hide_index=True, use_container_width=True)

            tranche_options = {int(t["purchase_transaction_id"]):
                f"#{t['purchase_transaction_id']} · {t['symbol']} · zakup {t['purchase_date']} · wykup {t['maturity_date']}"
                for t in tranches}
            selected_tranche_id = st.selectbox("Wybierz transzę do edycji", list(tranche_options),
                format_func=lambda x: tranche_options[x], key="edit_bond_tranche_id")
            selected_tranche = next(t for t in tranches if int(t["purchase_transaction_id"]) == selected_tranche_id)
            edit_type = st.selectbox("Rodzaj oprocentowania transzy", ["fixed", "inflation"],
                index=0 if selected_tranche["bond_type"] == "fixed" else 1,
                format_func=lambda x: "Stałe" if x == "fixed" else "Indeksowane inflacją", key=f"edit_bond_tranche_type_{selected_tranche_id}")
            with st.form("edit_bond_tranche"):
                e1, e2, e3 = st.columns(3)
                if edit_type == "fixed":
                    edit_rate = e1.number_input("Oprocentowanie roczne (%)", min_value=0.0,
                        value=float(selected_tranche["annual_rate_pct"] if selected_tranche["annual_rate_pct"] is not None else 5.0),
                        format="%.3f", key=f"edit_bond_rate_{selected_tranche_id}")
                    edit_margin = None
                    edit_inflation = None
                else:
                    edit_rate = None
                    edit_margin = e1.number_input("Marża ponad inflację (%)",
                        value=float(selected_tranche["inflation_margin_pct"] if selected_tranche["inflation_margin_pct"] is not None else 1.0),
                        format="%.3f", key=f"edit_bond_margin_{selected_tranche_id}")
                    inflation_value = selected_tranche.get("assumed_inflation_pct")
                    edit_inflation = e2.number_input("Przyjęta stopa inflacji (%)", min_value=-5.0, max_value=100.0,
                        value=float(inflation_value if inflation_value is not None else 4.0), format="%.2f", key=f"edit_bond_inflation_{selected_tranche_id}")
                edit_nominal = e2.number_input("Nominał jednej obligacji", min_value=0.01,
                    value=float(selected_tranche["nominal_value"]), format="%.2f", key=f"edit_bond_nominal_{selected_tranche_id}")
                try:
                    maturity_edit_default = date.fromisoformat(selected_tranche["maturity_date"]) if selected_tranche["maturity_date"] else date.fromisoformat(selected_tranche["purchase_date"]) + timedelta(days=365)
                except (TypeError, ValueError):
                    maturity_edit_default = date.fromisoformat(selected_tranche["purchase_date"]) + timedelta(days=365)
                edit_maturity = e3.date_input("Termin wykupu", value=maturity_edit_default, key=f"edit_bond_maturity_{selected_tranche_id}")
                edit_compounding = False
                st.caption('Wycena transzy jest szacunkowa: naliczamy odsetki liniowo, bez kapitalizacji.')
                save_tranche = st.form_submit_button("Zapisz warunki i przelicz historię", type="primary")
            if save_tranche:
                try:
                    backup_db()
                    conn.execute("SAVEPOINT bond_tranche_update")
                    try:
                        upsert_bond_tranche(conn, selected_tranche_id, {
                            "bond_type": edit_type, "annual_rate_pct": edit_rate,
                            "inflation_margin_pct": edit_margin, "assumed_inflation_pct": edit_inflation,
                            "nominal_value": edit_nominal, "maturity_date": edit_maturity.isoformat(),
                            "compounding": edit_compounding})
                        if edit_type == "inflation":
                            conn.execute("INSERT INTO settings(key,value) VALUES('bond_default_inflation_pct',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                                (str(edit_inflation),))
                        rebuild_snapshots(conn, selected_tranche["purchase_date"], commit=False)
                        conn.execute("RELEASE SAVEPOINT bond_tranche_update")
                        conn.commit()
                    except Exception:
                        conn.execute("ROLLBACK TO SAVEPOINT bond_tranche_update")
                        conn.execute("RELEASE SAVEPOINT bond_tranche_update")
                        conn.rollback()
                        raise
                    st.success("Zapisano warunki transzy. Historia od daty jej zakupu została przeliczona; ręczne korekty migawek pozostały zachowane.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Nie udało się zapisać warunków transzy: {exc}")
        else:
            st.info("Nie ma jeszcze zarejestrowanych zakupów obligacji.")
        st.caption("Wycena jest uproszczonym modelem szacunkowym, a nie odtworzeniem rzeczywistych historycznych stóp inflacji ani wszystkich szczegółowych zasad emisji. Założona inflacja obowiązuje dla całego okresu danej transzy i może być zmieniona w dowolnym momencie.")
        st.divider()
        st.subheader("Kopie zapasowe")
        backups = sorted((DB_PATH.parent / "backups").glob("*.sqlite3"), key=lambda p: p.stat().st_mtime, reverse=True) if (DB_PATH.parent / "backups").exists() else []
        c1,c2 = st.columns(2)
        if c1.button("Utwórz kopię teraz"):
            path = backup_db(); st.success(f"Kopia utworzona: {path.name if path else 'brak'}"); st.rerun()
        c2.download_button("Pobierz bieżącą bazę", data=database_bytes(conn), file_name="portfolio.sqlite3", mime="application/x-sqlite3")
        if backups:
            st.write("Ostatnie kopie:")
            st.dataframe(pd.DataFrame([{"Plik": p.name, "Rozmiar KB": round(p.stat().st_size/1024,1)} for p in backups[:30]]), hide_index=True, use_container_width=True)
        st.subheader("Eksport danych")
        tx_csv = pd.read_sql_query("""SELECT t.*,a.name AS account_name,a2.name AS to_account_name,
            x.symbol AS asset_symbol,x.name AS asset_name,x2.symbol AS to_asset_symbol,x2.name AS to_asset_name,
            bt.bond_type AS tranche_bond_type,bt.nominal_value AS tranche_nominal_value,
            bt.maturity_date AS tranche_maturity_date,bt.annual_rate_pct AS tranche_annual_rate_pct,
            bt.inflation_margin_pct AS tranche_inflation_margin_pct,
            bt.assumed_inflation_pct AS tranche_assumed_inflation_pct,bt.compounding AS tranche_compounding
            FROM transactions t LEFT JOIN accounts a ON a.id=t.account_id
            LEFT JOIN accounts a2 ON a2.id=t.to_account_id LEFT JOIN assets x ON x.id=t.asset_id
            LEFT JOIN assets x2 ON x2.id=t.to_asset_id
            LEFT JOIN bond_tranches bt ON bt.purchase_transaction_id=t.id
            ORDER BY t.occurred_at,t.id""", conn).to_csv(index=False).encode("utf-8-sig")
        asset_csv = pd.read_sql_query("SELECT * FROM assets", conn).to_csv(index=False).encode("utf-8-sig")
        cash_csv = pd.read_sql_query("SELECT * FROM price_history ORDER BY price_date", conn).to_csv(index=False).encode("utf-8-sig")
        x1,x2,x3 = st.columns(3)
        x1.download_button("Eksport transakcji CSV", tx_csv, "transakcje.csv", "text/csv")
        x2.download_button("Eksport aktywów CSV", asset_csv, "aktywa.csv", "text/csv")
        x3.download_button("Eksport wycen CSV", cash_csv, "wyceny.csv", "text/csv")
        st.subheader("Przywracanie kopii zapasowej")
        restore_upload = st.file_uploader("Wybierz plik bazy SQLite", type=["sqlite3", "db"], key="restore_db_upload")
        restore_confirm = st.checkbox("Rozumiem, że przywrócenie zastąpi bieżące dane. Przed operacją zostanie wykonana kopia obecnej bazy.", key="restore_db_confirm")
        if backups:
            local_backup = st.selectbox("Lub wybierz kopię zapisaną lokalnie", backups, format_func=lambda p: f"{p.name} · {p.stat().st_size/1024:.1f} KB")
            if st.button("Przywróć kopię lokalną", disabled=not restore_confirm, key="restore_local_backup"):
                try:
                    backup_db()
                    restore_database_from_bytes(conn, local_backup.read_bytes())
                    init_db()
                    st.success("Przywrócono lokalną kopię. Przed operacją wykonano kopię obecnej bazy.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Nie udało się przywrócić kopii: {exc}")
        if st.button("Przywróć wybraną kopię", disabled=(restore_upload is None or not restore_confirm), type="secondary"):
            try:
                backup_db()
                restore_database_from_bytes(conn, restore_upload.getvalue())
                init_db()
                st.success("Kopia została przywrócona. Bieżąca baza została wcześniej zabezpieczona kopią.")
                st.rerun()
            except Exception as exc:
                st.error(f"Nie udało się przywrócić kopii: {exc}")
        st.subheader("Import operacji z CSV")
        csv_upload = st.file_uploader("Wybierz plik transakcje.csv wyeksportowany z aplikacji", type=["csv"], key="import_tx_upload")
        if csv_upload is not None:
            try:
                preview_df = pd.read_csv(csv_upload, encoding="utf-8-sig")
                st.caption(f"Podgląd importu: {len(preview_df)} wierszy. Najlepiej używać eksportu transakcji z Mój Portfel: nazwy rachunków i instrumentów pozwalają dopasować dane również w innej bazie. Rachunki i instrumenty muszą być wcześniej dodane.")
                st.dataframe(preview_df.head(10), hide_index=True, use_container_width=True)
                if st.button("Zaimportuj transakcje z tego pliku", key="confirm_csv_import"):
                    backup_db()
                    result = import_transactions_csv(conn, csv_upload.getvalue(), csv_upload.name)
                    st.success(result["message"])
                    st.rerun()
            except Exception as exc:
                st.error(f"Nie udało się odczytać / zaimportować CSV: {exc}")
        st.divider()
        st.subheader("Informacje o metodzie")
        st.markdown("""
- Walutą bazową jest PLN. Koszt kupna jest oparty na faktycznej kwocie rozliczenia i zapisanym kursie waluty gotówki, powiększonej o opłaty.
- Koszt pozycji jest liczony metodą średniej ważonej globalnie dla instrumentu. Sprzedaż zmniejsza koszt proporcjonalnie.
- Transfer między własnymi rachunkami nie jest sprzedażą i nie zmienia łącznego kosztu aktywa.
- Wymiana krypto jest jednym zdarzeniem bez sztucznego przepływu gotówki.
- XIRR i benchmark wykorzystują wyłącznie wpłaty i wypłaty zewnętrzne.
- Podatki nie są obliczane.
        """)

st.sidebar.divider()
st.sidebar.caption("Mój Portfel · wersja 0.8.0 · TESTY ONLINE — dane fikcyjne")
