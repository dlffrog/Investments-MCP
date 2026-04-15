"""
prices.py — market data fetching for the MCP server and vault scripts.

Provider chain:
  1. EODHD REST (primary — uses investments_mcp.eodhd_client.EODHDClient)
  2. yfinance (narrow fallback for exchanges EODHD does not cover, e.g. SGX)

Public surface is kept stable so Scripts/update_prices.py, server.py, and
trade_ops.py don't need to change their call sites.
"""

from __future__ import annotations

import logging
from typing import Any

from .eodhd_client import EODHDClient, EODHDError
from .exchanges import (
    VAULT_TO_EODHD,
    VAULT_TO_YAHOO,
    build_eodhd_symbol,
    build_yahoo_symbol,
    has_eodhd_coverage,
)

log = logging.getLogger(__name__)


# Currencies supported for GBP cross-rate fetching.
# Value: EODHD forex symbol (GBP base, quote currency).
SUPPORTED_CURRENCIES: dict[str, str] = {
    "USD": "GBPUSD.FOREX",
    "CAD": "GBPCAD.FOREX",
    "EUR": "GBPEUR.FOREX",
    "AUD": "GBPAUD.FOREX",
    "HKD": "GBPHKD.FOREX",
    "SGD": "GBPSGD.FOREX",
    "PLN": "GBPPLN.FOREX",
    "ILS": "GBPILS.FOREX",
    "NOK": "GBPNOK.FOREX",
    "MXN": "GBPMXN.FOREX",
}


# ---------------------------------------------------------------------------
# Legacy symbol-building shim
# ---------------------------------------------------------------------------

def build_symbol(ticker: str, exchange: str) -> str | None:
    """
    Legacy alias: return an EODHD symbol if the exchange is covered,
    otherwise a yfinance fallback symbol. Returns None when exchange is
    'skip'/empty/unknown.
    """
    eodhd = build_eodhd_symbol(ticker, exchange)
    if eodhd is not None:
        return eodhd
    return build_yahoo_symbol(ticker, exchange)


# ---------------------------------------------------------------------------
# Price normalisation
# ---------------------------------------------------------------------------

def _coerce_na(value: Any) -> Any:
    """EODHD uses the string 'NA' for missing real-time fields; treat as None."""
    if value in (None, "NA", "N/A", ""):
        return None
    return value


def _normalise_price(price: float | None, currency: str) -> tuple[float, str]:
    """Convert GBX/GBp prices to GBP. Returns (price, normalised_currency)."""
    if price is None:
        raise ValueError("No price returned from provider")
    if currency in ("GBX", "GBp", "GBx"):
        return float(price) / 100.0, "GBP"
    return float(price), currency


# ---------------------------------------------------------------------------
# Client factory & caches
# ---------------------------------------------------------------------------

_client_cache: dict[str, EODHDClient] = {}


def _get_client(api_key: str | None) -> EODHDClient:
    key = api_key or ""
    if key not in _client_cache:
        _client_cache[key] = EODHDClient(api_key=api_key or None)
    return _client_cache[key]


# ---------------------------------------------------------------------------
# yfinance fallback (lazy import — only used for SGX-like gaps)
# ---------------------------------------------------------------------------

def _yf_quote(symbol: str) -> dict[str, Any]:
    """Return {'price','currency','change_pct','volume'} for a yfinance symbol."""
    import yfinance as yf  # Lazy import; only pulled in when needed.

    t = yf.Ticker(symbol)
    info = t.fast_info if hasattr(t, "fast_info") else {}
    price = None
    currency = "USD"
    try:
        price = float(info["last_price"]) if info.get("last_price") else None
        currency = info.get("currency") or "USD"
    except Exception:
        pass
    if price is None:
        hist = t.history(period="5d")
        if hist.empty:
            raise ValueError(f"yfinance returned no data for {symbol}")
        price = float(hist["Close"].iloc[-1])
        currency = getattr(t, "info", {}).get("currency", currency)
    prev_close = info.get("previous_close") if hasattr(info, "get") else None
    change_pct = None
    if prev_close and prev_close > 0:
        change_pct = (price / float(prev_close) - 1) * 100
    return {
        "price": price,
        "currency": currency,
        "change_pct": change_pct,
        "volume": info.get("last_volume") if hasattr(info, "get") else None,
    }


def _yf_historical(symbol: str, start: str, end: str | None) -> list[dict]:
    import yfinance as yf

    t = yf.Ticker(symbol)
    hist = t.history(start=start, end=end) if end else t.history(start=start)
    if hist.empty:
        return []
    rows = []
    for ts, row in hist.iterrows():
        rows.append({
            "date": ts.strftime("%Y-%m-%d"),
            "open": float(row["Open"]),
            "high": float(row["High"]),
            "low": float(row["Low"]),
            "close": float(row["Close"]),
            "volume": int(row["Volume"]) if row["Volume"] else None,
        })
    return rows


# ---------------------------------------------------------------------------
# Public API — equity quotes
# ---------------------------------------------------------------------------

def get_equity_quote(
    symbol: str,
    api_key: str | None = None,
    currency_hint: str | None = None,
    **_ignored: Any,
) -> dict:
    """
    Fetch a live equity quote.

    `symbol` is expected in `{TICKER}.{EXCHANGE}` form. If it looks like a
    Yahoo-style symbol (no dot or an unknown suffix), the EODHD client will
    interpret a dot-less symbol as US; a yfinance fallback kicks in only
    when EODHD returns an error for an already-routed symbol.

    `currency_hint`: when provided, overrides the exchange-based currency
    inference. Use this for instruments whose currency differs from the
    exchange default (e.g. EUR-denominated ETFs listed on the LSE).

    Returns: {price, currency, change_pct, volume, provider, symbol}
    """
    client = _get_client(api_key)

    # Heuristic: if symbol has a Yahoo-style suffix not in EODHD's exchanges,
    # skip straight to yfinance. We detect this by inspecting the trailing
    # component; callers using build_symbol() already route correctly.
    yahoo_suffixes_unsupported = {".SI", ".MI"}
    if any(symbol.upper().endswith(sfx) for sfx in yahoo_suffixes_unsupported):
        q = _yf_quote(symbol)
        price, currency = _normalise_price(q["price"], q["currency"])
        return {
            "price": price,
            "currency": currency,
            "change_pct": round(q["change_pct"], 4) if q["change_pct"] is not None else None,
            "volume": q["volume"],
            "provider": "yfinance",
            "symbol": symbol,
        }

    try:
        data = client.real_time_quote(symbol)
        price = _coerce_na(data.get("close")) or _coerce_na(data.get("previousClose"))
        if price is None:
            raise ValueError(f"EODHD returned no price for {symbol} (likely unsupported ticker)")
        currency = currency_hint or _infer_currency_from_symbol(symbol)
        price, currency = _normalise_price(float(price), currency)
        change_pct = _coerce_na(data.get("change_p"))
        return {
            "price": price,
            "currency": currency,
            "change_pct": round(float(change_pct), 4) if change_pct is not None else None,
            "volume": _coerce_na(data.get("volume")),
            "provider": "eodhd",
            "symbol": symbol,
        }
    except (EODHDError, ValueError, TypeError) as exc:
        log.warning("EODHD quote for %s failed: %s", symbol, exc)
        # Last-ditch yfinance attempt — only helps when `symbol` happens to
        # have a yahoo-parseable form.
        try:
            q = _yf_quote(symbol)
            price, currency = _normalise_price(q["price"], q["currency"])
            return {
                "price": price,
                "currency": currency,
                "change_pct": round(q["change_pct"], 4) if q["change_pct"] is not None else None,
                "volume": q["volume"],
                "provider": "yfinance",
                "symbol": symbol,
            }
        except Exception as yf_exc:
            raise ValueError(f"Could not fetch quote for {symbol}: {exc}; yfinance fallback: {yf_exc}") from exc


def _infer_currency_from_symbol(symbol: str) -> str:
    """
    EODHD's real-time payload does not always include a currency field.
    Infer it from the exchange suffix so the caller gets a sane value.
    """
    if "." not in symbol:
        return "USD"
    suffix = symbol.rsplit(".", 1)[1].upper()
    suffix_to_ccy = {
        "US": "USD",
        "LSE": "GBX",  # LSE prices are GBX — _normalise_price converts to GBP.
        "TO": "CAD", "V": "CAD",
        "XETRA": "EUR", "F": "EUR", "PA": "EUR", "AS": "EUR", "LS": "EUR",
        "VI": "EUR", "AT": "EUR", "BR": "EUR", "HE": "EUR", "IR": "EUR",
        "MC": "EUR", "LU": "EUR",
        "SW": "CHF",
        "HK": "HKD",
        "AU": "AUD",
        "WAR": "PLN",
        "TA": "ILS",
        "OL": "NOK",
        "ST": "SEK",
        "CO": "DKK",
        "FOREX": "",
    }
    return suffix_to_ccy.get(suffix, "USD")


# ---------------------------------------------------------------------------
# Public API — historical
# ---------------------------------------------------------------------------

def get_historical_ohlcv(
    symbol: str,
    start_date: str,
    end_date: str | None = None,
    api_key: str | None = None,
    **_ignored: Any,
) -> list[dict]:
    """
    Return daily OHLCV rows in `[{date, open, high, low, close, volume}, ...]`.
    Most recent row last. Falls back to yfinance for exchanges EODHD lacks.
    """
    if any(symbol.upper().endswith(sfx) for sfx in (".SI", ".MI")):
        return _yf_historical(symbol, start_date, end_date)

    client = _get_client(api_key)
    try:
        rows = client.historical_eod(symbol, start=start_date, end=end_date)
        return [
            {
                "date": str(r.get("date", ""))[:10],
                "open": r.get("open"),
                "high": r.get("high"),
                "low": r.get("low"),
                "close": r.get("close"),
                "volume": r.get("volume"),
            }
            for r in rows
        ]
    except EODHDError as exc:
        log.warning("EODHD historical for %s failed: %s", symbol, exc)
        try:
            return _yf_historical(symbol, start_date, end_date)
        except Exception as yf_exc:
            raise ValueError(f"Could not fetch history for {symbol}: {exc}; yfinance: {yf_exc}") from exc


# ---------------------------------------------------------------------------
# Public API — FX rates
# ---------------------------------------------------------------------------

def get_fx_rate(
    currency: str,
    api_key: str | None = None,
    fallback_rates: dict[str, float] | None = None,
    **_ignored: Any,
) -> float:
    """Return units-per-GBP for `currency`. e.g. get_fx_rate('USD') → ~1.28."""
    if currency == "GBP":
        return 1.0

    pair = SUPPORTED_CURRENCIES.get(currency.upper())
    if not pair:
        log.warning("Unsupported currency %s; returning 1.0", currency)
        return fallback_rates.get(currency, 1.0) if fallback_rates else 1.0

    client = _get_client(api_key)
    try:
        data = client.real_time_quote(pair)
        rate = data.get("close") or data.get("previousClose")
        if rate and float(rate) > 0:
            return float(rate)
    except EODHDError as exc:
        log.warning("EODHD FX for %s failed: %s", pair, exc)

    if fallback_rates and currency in fallback_rates:
        log.warning("Using cached FX rate for %s: %s", currency, fallback_rates[currency])
        return float(fallback_rates[currency])
    log.warning("No FX rate for %s, defaulting to 1.0", currency)
    return 1.0


def get_all_fx_rates(
    api_key: str | None = None,
    fallback_rates: dict[str, float] | None = None,
    **_ignored: Any,
) -> dict[str, float]:
    """
    Fetch GBP cross-rates for every supported currency in a single HTTP call
    via EODHD's bulk real-time endpoint. Always includes GBP: 1.0.
    """
    rates: dict[str, float] = {"GBP": 1.0}
    pairs = list(SUPPORTED_CURRENCIES.values())
    client = _get_client(api_key)
    try:
        bulk = client.bulk_real_time(pairs)
        for ccy, pair in SUPPORTED_CURRENCIES.items():
            row = bulk.get(pair) or {}
            rate = row.get("close") or row.get("previousClose")
            if rate and float(rate) > 0:
                rates[ccy] = float(rate)
            elif fallback_rates and ccy in fallback_rates:
                rates[ccy] = float(fallback_rates[ccy])
                log.warning("Using cached FX rate for %s", ccy)
            else:
                rates[ccy] = 1.0
                log.warning("No FX rate for %s; defaulted to 1.0", ccy)
        return rates
    except EODHDError as exc:
        log.warning("Bulk FX fetch failed, falling back per-currency: %s", exc)
        for ccy in SUPPORTED_CURRENCIES:
            rates[ccy] = get_fx_rate(ccy, api_key=api_key, fallback_rates=fallback_rates)
        return rates


# ---------------------------------------------------------------------------
# Public API — ticker resolver
# ---------------------------------------------------------------------------

def resolve_ticker(
    query: str,
    api_key: str | None = None,
    preferred_exchange: str | None = None,
    asset_type: str | None = None,
) -> dict:
    """
    Resolve a name or ambiguous ticker to an EODHD `{CODE}.{EXCHANGE}` symbol.

    Returns {'resolved', 'name', 'isin', 'type', 'exchange', 'currency', 'alternatives'}.
    `resolved` is None when EODHD returns no matches.
    """
    client = _get_client(api_key)
    # Translate vault code to EODHD exchange code if provided.
    eodhd_exchange: str | None = None
    if preferred_exchange:
        eodhd_exchange = VAULT_TO_EODHD.get(preferred_exchange, preferred_exchange)

    try:
        matches = client.search(
            query,
            preferred_exchange=eodhd_exchange,
            asset_type=asset_type,
            limit=10,
        )
    except EODHDError as exc:
        return {"resolved": None, "error": str(exc), "alternatives": []}

    if not matches:
        return {"resolved": None, "alternatives": []}

    top = matches[0]
    code = top.get("Code", "")
    exchange = top.get("Exchange", "")
    symbol = f"{code}.{exchange}" if code and exchange else code
    return {
        "resolved": symbol,
        "name": top.get("Name"),
        "isin": top.get("ISIN"),
        "type": top.get("Type"),
        "exchange": exchange,
        "currency": top.get("Currency"),
        "alternatives": [
            {
                "symbol": f"{m.get('Code','')}.{m.get('Exchange','')}",
                "name": m.get("Name"),
                "exchange": m.get("Exchange"),
                "currency": m.get("Currency"),
                "type": m.get("Type"),
            }
            for m in matches[1:6]
        ],
    }
