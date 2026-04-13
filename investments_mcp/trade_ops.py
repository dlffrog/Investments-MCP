"""
trade_ops.py — Trade operation logic: open, close, add, trim positions.

All derived-field formulas are implemented here and match CLAUDE.md exactly.
The vault.py module handles file I/O and ticker resolution; this module handles
the business logic of what fields to set and how to compute them.

Derived-field formulas (from CLAUDE.md):
    exit_proceeds      = shares × exit_price                        (local currency)
    realized_pnl_pct   = (exit_price - entry_price) / entry_price × 100   (1 dp)
    realized_pnl_gbp   = (exit_price - entry_price) × shares / fx_rate    (2 dp)
    add entry_price    = (old_cost + new_cost) / (old_shares + new_shares) (weighted avg)
    target_allocation_gbp (AGS) = 10_000 / count_of_positions_in_theme
    target_allocation_gbp (DIV) = round(100_000 / 81, 0) = 1_235
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import frontmatter as fm

from .config import load_config, save_fx_cache
from .prices import get_fx_rate, get_equity_quote, get_all_fx_rates
from .vault import (
    resolve_ticker,
    load_position,
    save_position,
    rename_for_reentry,
    append_watchlist_task,
    append_position_history_row,
    find_active_positions,
    AmbiguousTicker,
    PositionNotFound,
)

log = logging.getLogger(__name__)

VALID_EXIT_REASONS = {"stop-loss", "profit-target", "thesis-broken"}

THEMATIC_STRATEGIES = {
    "Precious Metals", "Oil", "Defense", "Electrification", "Core", "Technology",
}
EXTERNALLY_MANAGED = {"Asymmetric Capital Gains", "Dividend Portfolio"}
SKIP_EXIT_CHECK = {"Deployment Ammunition"} | EXTERNALLY_MANAGED

# Alerts for check_exits
from dataclasses import dataclass

@dataclass
class Alert:
    ticker: str
    strategy: str
    level: str   # CRITICAL | WARNING | INFO
    message: str


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_positions_dir(cfg: dict) -> Path:
    vault_root = Path(cfg["vault"]["root"])
    return vault_root / cfg["vault"]["positions"]


def _get_watchlist_path(cfg: dict) -> Path:
    vault_root = Path(cfg["vault"]["root"])
    return vault_root / "_Watchlist.md"


def _fmp_key(cfg: dict) -> str | None:
    return cfg.get("fmp", {}).get("api_key")


def _default_provider(cfg: dict) -> str:
    return cfg.get("openbb", {}).get("default_provider", "fmp")


def _live_fx_rate(currency: str, cfg: dict) -> float:
    """Fetch live FX rate, falling back to cached config values."""
    fallback = cfg.get("fx_rates", {})
    provider = _default_provider(cfg)
    return get_fx_rate(
        currency,
        provider=provider,
        fallback_rates=fallback,
        fmp_api_key=_fmp_key(cfg),
    )


def _count_theme_positions(positions_dir: Path, theme: str) -> int:
    """Count active AGS positions sharing the same theme."""
    count = 0
    for p in sorted(positions_dir.glob("*-AGS.md")):
        try:
            post = fm.load(p)
            if (
                post.metadata.get("status") == "active"
                and post.metadata.get("theme", "").lower() == theme.lower()
            ):
                count += 1
        except Exception:
            pass
    return max(count, 1)


def _compute_target_allocation_gbp(strategy: str, positions_dir: Path, theme: str | None) -> float | None:
    """Return target_allocation_gbp per CLAUDE.md rules."""
    if strategy == "Asymmetric Capital Gains":
        if not theme:
            return None
        n = _count_theme_positions(positions_dir, theme)
        return round(10_000 / n, 0)
    if strategy == "Dividend Portfolio":
        return 1_235.0
    return None  # Caller supplies for thematic strategies


# ---------------------------------------------------------------------------
# close_position
# ---------------------------------------------------------------------------

def close_position(
    ticker: str,
    exit_price: float,
    shares: int,
    exit_date: str,
    exit_reason: str,
    reentry_condition: str = "",
    strategy: str | None = None,
) -> str:
    """
    Close a position: write all canonical closing fields from CLAUDE.md.

    If reentry_condition is provided:
      - Renames TICKER.md → TICKER-YYYYMMDD.md
      - Appends re-entry task to _Watchlist.md

    Returns a confirmation string.
    """
    if exit_reason not in VALID_EXIT_REASONS:
        raise ValueError(
            f"Invalid exit_reason '{exit_reason}'. "
            f"Must be one of: {', '.join(sorted(VALID_EXIT_REASONS))}"
        )

    cfg = load_config()
    positions_dir = _get_positions_dir(cfg)

    filepath = resolve_ticker(ticker, positions_dir, strategy)
    post, meta = load_position(filepath)

    entry_price = float(meta.get("entry_price", 0) or 0)
    currency = meta.get("currency", "GBP")
    position_strategy = meta.get("strategy", strategy or "")
    position_ticker = meta.get("ticker", ticker)

    # Derived fields
    exit_proceeds = round(shares * exit_price, 2)
    realized_pnl_pct = (
        round((exit_price - entry_price) / entry_price * 100, 1)
        if entry_price else 0.0
    )
    fx_rate = _live_fx_rate(currency, cfg)
    realized_pnl_gbp = round(
        (exit_price - entry_price) * shares / fx_rate, 2
    ) if entry_price else 0.0

    # Write closing fields
    meta["status"] = "closed"
    meta["exit_date"] = exit_date
    meta["exit_price"] = exit_price
    meta["exit_reason"] = exit_reason
    meta["exit_proceeds"] = exit_proceeds
    meta["shares"] = 0
    meta["market_value"] = 0
    meta["market_value_gbp"] = 0
    meta["realized_pnl_pct"] = realized_pnl_pct
    meta["realized_pnl_gbp"] = realized_pnl_gbp
    meta["last_updated"] = exit_date

    if reentry_condition:
        meta["reentry_condition"] = reentry_condition
    elif "reentry_condition" in meta and not meta["reentry_condition"]:
        # Remove blank reentry_condition (CLAUDE.md: omit entirely when not provided)
        del meta["reentry_condition"]

    # Append to Position History table in body
    post = append_position_history_row(
        post,
        exit_date,
        f"{'Stop-loss' if exit_reason == 'stop-loss' else 'Exit'} ({exit_reason})",
        -shares,
        exit_price,
        "Full exit",
    )

    # Rename file if re-entry is plausible
    closed_filename = filepath.name
    if reentry_condition:
        try:
            new_path = rename_for_reentry(filepath, exit_date)
            closed_filename = new_path.name
            filepath = new_path
        except ValueError as e:
            log.warning("Could not rename file: %s", e)

    save_position(filepath, post)

    # Append to _Watchlist.md
    if reentry_condition:
        watchlist = _get_watchlist_path(cfg)
        if watchlist.exists():
            append_watchlist_task(
                watchlist,
                position_ticker,
                position_strategy,
                exit_date,
                reentry_condition,
                closed_filename,
            )

    return (
        f"Closed {position_ticker} ({position_strategy})\n"
        f"  Exit: {exit_price} × {shares} shares = {currency} {exit_proceeds}\n"
        f"  Realized P&L: {realized_pnl_pct:+.1f}% / GBP {realized_pnl_gbp:+,.2f}\n"
        f"  File: {filepath.name}"
        + (f"\n  Re-entry condition added to _Watchlist.md" if reentry_condition else "")
    )


# ---------------------------------------------------------------------------
# open_position
# ---------------------------------------------------------------------------

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
    theme: str = "",            # AGS only
    country: str = "",          # Dividend Portfolio only
) -> str:
    """
    Open a new position: create TICKER.md with correct frontmatter.

    Selects the thematic or momentum template based on strategy.
    Computes target_allocation_gbp per CLAUDE.md rules.
    Warns if ticker ends in .OL (Oslo Stock Exchange — not accessible).
    """
    cfg = load_config()
    positions_dir = _get_positions_dir(cfg)

    # Oslo restriction warning
    yt = yahoo_ticker or ticker
    if yt.endswith(".OL"):
        log.warning(
            "Ticker %s uses Oslo Stock Exchange (.OL) which is not accessible. "
            "Find the NYSE/NASDAQ equivalent and update yahoo_ticker accordingly.",
            yt,
        )

    # Check for existing position (prevent accidental overwrite)
    filepath = positions_dir / f"{ticker}.md"
    if filepath.exists():
        raise FileExistsError(
            f"Position file {filepath.name} already exists. "
            "Close and rename the existing position first, or use add_to_position()."
        )

    # Derive market values
    fx_rate = _live_fx_rate(currency, cfg)
    cost_basis = round(shares * entry_price, 2)
    market_value = cost_basis
    market_value_gbp = round(market_value / fx_rate, 2)

    # target_allocation_gbp
    target_alloc = _compute_target_allocation_gbp(strategy, positions_dir, theme)

    # Build frontmatter
    is_momentum = strategy == "Crowded Market Report"

    meta: dict = {
        "ticker": ticker,
        "name": name,
        "sector": sector,
        "strategy": strategy,
        "status": "active",
        "entry_date": entry_date,
        "entry_price": entry_price,
        "current_price": entry_price,
        "shares": shares,
        "cost_basis_total": cost_basis,
        "market_value": market_value,
        "market_value_gbp": market_value_gbp,
        "unrealized_pnl": 0.0,
        "unrealized_pnl_pct": 0.0,
        "currency": currency,
        "last_updated": entry_date,
        "tags": ["position"],
    }

    if yahoo_ticker:
        meta["yahoo_ticker"] = yahoo_ticker

    if target_alloc is not None:
        meta["target_allocation_gbp"] = target_alloc

    if is_momentum:
        meta["risk_pct"] = risk_pct
        meta["stop_loss"] = stop_loss
        meta["stop_type"] = "ATR"
        meta["atr_multiple"] = atr_multiple
        meta["atr_value"] = 0.0
        meta["target_price"] = target_price
        meta["risk_reward_ratio"] = 0.0
        meta["regime"] = ""
        meta["timeframe"] = "position"
        meta["tags"] = ["momentum", "position"]
    else:
        meta["target_multiple"] = target_multiple
        meta["target_price"] = target_price
        meta["time_horizon_years"] = time_horizon_years
        meta["catalyst"] = catalyst
        meta["catalyst_date"] = catalyst_date
        meta["max_allocation_pct"] = 10.0
        if strategy == "Asymmetric Capital Gains" and theme:
            meta["theme"] = theme
        if strategy == "Dividend Portfolio" and country:
            meta["country"] = country

    # Build body from template stub
    body = _build_position_body(ticker, name, strategy, is_momentum)

    post = fm.Post(body, **meta)
    fm.dump(post, filepath)

    return (
        f"Opened {ticker} ({strategy})\n"
        f"  Entry: {entry_price} × {shares} shares = {currency} {cost_basis}\n"
        f"  GBP value: £{market_value_gbp:,.2f}"
        + (f" / Target allocation: £{target_alloc:,.0f}" if target_alloc else "")
        + f"\n  File: {filepath.name}"
    )


def _build_position_body(ticker: str, name: str, strategy: str, is_momentum: bool) -> str:
    if is_momentum:
        return f"""# {ticker} - {name}

## Trade Setup
**Entry trigger:**
**Timeframe:**
**Regime at entry:**

### Risk Parameters
| Parameter | Value |
|-----------|-------|
| Entry price | |
| Stop loss | |
| Risk per share | |
| Portfolio risk | |
| Target price | |
| R:R ratio | |

## Exit Rules
### Stop Loss
- **Trailing stop method:** Ratchet up by 1 ATR after each ATR of profit
- **Hard stop:** Never move stop down

### Profit Targets
| Level | Price | Action |
|-------|-------|--------|
| 1R | | Trail stop to breakeven |
| 2R | | Sell 1/3 |
| 3R | | Sell 1/3, trail remainder |

## Technical Notes
*Key levels, patterns, catalysts:*

## Position History
| Date | Action | Shares Δ | Price | Notes |
|------|--------|----------|-------|-------|

## Post-Trade Review
*(Fill after close)*
- **Result:**
- **R-multiple achieved:**
- **Did I follow the plan?**
- **Lesson:**
"""
    else:
        return f"""# {ticker} - {name}

## Investment Thesis


## Catalysts


## Valuation Framework
| Metric | At Entry | Current | At Target | Notes |
|--------|----------|---------|-----------|-------|
| Revenue ($M) | | | | |
| EV/Revenue | | | | |
| EV/EBITDA | | | | |
| P/E | | | | |
| FCF Yield | | | | |
| ROIC | | | | |

## Risk Factors


## Position History
| Date | Action | Shares Δ | Price | Notes |
|------|--------|----------|-------|-------|

## Notes

"""


# ---------------------------------------------------------------------------
# add_to_position
# ---------------------------------------------------------------------------

def add_to_position(
    ticker: str,
    shares: int,
    price: float,
    date_str: str,
    notes: str = "",
    strategy: str | None = None,
) -> str:
    """
    Add shares to an existing position.
    Updates entry_price (weighted average), cost_basis_total, shares, market_value.
    """
    cfg = load_config()
    positions_dir = _get_positions_dir(cfg)

    filepath = resolve_ticker(ticker, positions_dir, strategy)
    post, meta = load_position(filepath)

    old_shares = int(meta.get("shares", 0) or 0)
    old_entry = float(meta.get("entry_price", price) or price)
    old_cost = float(meta.get("cost_basis_total", old_shares * old_entry) or 0)
    currency = meta.get("currency", "GBP")

    new_cost = round(price * shares, 2)
    total_shares = old_shares + shares
    total_cost = round(old_cost + new_cost, 2)
    new_avg_entry = round(total_cost / total_shares, 4) if total_shares else price

    fx_rate = _live_fx_rate(currency, cfg)
    market_value = round(total_shares * price, 2)
    market_value_gbp = round(market_value / fx_rate, 2)

    meta["shares"] = total_shares
    meta["entry_price"] = new_avg_entry
    meta["cost_basis_total"] = total_cost
    meta["market_value"] = market_value
    meta["market_value_gbp"] = market_value_gbp
    meta["unrealized_pnl_pct"] = round((price / new_avg_entry - 1) * 100, 1)
    meta["last_updated"] = date_str

    post = append_position_history_row(post, date_str, "Add", shares, price, notes)
    save_position(filepath, post)

    return (
        f"Added {shares} shares of {ticker} at {price}\n"
        f"  New total: {total_shares} shares @ avg {new_avg_entry} "
        f"(cost basis: {currency} {total_cost})\n"
        f"  GBP value: £{market_value_gbp:,.2f}"
    )


# ---------------------------------------------------------------------------
# trim_position
# ---------------------------------------------------------------------------

def trim_position(
    ticker: str,
    shares: int,
    price: float,
    date_str: str,
    notes: str = "",
    strategy: str | None = None,
) -> str:
    """
    Trim (partially sell) shares from an existing position.
    Computes partial realized_pnl_gbp for the sold lot.
    """
    cfg = load_config()
    positions_dir = _get_positions_dir(cfg)

    filepath = resolve_ticker(ticker, positions_dir, strategy)
    post, meta = load_position(filepath)

    old_shares = int(meta.get("shares", 0) or 0)
    if shares > old_shares:
        raise ValueError(
            f"Cannot trim {shares} shares — position only has {old_shares}. "
            "Use close_position() for a full exit."
        )

    entry_price = float(meta.get("entry_price", price) or price)
    currency = meta.get("currency", "GBP")
    fx_rate = _live_fx_rate(currency, cfg)

    # Realized P&L on the trimmed lot
    partial_pnl_pct = round((price - entry_price) / entry_price * 100, 1) if entry_price else 0.0
    partial_pnl_gbp = round((price - entry_price) * shares / fx_rate, 2) if entry_price else 0.0

    remaining = old_shares - shares
    market_value = round(remaining * price, 2)
    market_value_gbp = round(market_value / fx_rate, 2)

    meta["shares"] = remaining
    meta["market_value"] = market_value
    meta["market_value_gbp"] = market_value_gbp
    meta["unrealized_pnl_pct"] = round((price / entry_price - 1) * 100, 1) if entry_price else 0.0
    meta["last_updated"] = date_str

    # Accumulate realized P&L if field exists
    existing_realized = float(meta.get("realized_pnl_gbp", 0) or 0)
    meta["realized_pnl_gbp"] = round(existing_realized + partial_pnl_gbp, 2)

    if remaining == 0:
        meta["status"] = "closed"
        meta["market_value_gbp"] = 0

    post = append_position_history_row(post, date_str, "Trim", -shares, price, notes)
    save_position(filepath, post)

    return (
        f"Trimmed {shares} shares of {ticker} at {price}\n"
        f"  Realized on trim: {partial_pnl_pct:+.1f}% / GBP {partial_pnl_gbp:+,.2f}\n"
        f"  Remaining: {remaining} shares, GBP value: £{market_value_gbp:,.2f}"
    )


# ---------------------------------------------------------------------------
# get_position
# ---------------------------------------------------------------------------

def get_position(ticker: str, strategy: str | None = None) -> dict:
    """Return frontmatter metadata for the given position (read-only)."""
    cfg = load_config()
    positions_dir = _get_positions_dir(cfg)
    filepath = resolve_ticker(ticker, positions_dir, strategy)
    _, meta = load_position(filepath)
    return {
        "file": filepath.name,
        **{k: v for k, v in meta.items()},
    }


# ---------------------------------------------------------------------------
# list_positions
# ---------------------------------------------------------------------------

def list_positions(
    strategy: str | None = None,
    status: str | None = None,
) -> list[dict]:
    """List positions matching optional strategy and/or status filters."""
    cfg = load_config()
    positions_dir = _get_positions_dir(cfg)

    results = []
    for p in sorted(positions_dir.glob("*.md")):
        try:
            post = fm.load(p)
            meta = post.metadata
            if strategy and meta.get("strategy", "") != strategy:
                continue
            if status and meta.get("status", "") != status:
                continue
            results.append({
                "file": p.name,
                "ticker": meta.get("ticker", ""),
                "name": meta.get("name", ""),
                "strategy": meta.get("strategy", ""),
                "status": meta.get("status", ""),
                "unrealized_pnl_pct": meta.get("unrealized_pnl_pct"),
                "market_value_gbp": meta.get("market_value_gbp"),
            })
        except Exception:
            pass
    return results


# ---------------------------------------------------------------------------
# update_all_prices
# ---------------------------------------------------------------------------

def update_all_prices(tickers: list[str] | None = None) -> str:
    """
    Batch-update prices for all active positions (or a filtered list of tickers).
    Replicates update_prices.py logic but using OpenBB via prices.py.
    """
    cfg = load_config()
    positions_dir = _get_positions_dir(cfg)
    provider = _default_provider(cfg)
    fmp_key = _fmp_key(cfg)
    fallback_fx = cfg.get("fx_rates", {})

    filter_set = set(tickers) if tickers else None

    # Collect files to update
    files_to_update: list[tuple[str, str, Path]] = []  # (yahoo_ticker, broker_ticker, path)
    for p in sorted(positions_dir.glob("*.md")):
        post = fm.load(p)
        meta = post.metadata
        broker = meta.get("ticker", "")
        status = meta.get("status", "")

        if status != "active" and not filter_set:
            continue
        if filter_set and broker not in filter_set:
            continue

        yt = meta.get("yahoo_ticker", "")
        if yt == "skip" or broker == "n/a":
            continue

        fetch = yt or broker
        files_to_update.append((fetch, broker, p))

    if not files_to_update:
        return "No positions to update."

    # Fetch FX rates once
    fx_rates = get_all_fx_rates(provider=provider, fallback_rates=fallback_fx, fmp_api_key=fmp_key)
    save_fx_cache({k: v for k, v in fx_rates.items() if k != "GBP"})

    updated = failed = 0
    lines = [f"Updating {len(files_to_update)} positions (provider: {provider})..."]

    for yahoo_ticker, broker_ticker, filepath in files_to_update:
        try:
            q = get_equity_quote(yahoo_ticker, provider=provider, fmp_api_key=fmp_key)
            price = q["price"]
            currency = q["currency"]

            post, meta = load_position(filepath)

            # Apply divisor (gilts)
            divisor = meta.get("yahoo_price_divisor", 1) or 1
            if divisor != 1:
                price = price / divisor

            meta["current_price"] = round(price, 4) if price < 1 else round(price, 2)
            meta["last_updated"] = date.today().isoformat()

            entry_price = float(meta.get("entry_price", 0) or 0)
            if entry_price:
                pnl = round((price / entry_price - 1) * 100, 1)
                meta["unrealized_pnl_pct"] = pnl
                # Watermarks
                prev_max = meta.get("max_unrealized_pnl_pct")
                prev_min = meta.get("min_unrealized_pnl_pct")
                if prev_max is None or pnl > prev_max:
                    meta["max_unrealized_pnl_pct"] = pnl
                if prev_min is None or pnl < prev_min:
                    meta["min_unrealized_pnl_pct"] = pnl

            shares = meta.get("shares", 0) or 0
            pos_currency = meta.get("currency", "GBP")
            if shares > 0:
                mv = round(shares * price, 2)
                meta["market_value"] = mv
                rate = fx_rates.get(pos_currency, 1.0)
                meta["market_value_gbp"] = round(mv / rate, 2)

            save_position(filepath, post)
            lines.append(f"  ✓ {broker_ticker}: {price}")
            updated += 1

        except Exception as exc:
            lines.append(f"  ✗ {broker_ticker}: {exc}")
            failed += 1

    lines.append(f"\nDone. Updated: {updated}, Failed: {failed}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# get_portfolio_snapshot
# ---------------------------------------------------------------------------

def get_portfolio_snapshot() -> list[dict]:
    """Return all active positions with current prices + GBP values."""
    cfg = load_config()
    positions_dir = _get_positions_dir(cfg)

    rows = []
    for p in sorted(positions_dir.glob("*.md")):
        post = fm.load(p)
        meta = post.metadata
        if meta.get("status") != "active":
            continue
        yt = meta.get("yahoo_ticker", "")
        if yt == "skip" or meta.get("ticker") == "n/a":
            continue
        rows.append({
            "ticker": meta.get("ticker", ""),
            "name": meta.get("name", ""),
            "strategy": meta.get("strategy", ""),
            "shares": meta.get("shares", 0),
            "entry_price": meta.get("entry_price"),
            "current_price": meta.get("current_price"),
            "unrealized_pnl_pct": meta.get("unrealized_pnl_pct"),
            "market_value_gbp": meta.get("market_value_gbp"),
            "currency": meta.get("currency"),
            "last_updated": meta.get("last_updated"),
        })
    return rows


# ---------------------------------------------------------------------------
# check_exits
# ---------------------------------------------------------------------------

def check_exits(verbose: bool = False) -> str:
    """
    Run exit-condition checks and return a formatted alert report.
    Mirrors the logic in Scripts/check_exits.py.
    """
    cfg = load_config()
    positions_dir = _get_positions_dir(cfg)
    thresholds = cfg.get("alerts", {})

    alerts: list[Alert] = []

    for filepath in sorted(positions_dir.glob("*.md")):
        post = fm.load(filepath)
        meta = post.metadata

        if meta.get("status") != "active":
            continue

        strategy = meta.get("strategy", "")
        if strategy in SKIP_EXIT_CHECK:
            continue

        if strategy == "Crowded Market Report":
            alerts.extend(_check_momentum(meta, thresholds))
        elif strategy in THEMATIC_STRATEGIES:
            alerts.extend(_check_thematic(meta, thresholds))

    if not alerts:
        return "No alerts. All positions within parameters."

    priority = {"CRITICAL": 0, "WARNING": 1, "INFO": 2}
    alerts.sort(key=lambda a: priority.get(a.level, 99))

    lines = ["Exit Condition Alerts", "=" * 60]
    for a in alerts:
        lines.append(f"[{a.level}] {a.ticker} ({a.strategy}): {a.message}")
    lines.append(f"\nTotal: {len(alerts)} alert(s)")

    if any(a.level == "CRITICAL" for a in alerts):
        lines.append("\n⚠ CRITICAL alerts require immediate action.")

    return "\n".join(lines)


def _check_momentum(meta: dict, thresholds: dict) -> list[Alert]:
    alerts = []
    ticker = meta.get("ticker", "?")
    strategy = meta.get("strategy", "")
    current = float(meta.get("current_price", 0) or 0)
    stop = float(meta.get("stop_loss", 0) or 0)
    if not current or not stop:
        return alerts
    dist = ((current - stop) / current) * 100
    if current <= stop:
        alerts.append(Alert(ticker, strategy, "CRITICAL",
            f"BELOW STOP! Current: {current}, Stop: {stop}. Execute exit immediately."))
    elif dist <= thresholds.get("momentum_stop_distance_pct", 5.0):
        alerts.append(Alert(ticker, strategy, "WARNING",
            f"Near stop: {current} is {dist:.1f}% above stop at {stop}"))
    return alerts


def _check_thematic(meta: dict, thresholds: dict) -> list[Alert]:
    alerts = []
    ticker = meta.get("ticker", "?")
    strategy = meta.get("strategy", "")
    current = float(meta.get("current_price", 0) or 0)
    target = float(meta.get("target_price", 0) or 0)
    pnl_pct = meta.get("unrealized_pnl_pct", 0) or 0
    tm = meta.get("target_multiple", 0) or 0
    proximity = thresholds.get("thematic_target_proximity_pct", 20)

    if tm == 0:
        return alerts

    high_conviction = tm >= 5

    if current and target > 0:
        pct_of_target = (current / target) * 100
        if pct_of_target >= (100 - proximity):
            alerts.append(Alert(ticker, strategy, "INFO",
                f"Approaching target: {current} is {pct_of_target:.0f}% of target {target} ({tm}x)"))

    drawdown_threshold = -50 if high_conviction else -30
    if isinstance(pnl_pct, (int, float)) and pnl_pct <= drawdown_threshold:
        alerts.append(Alert(ticker, strategy, "WARNING",
            f"Large drawdown: {pnl_pct:.1f}% from entry. Review thesis."))

    return alerts
