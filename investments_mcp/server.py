"""
server.py — FastMCP server exposing 12 investment vault tools.

Transport: Streamable HTTP (FastMCP 3.x) — accessible over a network.
Auth:      Bearer token checked via ASGI middleware.
Endpoint:  http://host:port/mcp

Run:
    python3 -m investments_mcp.server

Register in Claude Code (local):
    claude mcp add investments-vault --transport http http://localhost:8765/mcp

Register from another machine:
    claude mcp add investments-vault \\
      --transport http \\
      --header "Authorization: Bearer <your_token>" \\
      http://192.168.x.x:8765/mcp
"""

from __future__ import annotations

import logging
import sys
from typing import Optional

import uvicorn
from fastmcp import FastMCP

from .config import load_config
from .prices import get_equity_quote, get_fx_rate, get_historical_ohlcv, get_all_fx_rates
from .config import save_fx_cache
from .trade_ops import (
    close_position as _close_position,
    open_position as _open_position,
    add_to_position as _add_to_position,
    trim_position as _trim_position,
    get_position as _get_position,
    list_positions as _list_positions,
    update_all_prices as _update_all_prices,
    get_portfolio_snapshot as _get_portfolio_snapshot,
    check_exits as _check_exits,
)
from .vault import AmbiguousTicker, PositionNotFound

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)

mcp = FastMCP("investments-vault")


# ---------------------------------------------------------------------------
# ASGI bearer token middleware
# ---------------------------------------------------------------------------

class _BearerAuth:
    """Pure-ASGI middleware that enforces a shared bearer token."""

    def __init__(self, app, token: str):
        self._app = app
        self._expected = f"Bearer {token}"

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            headers = {k.lower(): v for k, v in scope.get("headers", [])}
            auth = headers.get(b"authorization", b"").decode("utf-8", errors="replace")
            if auth != self._expected:
                if scope["type"] == "http":
                    body = b"Unauthorized"
                    response_start = {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [
                            [b"content-type", b"text/plain"],
                            [b"content-length", str(len(body)).encode()],
                        ],
                    }
                    await send(response_start)
                    await send({"type": "http.response.body", "body": body})
                return
        await self._app(scope, receive, send)


# ---------------------------------------------------------------------------
# Tool helpers
# ---------------------------------------------------------------------------

def _fmt_error(exc: Exception) -> str:
    return f"Error: {exc}"


# ---------------------------------------------------------------------------
# Trade operation tools
# ---------------------------------------------------------------------------

@mcp.tool()
def close_position(
    ticker: str,
    exit_price: float,
    shares: int,
    exit_date: str,
    exit_reason: str,
    reentry_condition: str = "",
    strategy: str = "",
) -> str:
    """
    Close a position and write all canonical CLAUDE.md closing fields.

    exit_reason must be one of: stop-loss, profit-target, thesis-broken
    exit_date format: YYYY-MM-DD
    reentry_condition: if provided, renames TICKER.md → TICKER-YYYYMMDD.md
                       and appends a task to _Watchlist.md.
    strategy: required only for dual-strategy tickers (FRO, DHT, SBLK, etc.)
    """
    try:
        return _close_position(
            ticker=ticker,
            exit_price=exit_price,
            shares=shares,
            exit_date=exit_date,
            exit_reason=exit_reason,
            reentry_condition=reentry_condition,
            strategy=strategy or None,
        )
    except (AmbiguousTicker, PositionNotFound, ValueError) as exc:
        return _fmt_error(exc)


@mcp.tool()
def open_position(
    ticker: str,
    name: str,
    strategy: str,
    entry_price: float,
    shares: int,
    entry_date: str,
    currency: str,
    sector: str,
    yahoo_ticker: str = "",
    target_price: float = 0.0,
    target_multiple: int = 0,
    time_horizon_years: int = 5,
    catalyst: str = "",
    catalyst_date: str = "",
    stop_loss: float = 0.0,
    risk_pct: float = 1.0,
    atr_multiple: float = 2.0,
    theme: str = "",
    country: str = "",
) -> str:
    """
    Create a new position file (TICKER.md) with frontmatter and body stub.

    strategy must be one of the 10 vault strategies.
    For Crowded Market Report: provide stop_loss, risk_pct, atr_multiple.
    For Asymmetric Capital Gains: provide theme (e.g. "Agriculture", "Shipping").
    For Dividend Portfolio: provide country.
    entry_date format: YYYY-MM-DD
    """
    try:
        return _open_position(
            ticker=ticker, name=name, strategy=strategy,
            entry_price=entry_price, shares=shares, entry_date=entry_date,
            currency=currency, sector=sector, yahoo_ticker=yahoo_ticker,
            target_price=target_price, target_multiple=target_multiple,
            time_horizon_years=time_horizon_years, catalyst=catalyst,
            catalyst_date=catalyst_date, stop_loss=stop_loss,
            risk_pct=risk_pct, atr_multiple=atr_multiple,
            theme=theme, country=country,
        )
    except (FileExistsError, ValueError) as exc:
        return _fmt_error(exc)


@mcp.tool()
def add_to_position(
    ticker: str,
    shares: int,
    price: float,
    date: str,
    notes: str = "",
    strategy: str = "",
) -> str:
    """
    Add shares to an existing position.
    Updates weighted-average entry_price, cost_basis_total, shares, market_value.
    date format: YYYY-MM-DD
    strategy: required only for dual-strategy tickers.
    """
    try:
        return _add_to_position(
            ticker=ticker, shares=shares, price=price,
            date_str=date, notes=notes, strategy=strategy or None,
        )
    except (AmbiguousTicker, PositionNotFound, ValueError) as exc:
        return _fmt_error(exc)


@mcp.tool()
def trim_position(
    ticker: str,
    shares: int,
    price: float,
    date: str,
    notes: str = "",
    strategy: str = "",
) -> str:
    """
    Partially sell shares from an existing position.
    Computes realized P&L for the sold lot.
    date format: YYYY-MM-DD
    strategy: required only for dual-strategy tickers.
    """
    try:
        return _trim_position(
            ticker=ticker, shares=shares, price=price,
            date_str=date, notes=notes, strategy=strategy or None,
        )
    except (AmbiguousTicker, PositionNotFound, ValueError) as exc:
        return _fmt_error(exc)


@mcp.tool()
def get_position(ticker: str, strategy: str = "") -> str:
    """
    Return current frontmatter for a position (read-only).
    strategy: required only for dual-strategy tickers (FRO, DHT, SBLK, LOMA, PAM, 1171, NHC).
    """
    try:
        data = _get_position(ticker, strategy or None)
        lines = [f"Position: {data.get('file', ticker)}"]
        for k, v in data.items():
            if k != "file" and v is not None:
                lines.append(f"  {k}: {v}")
        return "\n".join(lines)
    except (AmbiguousTicker, PositionNotFound) as exc:
        return _fmt_error(exc)


@mcp.tool()
def list_positions(strategy: str = "", status: str = "active") -> str:
    """
    List positions, optionally filtered by strategy and/or status.
    status defaults to 'active'. Pass '' for all statuses.
    """
    try:
        rows = _list_positions(
            strategy=strategy or None,
            status=status or None,
        )
        if not rows:
            return "No positions found matching those filters."
        lines = [f"{'File':<30} {'Ticker':<8} {'Strategy':<26} {'Status':<8} {'P&L%':<8} {'GBP Val':>10}"]
        lines.append("-" * 100)
        for r in rows:
            pnl = r.get("unrealized_pnl_pct")
            pnl_str = f"{pnl:+.1f}%" if pnl is not None else "—"
            gbp = r.get("market_value_gbp")
            gbp_str = f"£{gbp:,.0f}" if gbp else "—"
            lines.append(
                f"{r['file']:<30} {r['ticker']:<8} {r['strategy']:<26} "
                f"{r['status']:<8} {pnl_str:<8} {gbp_str:>10}"
            )
        lines.append(f"\n{len(rows)} position(s)")
        return "\n".join(lines)
    except Exception as exc:
        return _fmt_error(exc)


# ---------------------------------------------------------------------------
# Market data tools
# ---------------------------------------------------------------------------

@mcp.tool()
def get_quote(ticker: str, yahoo_ticker: str = "", provider: str = "fmp") -> str:
    """
    Fetch a live price quote for a ticker via OpenBB.
    yahoo_ticker: override the symbol sent to the provider (e.g. "WPP.L" for LSE).
    provider: fmp (default) or yfinance.
    """
    try:
        cfg = load_config()
        symbol = yahoo_ticker or ticker
        q = get_equity_quote(
            symbol,
            provider=provider or "fmp",
            fmp_api_key=cfg.get("fmp", {}).get("api_key"),
        )
        change = q.get("change_pct")
        change_str = f" ({change:+.2f}%)" if change is not None else ""
        return (
            f"{ticker} ({symbol})\n"
            f"  Price: {q['price']} {q['currency']}{change_str}\n"
            f"  Provider: {q['provider']}"
        )
    except Exception as exc:
        return _fmt_error(exc)


@mcp.tool()
def get_historical(
    ticker: str,
    start_date: str,
    end_date: str = "",
    yahoo_ticker: str = "",
    provider: str = "fmp",
) -> str:
    """
    Fetch historical OHLCV data for a ticker.
    start_date / end_date format: YYYY-MM-DD
    """
    try:
        cfg = load_config()
        symbol = yahoo_ticker or ticker
        rows = get_historical_ohlcv(
            symbol,
            start_date=start_date,
            end_date=end_date or None,
            provider=provider or "fmp",
            fmp_api_key=cfg.get("fmp", {}).get("api_key"),
        )
        if not rows:
            return f"No historical data returned for {symbol}"
        lines = [f"Historical prices for {ticker} ({symbol}) — {len(rows)} rows"]
        lines.append(f"{'Date':<12} {'Open':>8} {'High':>8} {'Low':>8} {'Close':>8} {'Volume':>12}")
        lines.append("-" * 60)
        for r in rows[-20:]:  # last 20 rows to keep output manageable
            vol = f"{r['volume']:,}" if r.get("volume") else "—"
            lines.append(
                f"{r['date']:<12} {r.get('open') or '—':>8} {r.get('high') or '—':>8} "
                f"{r.get('low') or '—':>8} {r.get('close') or '—':>8} {vol:>12}"
            )
        if len(rows) > 20:
            lines.append(f"(showing last 20 of {len(rows)} rows)")
        return "\n".join(lines)
    except Exception as exc:
        return _fmt_error(exc)


@mcp.tool()
def get_fx_rate_tool(currency: str, provider: str = "fmp") -> str:
    """
    Fetch the current GBP cross-rate for a currency.
    Returns units-per-GBP (e.g. USD: 1.28 means £1 = $1.28).
    currency: USD, CAD, EUR, AUD, HKD, SGD, PLN, ILS, NOK, MXN
    """
    try:
        cfg = load_config()
        fallback = cfg.get("fx_rates", {})
        rate = get_fx_rate(
            currency,
            provider=provider or "fmp",
            fallback_rates=fallback,
            fmp_api_key=cfg.get("fmp", {}).get("api_key"),
        )
        save_fx_cache({currency: rate})
        return f"£1 = {rate:.6f} {currency}"
    except Exception as exc:
        return _fmt_error(exc)


@mcp.tool()
def update_all_prices(tickers: str = "") -> str:
    """
    Batch-update prices for all active vault positions using OpenBB.
    tickers: optional comma-separated list to update only specific tickers, e.g. "AAPL,QCOM"
    Equivalent to running Scripts/update_prices.py.
    """
    try:
        ticker_list = [t.strip() for t in tickers.split(",") if t.strip()] if tickers else None
        return _update_all_prices(ticker_list)
    except Exception as exc:
        return _fmt_error(exc)


@mcp.tool()
def get_portfolio_snapshot() -> str:
    """
    Return a summary table of all active positions with last-cached prices and GBP values.
    Prices reflect the last update_all_prices run (not live — call update_all_prices first).
    """
    try:
        rows = _get_portfolio_snapshot()
        if not rows:
            return "No active positions."
        total_gbp = sum(r.get("market_value_gbp") or 0 for r in rows)
        lines = [
            f"{'Ticker':<8} {'Strategy':<26} {'Shares':>7} {'Entry':>8} {'Current':>8} {'P&L%':>7} {'GBP Val':>10}",
            "-" * 85,
        ]
        for r in rows:
            pnl = r.get("unrealized_pnl_pct")
            pnl_str = f"{pnl:+.1f}%" if pnl is not None else "—"
            gbp = r.get("market_value_gbp") or 0
            lines.append(
                f"{r['ticker']:<8} {r['strategy']:<26} {r.get('shares') or 0:>7} "
                f"{r.get('entry_price') or '—':>8} {r.get('current_price') or '—':>8} "
                f"{pnl_str:>7} £{gbp:>9,.0f}"
            )
        lines.append("-" * 85)
        lines.append(f"{'Total':<68} £{total_gbp:>9,.0f}")
        lines.append(f"\n{len(rows)} active positions")
        return "\n".join(lines)
    except Exception as exc:
        return _fmt_error(exc)


@mcp.tool()
def check_exits(verbose: bool = False) -> str:
    """
    Run exit-condition checks across all active positions.
    Returns CRITICAL (stop breached), WARNING (near stop / large drawdown), INFO (near target).
    Excludes: Deployment Ammunition, Asymmetric Capital Gains, Dividend Portfolio.
    """
    try:
        return _check_exits(verbose=verbose)
    except Exception as exc:
        return _fmt_error(exc)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    cfg = load_config()
    server_cfg = cfg.get("server", {})
    port = int(server_cfg.get("port", 8765))
    host = server_cfg.get("host", "0.0.0.0")
    auth_token = server_cfg.get("auth_token", "")

    log.info("Starting investments-mcp on %s:%d", host, port)

    # Get the FastMCP ASGI app.
    # FastMCP 3.x uses streamable-HTTP via http_app() → endpoint /mcp
    try:
        app = mcp.http_app()
    except AttributeError:
        # FastMCP 2.x fallback
        try:
            app = mcp.sse_app()
            log.info("Using SSE transport (FastMCP 2.x) — endpoint: /sse")
        except AttributeError:
            log.error(
                "Could not get FastMCP ASGI app. "
                "Ensure fastmcp>=2.0.0 is installed: pip install 'fastmcp>=2.0.0'"
            )
            sys.exit(1)
    else:
        log.info("Using streamable-HTTP transport (FastMCP 3.x) — endpoint: /mcp")

    if auth_token:
        log.info("Bearer token auth enabled")
        app = _BearerAuth(app, auth_token)
    else:
        log.warning(
            "No auth_token configured — server is open to anyone on the network. "
            "Set server.auth_token in config.local.yaml."
        )

    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
