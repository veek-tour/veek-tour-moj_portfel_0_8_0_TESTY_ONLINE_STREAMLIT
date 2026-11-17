from portfolio.db import connect, init_db, rows
from portfolio.default_assets import DEFAULT_ASSETS, seed_default_assets
from portfolio.market import fetch_binance_history
from datetime import date


def test_default_assets_seeded_once(tmp_path):
    path = tmp_path / "seed.sqlite3"
    init_db(path)
    with connect(path) as conn:
        seeded = rows(conn, "SELECT * FROM assets ORDER BY category, symbol")
        assert len(seeded) == 16
        assert len(DEFAULT_ASSETS) == 16
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM price_history").fetchone()[0] == 0
        before = conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
        assert seed_default_assets(conn) == 0
        assert conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0] == before


def test_default_assets_have_required_quote_metadata(tmp_path):
    path = tmp_path / "metadata.sqlite3"
    init_db(path)
    with connect(path) as conn:
        records = {r["symbol"]: dict(r) for r in rows(conn, "SELECT * FROM assets")}
        assert records["GOLD"]["unit"] == "oz" and records["SILVER"]["unit"] == "oz"
        assert records["GOLD"]["price_symbol"] == "GC=F"
        assert records["SILVER"]["price_symbol"] == "SI=F"
        assert records["BIO"]["price_symbol"] == "BINANCE:BIOUSDT"
        assert records["BB"]["price_symbol"] == "BINANCE:BBUSDT"
        assert records["BTC"]["currency"] == "USD"
        assert records["OBLIGACJE"]["category"] == "Obligacje"
        for symbol, isin in {"EIMI":"IE00BKM4GZ66", "EUNL":"IE00B4L5Y983",
                             "LYP6":"LU0908500753", "IWQU":"IE00BP3QZ601",
                             "SXRV":"IE00B53SZB19"}.items():
            assert isin in records[symbol]["notes"]
            assert "Acc" in records[symbol]["name"] or "akumulacyjny" in records[symbol]["notes"]


def test_binance_history_parses_daily_candles(monkeypatch):
    class Response:
        def raise_for_status(self):
            pass
        def json(self):
            return [[1735689600000, "1", "2", "0.5", "1.5", "100"]]

    class Session:
        def get(self, url, params, timeout):
            assert url.endswith("/api/v3/klines")
            assert params["symbol"] == "BIOUSDT"
            return Response()

    import portfolio.market as market
    monkeypatch.setattr(market.requests, "Session", Session)
    result = fetch_binance_history("BIOUSDT", date(2025, 1, 1), date(2025, 1, 1))
    assert result == [("2025-01-01", 1.5)]


def test_existing_database_is_backed_up_and_existing_definition_is_preserved(tmp_path):
    path = tmp_path / "existing.sqlite3"
    init_db(path)
    with connect(path) as conn:
        conn.execute("UPDATE assets SET name='Moja nazwa BTC' WHERE symbol='BTC' AND category='Kryptowaluty'")
        conn.execute("DELETE FROM assets WHERE symbol='BB' AND category='Kryptowaluty'")
        conn.commit()
    init_db(path)
    with connect(path) as conn:
        assert conn.execute("SELECT name FROM assets WHERE symbol='BTC'").fetchone()[0] == 'Moja nazwa BTC'
        assert conn.execute("SELECT COUNT(*) FROM assets WHERE symbol='BB' AND category='Kryptowaluty'").fetchone()[0] == 1
    backups = list((tmp_path / "backups").glob("portfolio_*.sqlite3"))
    assert len(backups) == 1


def test_sync_market_data_routes_binance_assets_and_stores_usd_prices(tmp_path, monkeypatch):
    path = tmp_path / "market.sqlite3"
    init_db(path)
    import portfolio.market as market
    with connect(path) as conn:
        monkeypatch.setattr(market, "fetch_binance_history", lambda symbol, start, end: [("2026-11-05", 10.0)])
        monkeypatch.setattr(market, "fetch_binance_latest", lambda symbol: ("2026-11-06", 11.0))
        monkeypatch.setattr(market, "fetch_yahoo_history", lambda *args, **kwargs: [])
        monkeypatch.setattr(market, "fetch_nbp_rates", lambda *args, **kwargs: [])
        result = market.sync_market_data(conn, days_back=14, force=True)
        assert result["crypto_intraday_saved"] == 8
        for symbol in ("BTC", "BIO", "BB", "OP"):
            records = rows(conn, "SELECT p.price,p.currency,p.source FROM price_history p JOIN assets a ON a.id=p.asset_id WHERE a.symbol=? ORDER BY p.price_date", (symbol,))
            assert len(records) == 2
            assert {r["currency"] for r in records} == {"USD"}
            assert {r["source"] for r in records} == {"binance"}
