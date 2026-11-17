import pytest
from portfolio.db import init_db, connect, add_account, add_asset

@pytest.fixture
def db(tmp_path):
    path = tmp_path / "test.sqlite3"
    init_db(path)
    conn = connect(path)
    # Fixtures use hand-built test assets; keep application defaults out of unit tests.
    conn.execute("DELETE FROM assets")
    conn.commit()
    a1 = add_account(conn, "Broker", "broker")
    a2 = add_account(conn, "Staking", "staking")
    asset = add_asset(conn, {"symbol":"ABC", "name":"Test Asset", "category":"ETF", "currency":"USD",
                             "listing_exchange":"NYSE", "price_mode":"manual", "price_symbol":"",
                             "unit":"szt.", "bond_type":None, "annual_rate_pct":None, "inflation_margin_pct":None,
                             "nominal_value":100, "maturity_date":None, "manual_price":None, "notes":""})
    asset2 = add_asset(conn, {"symbol":"XYZ", "name":"Second Asset", "category":"Kryptowaluty", "currency":"USD",
                             "listing_exchange":"", "price_mode":"manual", "price_symbol":"",
                             "unit":"szt.", "bond_type":None, "annual_rate_pct":None, "inflation_margin_pct":None,
                             "nominal_value":100, "maturity_date":None, "manual_price":None, "notes":""})
    yield conn, a1, a2, asset, asset2
    conn.close()
