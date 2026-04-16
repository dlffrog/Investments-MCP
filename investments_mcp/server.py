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

import argparse
import logging
import sys
from typing import Optional

import uvicorn
from fastmcp import FastMCP

from .config import load_config, save_fx_cache
from .prices import (
    get_equity_quote,
    get_fx_rate,
    get_historical_ohlcv,
    resolve_ticker as _resolve_ticker,
)
from .exchanges import build_eodhd_symbol, build_yahoo_symbol
from .trade_ops import (
    close_position as _close_position,
    open_position as _open_position,
    add_to_position as _add_to_position,
    trim_position as _trim_position,
    log_dividend as _log_dividend,
    get_dividend_history as _get_dividend_history,
    get_position as _get_position,
    list_positions as _list_positions,
    update_all_prices as _update_all_prices,
    update_dividends as _update_dividends,
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

_LOCALHOST = {"127.0.0.1", "::1", "localhost"}


class _BearerAuth:
    """
    ASGI middleware that enforces a shared bearer token for remote clients.
    Localhost connections (127.0.0.1 / ::1) are trusted without a token,
    so local Claude Code sessions work without needing --header at registration.
    """

    def __init__(self, app, token: str):
        self._app = app
        self._expected = f"Bearer {token}"

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            client_host = (scope.get("client") or ("", 0))[0]
            if client_host not in _LOCALHOST:
                headers = {k.lower(): v for k, v in scope.get("headers", [])}
                auth = headers.get(b"authorization", b"").decode("utf-8", errors="replace")
                if auth != self._expected:
                    if scope["type"] == "http":
                        body = b"Unauthorized"
                        await send({
                            "type": "http.response.start",
                            "status": 401,
                            "headers": [
                                [b"content-type", b"text/plain"],
                                [b"content-length", str(len(body)).encode()],
                            ],
                        })
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
    exchange: str = "",
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
    exchange: exchange code (e.g. "LSE", "TSX", "NYSE").
    For Crowded Market Report: provide stop_loss, risk_pct, atr_multiple.
    For Asymmetric Capital Gains: provide theme (e.g. "Agriculture", "Shipping").
    For Dividend Portfolio: provide country.
    entry_date format: YYYY-MM-DD
    """
    try:
        return _open_position(
            ticker=ticker, name=name, strategy=strategy,
            entry_price=entry_price, shares=shares, entry_date=entry_date,
            currency=currency, sector=sector, exchange=exchange,
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
def log_dividend(
    ticker: str,
    total_amount: float,
    date: str,
    currency: str = "",
    strategy: str = "",
    amount_per_share: float = 0.0,
    shares_at_payment: int = 0,
) -> str:
    """
    Record a received dividend payment.

    Appends an entry to _dividend_log.json and increments dividends_received_gbp
    in the position's frontmatter. Works for both active and closed positions.

    ticker:            Position ticker (e.g. "TRMD", "IBT").
    total_amount:      Total received in the dividend currency — the figure the broker shows.
    date:              Payment or ex-dividend date (YYYY-MM-DD).
    currency:          Currency of total_amount. Defaults to the position's currency.
                       Pass "GBP" for UK dividends already stated in GBP.
    strategy:          Required for dual-strategy tickers (FRO, DHT, SBLK, LOMA, PAM, 1171, NHC).
    amount_per_share:  Optional dividend rate per share (for record-keeping).
    shares_at_payment: Required when position is closed (shares=0). Pass the share count
                       held at the time of the dividend.
    """
    try:
        return _log_dividend(
            ticker=ticker,
            total_amount=total_amount,
            date_str=date,
            currency=currency or None,
            strategy=strategy or None,
            amount_per_share=amount_per_share,
            shares_at_payment=shares_at_payment,
        )
    except (AmbiguousTicker, PositionNotFound, ValueError) as exc:
        return _fmt_error(exc)


@mcp.tool()
def get_dividend_history(ticker: str = "", strategy: str = "", year: int = 0) -> str:
    """
    Return received dividend payment history from _dividend_log.json.
    Optionally filter by ticker, strategy (substring match), or calendar year.
    year: 4-digit integer e.g. 2026. Pass 0 (default) for all years.
    """
    try:
        return _get_dividend_history(
            ticker=ticker or None,
            strategy=strategy or None,
            year=year or None,
        )
    except Exception as exc:
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
def get_quote(ticker: str, exchange: str = "", currency: str = "") -> str:
    """
    Fetch a live price quote for a ticker via EODHD.
    exchange: exchange code (e.g. "LSE", "TSX", "NYSE"). If blank, the name
    or ticker is resolved through EODHD's search endpoint.
    currency: optional hint (e.g. "EUR", "USD") to override the default
    exchange-based currency inference. Use for instruments whose currency
    differs from the exchange norm (e.g. EUR-denominated ETFs on the LSE).
    """
    try:
        cfg = load_config()
        key = cfg.get("eodhd", {}).get("api_key")

        if exchange:
            symbol = build_eodhd_symbol(ticker, exchange) or build_yahoo_symbol(ticker, exchange) or ticker
        else:
            info = _resolve_ticker(ticker, api_key=key)
            symbol = info.get("resolved") or ticker

        q = get_equity_quote(symbol, api_key=key, currency_hint=currency or None)
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
    exchange: str = "",
) -> str:
    """
    Fetch historical OHLCV data for a ticker via EODHD.
    start_date / end_date format: YYYY-MM-DD
    exchange: exchange code (e.g. "LSE", "TSX", "NYSE"). If blank, the name
    or ticker is resolved through EODHD's search endpoint.
    """
    try:
        cfg = load_config()
        key = cfg.get("eodhd", {}).get("api_key")
        if exchange:
            symbol = build_eodhd_symbol(ticker, exchange) or build_yahoo_symbol(ticker, exchange) or ticker
        else:
            info = _resolve_ticker(ticker, api_key=key)
            symbol = info.get("resolved") or ticker
        rows = get_historical_ohlcv(
            symbol,
            start_date=start_date,
            end_date=end_date or None,
            api_key=key,
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
def get_fx_rate_tool(currency: str) -> str:
    """
    Fetch the current GBP cross-rate for a currency via EODHD.
    Returns units-per-GBP (e.g. USD: 1.28 means £1 = $1.28).
    currency: USD, CAD, EUR, AUD, HKD, SGD, PLN, ILS, NOK, MXN
    """
    try:
        cfg = load_config()
        fallback = cfg.get("fx_rates", {})
        rate = get_fx_rate(
            currency,
            api_key=cfg.get("eodhd", {}).get("api_key"),
            fallback_rates=fallback,
        )
        save_fx_cache({currency: rate})
        return f"£1 = {rate:.6f} {currency}"
    except Exception as exc:
        return _fmt_error(exc)


@mcp.tool()
def resolve_ticker(query: str, preferred_exchange: str = "", asset_type: str = "") -> str:
    """
    Resolve a company name or ambiguous ticker to an EODHD symbol via search.

    query: name or ticker (e.g. "Apple", "Vopak", "BMW")
    preferred_exchange: optional vault exchange code to bias the match (e.g. "LSE")
    asset_type: optional EODHD type filter (e.g. "Common Stock", "ETF")
    """
    try:
        cfg = load_config()
        info = _resolve_ticker(
            query,
            api_key=cfg.get("eodhd", {}).get("api_key"),
            preferred_exchange=preferred_exchange or None,
            asset_type=asset_type or None,
        )
        if not info.get("resolved"):
            return f"No matches for '{query}'."
        lines = [
            f"Resolved: {info['resolved']}",
            f"  Name:     {info.get('name') or '—'}",
            f"  ISIN:     {info.get('isin') or '—'}",
            f"  Type:     {info.get('type') or '—'}",
            f"  Currency: {info.get('currency') or '—'}",
        ]
        alts = info.get("alternatives") or []
        if alts:
            lines.append("\nAlternatives:")
            for a in alts:
                lines.append(f"  - {a['symbol']:<20} {a.get('name') or ''} ({a.get('currency') or ''})")
        return "\n".join(lines)
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
def update_dividends(tickers: str = "") -> str:
    """
    Fetch dividend data from EODHD and update active position frontmatter.
    Updates: div_per_share, div_yield_pct, div_income_gbp, next_ex_div_date.
    tickers: optional comma-separated list to update only specific tickers, e.g. "TRMD,WPM"
    Equivalent to running Scripts/update_dividends.py.
    """
    try:
        ticker_list = [t.strip() for t in tickers.split(",") if t.strip()] if tickers else None
        return _update_dividends(ticker_list)
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
    parser = argparse.ArgumentParser(description="investments-mcp server")
    parser.add_argument(
        "--http",
        action="store_true",
        help="Run HTTP transport (port 8765) instead of stdio. Use for remote access via SSH tunnel.",
    )
    args = parser.parse_args()

    cfg = load_config()
    server_cfg = cfg.get("server", {})

    if not args.http:
        # Stdio transport — used for local Claude Code registration.
        # No OAuth metadata, no port binding, no auth needed.
        log.info("Starting investments-mcp (stdio transport)")
        mcp.run()
        return

    # HTTP transport — for remote access via SSH tunnel or LAN.
    port = int(server_cfg.get("port", 8765))
    host = server_cfg.get("host", "0.0.0.0")
    auth_token = server_cfg.get("auth_token", "")

    log.info("Starting investments-mcp on %s:%d (HTTP transport)", host, port)

    try:
        app = mcp.http_app()
    except AttributeError:
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
        app = _BearerAuth(app, auth_token)
        log.info("Bearer token auth enabled for non-localhost connections")

    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
