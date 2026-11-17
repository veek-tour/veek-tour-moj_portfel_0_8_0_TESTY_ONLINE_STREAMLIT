from __future__ import annotations

"""Instrumenty wstępnie dodawane do nowej bazy Mój Portfel.

Wpisy są dodawane idempotentnie: istniejący instrument o tym samym
(symbol, kategoria) nie jest nadpisywany.
"""

DEFAULT_ASSETS = [
    {
        "symbol": "GOLD", "name": "Złoto", "category": "Metale szlachetne",
        "currency": "USD", "listing_exchange": "NBP złoto (referencja), COMEX (zapasowy)",
        "price_mode": "auto", "price_symbol": "GC=F", "unit": "oz",
        "notes": "Wycena referencyjna: NBP, PLN za gram czystego złota przeliczone na uncję trojańską. GC=F jest jedynie zapasowym przybliżeniem futures. Fizyczne monety mają własną korektę ceny zapisywaną przy transakcji.",
    },
    {
        "symbol": "SILVER", "name": "Srebro", "category": "Metale szlachetne",
        "currency": "USD", "listing_exchange": "Spot srebro (referencja), COMEX (zapasowy)",
        "price_mode": "auto", "price_symbol": "SI=F", "unit": "oz",
        "notes": "Wycena referencyjna: cena spot XAG/USD z Gold API przeliczona na PLN. SI=F jest wyraźnie oznaczonym zapasowym przybliżeniem futures dla historii. Fizyczne monety mają własną korektę ceny zapisywaną przy transakcji.",
    },
    {
        "symbol": "BIO", "name": "Bio Protocol (BIO)", "category": "Kryptowaluty",
        "currency": "USD", "listing_exchange": "Binance Spot",
        "price_mode": "auto", "price_symbol": "BINANCE:BIOUSDT", "unit": "szt.",
        "notes": "Źródło: para spot BIO/USDT na Binance. Kwotowanie USDT jest traktowane jak USD zgodnie z ustawieniem aplikacji.",
    },
    {
        "symbol": "BB", "name": "BounceBit (BB)", "category": "Kryptowaluty",
        "currency": "USD", "listing_exchange": "Binance Spot",
        "price_mode": "auto", "price_symbol": "BINANCE:BBUSDT", "unit": "szt.",
        "notes": "Źródło: para spot BB/USDT na Binance. Kwotowanie USDT jest traktowane jak USD. BounceBit przeprowadził migrację tokena 1:1 na BNB Smart Chain w 2026 r.; symbol rynkowy pozostaje BB.",
    },
    {
        "symbol": "SOL", "name": "Solana (SOL)", "category": "Kryptowaluty",
        "currency": "USD", "listing_exchange": "Binance Spot",
        "price_mode": "auto", "price_symbol": "BINANCE:SOLUSDT", "unit": "szt.",
        "notes": "Źródło: para spot SOL/USDT na Binance. Kwotowanie USDT jest traktowane jak USD.",
    },
    {
        "symbol": "BTC", "name": "Bitcoin (BTC)", "category": "Kryptowaluty",
        "currency": "USD", "listing_exchange": "Binance Spot",
        "price_mode": "auto", "price_symbol": "BINANCE:BTCUSDT", "unit": "szt.",
        "notes": "Źródło: para spot BTC/USDT na Binance. Kwotowanie USDT jest traktowane jak USD.",
    },
    {
        "symbol": "XRP", "name": "XRP (Ripple)", "category": "Kryptowaluty",
        "currency": "USD", "listing_exchange": "Binance Spot",
        "price_mode": "auto", "price_symbol": "BINANCE:XRPUSDT", "unit": "szt.",
        "notes": "Źródło: para spot XRP/USDT na Binance. Kwotowanie USDT jest traktowane jak USD.",
    },
    {
        "symbol": "ETH", "name": "Ethereum (ETH)", "category": "Kryptowaluty",
        "currency": "USD", "listing_exchange": "Binance Spot",
        "price_mode": "auto", "price_symbol": "BINANCE:ETHUSDT", "unit": "szt.",
        "notes": "Źródło: para spot ETH/USDT na Binance. Kwotowanie USDT jest traktowane jak USD.",
    },
    {
        "symbol": "BNB", "name": "BNB", "category": "Kryptowaluty",
        "currency": "USD", "listing_exchange": "Binance Spot",
        "price_mode": "auto", "price_symbol": "BINANCE:BNBUSDT", "unit": "szt.",
        "notes": "Źródło: para spot BNB/USDT na Binance. Kwotowanie USDT jest traktowane jak USD.",
    },
    {
        "symbol": "OP", "name": "Optimism (OP)", "category": "Kryptowaluty",
        "currency": "USD", "listing_exchange": "Binance Spot",
        "price_mode": "auto", "price_symbol": "BINANCE:OPUSDT", "unit": "szt.",
        "notes": "Źródło: para spot OP/USDT na Binance. Kwotowanie USDT jest traktowane jak USD.",
    },
    {
        "symbol": "EIMI", "name": "iShares Core MSCI Emerging Markets IMI UCITS ETF USD (Acc)", "category": "ETF",
        "currency": "USD", "listing_exchange": "LSE (USD)",
        "price_mode": "auto", "price_symbol": "EIMI.L", "unit": "szt.",
        "notes": "ISIN: IE00BKM4GZ66. Wariant akumulacyjny (Acc). Notowanie EIMI na LSE w USD (XTB: EIMI.UK).",
    },
    {
        "symbol": "EUNL", "name": "iShares Core MSCI World UCITS ETF USD (Acc)", "category": "ETF",
        "currency": "EUR", "listing_exchange": "XETRA",
        "price_mode": "auto", "price_symbol": "EUNL.DE", "unit": "szt.",
        "notes": "ISIN: IE00B4L5Y983. Wariant akumulacyjny (Acc). Notowanie EUNL na XETRA w EUR.",
    },
    {
        "symbol": "LYP6", "name": "Amundi Core STOXX Europe 600 UCITS ETF Acc", "category": "ETF",
        "currency": "EUR", "listing_exchange": "XETRA",
        "price_mode": "auto", "price_symbol": "LYP6.DE", "unit": "szt.",
        "notes": "ISIN: LU0908500753. Wariant akumulacyjny (Acc / Capitalisation). Notowanie LYP6 na XETRA w EUR.",
    },
    {
        "symbol": "IWQU", "name": "iShares Edge MSCI World Quality Factor UCITS ETF USD (Acc)", "category": "ETF",
        "currency": "USD", "listing_exchange": "LSE (USD)",
        "price_mode": "auto", "price_symbol": "IWQU.L", "unit": "szt.",
        "notes": "ISIN: IE00BP3QZ601. Wariant akumulacyjny (Acc). Notowanie IWQU na LSE w USD (XTB: IWQU.UK).",
    },
    {
        "symbol": "SXRV", "name": "iShares Nasdaq 100 UCITS ETF USD (Acc)", "category": "ETF",
        "currency": "EUR", "listing_exchange": "XETRA",
        "price_mode": "auto", "price_symbol": "SXRV.DE", "unit": "szt.",
        "notes": "ISIN: IE00B53SZB19. Wariant akumulacyjny (Acc). Notowanie SXRV na XETRA w EUR.",
    },
    {
        "symbol": "OBLIGACJE", "name": "Polskie obligacje skarbowe", "category": "Obligacje",
        "currency": "PLN", "listing_exchange": "Rynek detalicznych obligacji skarbowych (MF)",
        "price_mode": "model", "price_symbol": "", "unit": "szt.",
        "bond_type": "fixed", "annual_rate_pct": None, "inflation_margin_pct": None,
        "nominal_value": 100.0, "bond_compounding": 0,
        "notes": "Instrument zbiorczy. Każdy zakup jest osobną transzą z własną datą, nominałem, oprocentowaniem, marżą, założeniem inflacji i terminem wykupu. Nie jest pobierana cena rynkowa.",
    },
]


def seed_default_assets(conn) -> int:
    """Add missing predefined assets without replacing user data or existing assets."""
    fields = (
        "symbol", "name", "category", "currency", "listing_exchange", "price_mode",
        "price_symbol", "unit", "bond_type", "annual_rate_pct", "inflation_margin_pct",
        "nominal_value", "maturity_date", "bond_compounding", "manual_price", "notes",
    )
    sql = f"INSERT OR IGNORE INTO assets ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})"
    before = conn.total_changes
    for asset in DEFAULT_ASSETS:
        values = [asset.get(field) for field in fields]
        if values[13] is None:
            values[13] = 1
        conn.execute(sql, values)
    return conn.total_changes - before
