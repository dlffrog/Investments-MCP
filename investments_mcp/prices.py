"""
prices.py — OpenBB-based market data fetching.

Primary provider: FMP (Financial Modeling Prep).
Fallback provider: yfinance.

This module is shared between the MCP server and Scripts/update_prices.py in
the vault. Both import from here so there is one data source throughout.

Exported functions:
    get_equity_quote(symbol, provider) -> dict
    get_fx_rate(currency, provider) -> float          # units per GBP
    get_historical_ohlcv(symbol, start, end, provider) -> list[dict]
    get_all_fx_rates(provider) -> dict[str, float]    # all supported currencies
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

log = logging.getLogger(__name__)

# Currencies supported for GBP cross-rate fetching.
# Key: ISO code used in position files. Value: FX pair symbol (GBP base).
SUPPORTED_CURRENCIES: dict[str, str] = {
    "USD": "GBPUSD",
    "CAD": "GBPCAD",
    "EUR": "GBPEUR",
    "AUD": "GBPAUD",
    "HKD": "GBPHKD",
    "SGD": "GBPSGD",
    "PLN": "GBPPLN",
    "ILS": "GBPILS",
    "NOK": "GBPNOK",
    "MXN": "GBPMXN",
}

_obb_initialised = False


def _init_openbb(fmp_api_key: str | None = None) -> None:
    """Initialise OpenBB credentials once per process."""
    global _obb_initialised
    if _obb_initialised:
        return
    try:
        from openbb import obb  # noqa: F401 — triggers provider registration

        if fmp_api_key:
            try:
                obb.user.credentials.fmp_api_key = fmp_api_key
            except Exception:
                pass  # Older OpenBB versions may not have this attribute
    except ImportError:
        log.warning("openbb package not installed; price fetching will fail")
    _obb_initialised = True


def _get_obb():
    """Return the openbb module, raising clearly if not installed."""
    try:
        from openbb import obb
        return obb
    except ImportError as exc:
        raise RuntimeError(
            "openbb is not installed. Run: pip install openbb openbb-fmp openbb-yfinance"
        ) from exc


# ---------------------------------------------------------------------------
# Price normalisation helpers
# ---------------------------------------------------------------------------

def _normalise_price(price: float | None, currency: str) -> tuple[float, str]:
    """Convert GBX/GBp prices to GBP. Returns (price, normalised_currency)."""
    if price is None:
        raise ValueError("No price returned from provider")
    if currency in ("GBX", "GBp", "GBx"):
        return price / 100.0, "GBP"
    return float(price), currency


def _extract_quote_fields(result: Any, provider: str) -> tuple[float, str]:
    """
    Extract (price, currency) from an OpenBB quote result.
    Handles field-name differences between providers.
    """
    if not result.results:
        raise ValueError(f"Empty results from {provider}")
    q = result.results[0]

    # Field names vary by provider
    price = (
        getattr(q, "price", None)          # FMP
        or getattr(q, "last_price", None)   # yfinance
        or getattr(q, "close", None)        # some providers
    )
    currency = getattr(q, "currency", None) or "USD"

    if price is None or float(price) <= 0:
        raise ValueError(f"Invalid price ({price}) from {provider}")

    return _normalise_price(float(price), currency)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_equity_quote(
    symbol: str,
    provider: str = "fmp",
    fmp_api_key: str | None = None,
) -> dict:
    """
    Fetch a live equity quote for `symbol`.

    Returns a dict with keys:
        price (float), currency (str), change_pct (float | None),
        volume (int | None), provider (str), symbol (str)

    Raises ValueError if no valid price is returned.
    Falls back to yfinance if FMP fails and provider == "fmp".
    """
    _init_openbb(fmp_api_key)
    obb = _get_obb()

    providers_to_try = [provider]
    if provider == "fmp":
        providers_to_try.append("yfinance")  # automatic fallback

    last_exc: Exception | None = None
    for prov in providers_to_try:
        try:
            result = obb.equity.price.quote(symbol, provider=prov)
            price, currency = _extract_quote_fields(result, prov)

            q = result.results[0]
            change_pct = (
                getattr(q, "changes_percentage", None)       # FMP
                or getattr(q, "regular_market_change_percent", None)  # yfinance
                or getattr(q, "change_percent", None)
            )

            return {
                "price": price,
                "currency": currency,
                "change_pct": round(float(change_pct), 4) if change_pct is not None else None,
                "volume": getattr(q, "volume", None),
                "provider": prov,
                "symbol": symbol,
            }
        except Exception as exc:
            log.warning("get_equity_quote %s via %s failed: %s", symbol, prov, exc)
            last_exc = exc

    raise ValueError(f"Could not fetch quote for {symbol}: {last_exc}") from last_exc


def get_fx_rate(
    currency: str,
    provider: str = "fmp",
    fallback_rates: dict[str, float] | None = None,
    fmp_api_key: str | None = None,
) -> float:
    """
    Return the current exchange rate for `currency` expressed as units-per-GBP.
    e.g. get_fx_rate("USD") → 1.28 means £1 = $1.28

    Falls back to yfinance then to fallback_rates dict on failure.
    """
    if currency == "GBP":
        return 1.0

    pair = SUPPORTED_CURRENCIES.get(currency.upper())
    if not pair:
        log.warning("Unsupported currency %s, returning 1.0", currency)
        return fallback_rates.get(currency, 1.0) if fallback_rates else 1.0

    _init_openbb(fmp_api_key)
    obb = _get_obb()

    providers_to_try = [provider, "yfinance"] if provider == "fmp" else [provider]

    for prov in providers_to_try:
        try:
            # Use recent historical data (last 3 days) to get latest rate
            start = (date.today() - timedelta(days=5)).isoformat()
            result = obb.currency.price.historical(pair, start_date=start, provider=prov)
            if result.results:
                q = result.results[-1]  # most recent
                rate = (
                    getattr(q, "close", None)
                    or getattr(q, "last_price", None)
                    or getattr(q, "rate", None)
                )
                if rate and float(rate) > 0:
                    return float(rate)
        except Exception as exc:
            log.warning("get_fx_rate %s via %s failed: %s", pair, prov, exc)

    # Last resort: config fallback
    if fallback_rates and currency in fallback_rates:
        log.warning("Using cached FX rate for %s: %s", currency, fallback_rates[currency])
        return fallback_rates[currency]

    log.warning("No FX rate for %s, defaulting to 1.0", currency)
    return 1.0


def get_all_fx_rates(
    provider: str = "fmp",
    fallback_rates: dict[str, float] | None = None,
    fmp_api_key: str | None = None,
) -> dict[str, float]:
    """
    Fetch GBP cross-rates for all supported currencies in one pass.
    Returns dict {currency_code: units_per_gbp}, always includes GBP: 1.0.
    """
    rates: dict[str, float] = {"GBP": 1.0}
    for currency in SUPPORTED_CURRENCIES:
        rates[currency] = get_fx_rate(
            currency,
            provider=provider,
            fallback_rates=fallback_rates,
            fmp_api_key=fmp_api_key,
        )
    return rates


def get_historical_ohlcv(
    symbol: str,
    start_date: str,
    end_date: str | None = None,
    provider: str = "fmp",
    fmp_api_key: str | None = None,
) -> list[dict]:
    """
    Fetch daily OHLCV history for `symbol`.

    Returns a list of dicts with keys: date, open, high, low, close, volume.
    Most recent date last.
    """
    _init_openbb(fmp_api_key)
    obb = _get_obb()

    providers_to_try = [provider, "yfinance"] if provider == "fmp" else [provider]
    last_exc: Exception | None = None

    for prov in providers_to_try:
        try:
            kwargs: dict[str, Any] = {"start_date": start_date, "provider": prov}
            if end_date:
                kwargs["end_date"] = end_date
            result = obb.equity.price.historical(symbol, **kwargs)
            return [
                {
                    "date": str(getattr(r, "date", ""))[:10],
                    "open": getattr(r, "open", None),
                    "high": getattr(r, "high", None),
                    "low": getattr(r, "low", None),
                    "close": getattr(r, "close", None),
                    "volume": getattr(r, "volume", None),
                }
                for r in result.results
            ]
        except Exception as exc:
            log.warning("get_historical_ohlcv %s via %s failed: %s", symbol, prov, exc)
            last_exc = exc

    raise ValueError(f"Could not fetch history for {symbol}: {last_exc}") from last_exc
