"""
exchanges.py — single source of truth for vault exchange code mappings.

Vault position files use friendly codes (NYSE, LSE, XETRA, HKEX, ...).
This module maps them to EODHD exchange codes (or, when EODHD does not
cover the venue, to Yahoo Finance suffixes used by the yfinance fallback).
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


# Vault code → EODHD exchange code.
# Verified 2026-04-15 against EODHD's /api/exchanges-list response.
# Values of None mean "EODHD does not cover this venue" — callers must
# fall back to yfinance via VAULT_TO_YAHOO.
VAULT_TO_EODHD: dict[str, str | None] = {
    "NYSE":            "US",       # Virtual exchange combining NYSE/NASDAQ/OTC.
    "LSE":             "LSE",
    "TSX":             "TO",
    "TSXV":            "V",
    "XETRA":           "XETRA",
    "HKEX":            "HK",
    "ASX":             "AU",
    "WSE":             "WAR",
    "TASE":            "TA",
    "OSE":             "OL",
    "EURONEXT_PA":     "PA",
    "EURONEXT_AM":     "AS",
    "EURONEXT_LI":     "LS",
    "VSE":             "VI",       # Vault uses VSE for Vienna (per OMV.md).
    "ATHEX":           "AT",
    "SGX":             None,       # Not in EODHD's exchanges-list — fall back to yfinance.
    "BORSA_ITALIANA":  None,       # Not in EODHD's exchanges-list — fall back to yfinance.
}


# Vault code → Yahoo Finance suffix (fallback path for venues EODHD lacks).
# Empty string means "no suffix" (US listings).
VAULT_TO_YAHOO: dict[str, str] = {
    "NYSE":            "",
    "LSE":             ".L",
    "TSX":             ".TO",
    "TSXV":            ".V",
    "XETRA":           ".DE",
    "HKEX":            ".HK",
    "ASX":             ".AX",
    "SGX":             ".SI",
    "WSE":             ".WA",
    "TASE":            ".TA",
    "OSE":             ".OL",
    "EURONEXT_PA":     ".PA",
    "EURONEXT_AM":     ".AS",
    "EURONEXT_LI":     ".LS",
    "VSE":             ".VI",
    "ATHEX":           ".AT",
    "BORSA_ITALIANA":  ".MI",
}


def _normalise_ticker_for_eodhd(ticker: str, exchange: str) -> str:
    """Apply EODHD's ticker-part rules: uppercase, zero-padded HKEX, dots→hyphens."""
    t = ticker.upper()
    if exchange == "HKEX":
        t = t.zfill(4)
    if "." in t:
        # EODHD forbids dots in the ticker part (e.g. BRK.A → BRK-A).
        t = t.replace(".", "-")
    return t


def build_eodhd_symbol(ticker: str, exchange: str) -> str | None:
    """
    Return '{TICKER}.{EODHD_EXCHANGE}' or None.

    Returns None when:
      - exchange is empty, 'skip', or unknown
      - the exchange exists in the vault but EODHD does not cover it
        (e.g. SGX, BORSA_ITALIANA — callers should fall back to yfinance)
    """
    if not ticker or not exchange or exchange == "skip":
        return None
    eodhd_code = VAULT_TO_EODHD.get(exchange)
    if eodhd_code is None:
        return None
    t = _normalise_ticker_for_eodhd(ticker, exchange)
    return f"{t}.{eodhd_code}"


def build_yahoo_symbol(ticker: str, exchange: str) -> str | None:
    """Return a yfinance-ready symbol for fallback paths, or None on skip/unknown."""
    if not ticker or not exchange or exchange == "skip":
        return None
    suffix = VAULT_TO_YAHOO.get(exchange)
    if suffix is None:
        return None
    t = ticker.upper()
    if exchange == "HKEX":
        t = t.zfill(4)
    return t + suffix


def has_eodhd_coverage(exchange: str) -> bool:
    """True when the exchange has a non-None EODHD mapping."""
    return VAULT_TO_EODHD.get(exchange) is not None
