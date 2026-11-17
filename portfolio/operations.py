"""Shared, testable rules for the simplified transaction editor.

PLN is the reporting currency. An instrument's quote currency and the currency
actually paid can differ; neither is silently replaced by the other.
"""
from __future__ import annotations

from math import isfinite
from typing import Any

SUPPORTED_CURRENCIES = ("PLN", "EUR", "USD")
METAL_CATEGORY = "Metale szlachetne"


def positive_number(value: Any, label: str, *, zero_allowed: bool = False) -> float:
    try:
        amount = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"Nieprawidłowa wartość: {label}.") from exc
    if not isfinite(amount) or (amount < 0 if zero_allowed else amount <= 0):
        raise ValueError(f"{label}: podaj {'liczbę nieujemną' if zero_allowed else 'wartość większą od zera'}.")
    return amount


def check_currency(value: str) -> str:
    code = str(value or '').upper().strip()
    if code not in SUPPORTED_CURRENCIES:
        raise ValueError("Obsługiwane waluty to PLN, EUR i USD.")
    return code


def quote_total(quantity: float, unit_price: float) -> float:
    return positive_number(quantity, "Ilość") * positive_number(unit_price, "Cena jednostkowa")


def metal_trade_values(quantity: float, reference_pln_per_oz: float,
                       adjustment_pln_per_oz: float, *, additional_fee: float = 0.0) -> dict[str, float]:
    """Signed adjustment is per troy ounce, for purchases and sales alike.

    An adjustment already includes the dealer's premium/tax; an additional fee
    should represent a *different* cost to prevent double-counting.
    """
    qty = positive_number(quantity, "Ilość uncji")
    spot = positive_number(reference_pln_per_oz, "Cena rynkowa metalu")
    try:
        adjustment = float(adjustment_pln_per_oz)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Korekta ceny musi być liczbą.") from exc
    if not isfinite(adjustment):
        raise ValueError("Nieprawidłowa korekta ceny.")
    if spot + adjustment <= 0:
        raise ValueError("Cena monety po korekcie musi być dodatnia.")
    fee = positive_number(additional_fee, "Dodatkowa opłata", zero_allowed=True)
    return {"quantity": qty, "spot_pln_per_oz": spot, "adjustment_pln_per_oz": adjustment,
            "actual_pln_per_oz": spot + adjustment, "cash_amount_pln": qty * (spot + adjustment),
            "fee_pln": fee}


def settlement_amount(quantity: float, price: float, price_currency: str,
                      settlement_currency: str, *, real_amount: float | None = None,
                      reference_fx_price: float | None = None,
                      reference_fx_settlement: float | None = None) -> float:
    """Prefer the broker's actual settlement; otherwise convert via PLN.

    The calculated cross-currency amount is explicitly an NBP *estimate*, not
    a statement of how much the broker charged.
    """
    raw = quote_total(quantity, price)
    price_ccy, cash_ccy = check_currency(price_currency), check_currency(settlement_currency)
    if real_amount is not None:
        return positive_number(real_amount, "Rzeczywista kwota rozliczenia")
    if price_ccy == cash_ccy:
        return raw
    rate_from = positive_number(reference_fx_price, "Kurs waluty ceny")
    rate_to = positive_number(reference_fx_settlement, "Kurs waluty rozliczenia")
    return raw * rate_from / rate_to


def fx_metadata(currency: str, rate: float, *, rate_date: str | None, source: str) -> dict[str, Any]:
    currency = check_currency(currency)
    rate = positive_number(rate, "Kurs do PLN")
    if currency == "PLN" and abs(rate - 1.0) > 1e-9:
        raise ValueError("Kurs PLN do PLN musi wynosić 1.")
    return {"currency": currency, "rate": rate, "rate_date": rate_date, "source": source}
