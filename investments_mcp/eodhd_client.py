"""
eodhd_client.py — thin direct-HTTP wrapper around EODHD's REST API.

This is a small library (not a CLI) mirroring the paths and conventions
used by the `eodhd-api` skill's `scripts/eodhd_client.py` so the two
stay aligned. Endpoint docs:
  ~/.claude/plugins/marketplaces/eodhd-claude-skills/skills/eodhd-api/references/endpoints/
"""

from __future__ import annotations

import logging
import os
from typing import Any

import requests

log = logging.getLogger(__name__)

BASE_URL = "https://eodhd.com/api"
DEFAULT_TIMEOUT = 15


class EODHDError(RuntimeError):
    """Raised for non-2xx responses, empty payloads, or JSON decode failures."""


class EODHDClient:
    """
    Direct-HTTP client for EODHD's REST API.

    The token is never included in error messages. All methods expect
    symbols already in `{TICKER}.{EXCHANGE}` form; symbol construction
    lives in `investments_mcp.exchanges`.
    """

    def __init__(self, api_key: str | None = None, *, timeout: float = DEFAULT_TIMEOUT):
        key = api_key or os.environ.get("EODHD_API_TOKEN")
        if not key:
            raise EODHDError(
                "EODHD API key missing (pass api_key or set EODHD_API_TOKEN)"
            )
        self._key = key
        self._timeout = timeout
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "investments-mcp/1.0"})

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        p: dict[str, Any] = {"api_token": self._key, "fmt": "json"}
        if params:
            p.update({k: v for k, v in params.items() if v is not None})
        url = f"{BASE_URL}{path}"
        try:
            resp = self._session.get(url, params=p, timeout=self._timeout)
        except requests.RequestException as exc:
            raise EODHDError(f"HTTP error for {path}: {exc}") from exc
        if resp.status_code >= 400:
            raise EODHDError(
                f"EODHD {path} returned {resp.status_code}: {resp.text[:200]}"
            )
        try:
            return resp.json()
        except ValueError as exc:
            raise EODHDError(f"EODHD {path} returned non-JSON: {exc}") from exc

    # ------------------------------------------------------------------
    # Real-time quotes
    # ------------------------------------------------------------------

    def real_time_quote(self, symbol: str) -> dict[str, Any]:
        """GET /real-time/{symbol} — single ticker live quote."""
        data = self._get(f"/real-time/{symbol}")
        if not isinstance(data, dict) or not data:
            raise EODHDError(f"Empty real-time payload for {symbol}")
        return data

    def bulk_real_time(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        """
        GET /real-time/{first}?s={rest,comma,joined} — one call, many tickers.

        Returns {symbol_code_with_exchange: quote_dict}. EODHD returns a dict
        for a single symbol and a list for multiple, so we normalise both.
        """
        if not symbols:
            return {}
        first, *rest = symbols
        params = {"s": ",".join(rest)} if rest else None
        data = self._get(f"/real-time/{first}", params=params)

        rows: list[dict[str, Any]]
        if isinstance(data, dict):
            rows = [data]
        elif isinstance(data, list):
            rows = data
        else:
            raise EODHDError(f"Unexpected bulk real-time payload: {type(data).__name__}")

        out: dict[str, dict[str, Any]] = {}
        for idx, row in enumerate(rows):
            code = row.get("code") or (symbols[idx] if idx < len(symbols) else None)
            if code:
                out[code] = row
        return out

    # ------------------------------------------------------------------
    # Historical OHLCV
    # ------------------------------------------------------------------

    def historical_eod(
        self,
        symbol: str,
        start: str | None = None,
        end: str | None = None,
        period: str = "d",
    ) -> list[dict[str, Any]]:
        """GET /eod/{symbol} — daily/weekly/monthly OHLCV."""
        params = {"from": start, "to": end, "period": period}
        data = self._get(f"/eod/{symbol}", params=params)
        if not isinstance(data, list):
            raise EODHDError(f"Unexpected historical payload for {symbol}")
        return data

    # ------------------------------------------------------------------
    # Search / ticker resolver
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        preferred_exchange: str | None = None,
        asset_type: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """GET /search/{query} — find symbols by name or ticker."""
        params = {
            "type": asset_type,
            "exchange": preferred_exchange,
            "limit": limit,
        }
        data = self._get(f"/search/{query}", params=params)
        if not isinstance(data, list):
            raise EODHDError(f"Unexpected search payload for '{query}'")
        return data

    # ------------------------------------------------------------------
    # Fundamentals
    # ------------------------------------------------------------------

    def fundamentals(
        self,
        symbol: str,
        filter_: str = "Highlights,Valuation,Financials::Cash_Flow::yearly",
    ) -> dict[str, Any]:
        """
        GET /fundamentals/{symbol}?filter=...

        The unfiltered payload is ~800KB per call; the filter reduces it
        to ~5KB at the same 10-call cost (per the skill's fundamentals-api.md).
        """
        params = {"filter": filter_} if filter_ else None
        data = self._get(f"/fundamentals/{symbol}", params=params)
        if not data:
            raise EODHDError(f"Empty fundamentals payload for {symbol}")
        return data

    # ------------------------------------------------------------------
    # Reference / metadata
    # ------------------------------------------------------------------

    def exchanges_list(self) -> list[dict[str, Any]]:
        """GET /exchanges-list — authoritative list of supported exchanges."""
        data = self._get("/exchanges-list/")
        if not isinstance(data, list):
            raise EODHDError("Unexpected exchanges-list payload")
        return data
