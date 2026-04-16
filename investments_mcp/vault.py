"""
vault.py — Obsidian vault file I/O, ticker resolution, and _Watchlist.md writes.

Ticker resolution rules (from CLAUDE.md):
  1. TICKER.md — if exists, return it.
  2. TICKER-AGS.md + TICKER-DIV.md — if both exist, raise AmbiguousTicker
     (caller must supply strategy).
  3. Special: ticker == "FRO" + strategy == "Dividend Portfolio" → FERRO.md
  4. PositionNotFound otherwise.

Dual-strategy tickers: 1171, NHC, FRO, DHT, SBLK, LOMA, PAM
Cross-file collision: FERRO.md has ticker:"FRO", yahoo_ticker:"FRO.WA"
"""

from __future__ import annotations

import re
from pathlib import Path

import frontmatter

# Tickers that appear in both AGS and DIV strategies (two separate files each).
DUAL_STRATEGY_TICKERS = {"1171", "NHC", "FRO", "DHT", "SBLK", "LOMA", "PAM"}


class PositionNotFound(Exception):
    """No position file matches the given ticker."""


class AmbiguousTicker(Exception):
    """
    Ticker exists in multiple strategy files; caller must supply strategy.
    Attribute `candidates` holds the list of matching file names.
    """
    def __init__(self, ticker: str, candidates: list[str]):
        self.candidates = candidates
        super().__init__(
            f"Ticker '{ticker}' is ambiguous — found: {candidates}. "
            "Supply a strategy to disambiguate."
        )


# ---------------------------------------------------------------------------
# Ticker → file resolution
# ---------------------------------------------------------------------------

def resolve_ticker(
    ticker: str,
    positions_dir: Path,
    strategy: str | None = None,
) -> Path:
    """
    Resolve a ticker string to an absolute position file path.

    Raises:
        AmbiguousTicker — when dual-strategy files exist and strategy not given.
        PositionNotFound — when no matching file is found.
    """
    ticker = ticker.upper()

    # Special case: FRO + Dividend Portfolio → FERRO.md (Polish Ferro SA, WSE)
    if ticker == "FRO" and strategy and "Dividend" in strategy:
        path = positions_dir / "FERRO.md"
        if path.exists():
            return path

    # Dual-strategy tickers: check for -AGS.md and -DIV.md variants
    if ticker in DUAL_STRATEGY_TICKERS:
        ags_path = positions_dir / f"{ticker}-AGS.md"
        div_path = positions_dir / f"{ticker}-DIV.md"
        candidates = [p.name for p in [ags_path, div_path] if p.exists()]

        if len(candidates) == 2:
            # Both exist — need strategy to disambiguate
            if strategy:
                if "Capital Gains" in strategy or "AGS" in strategy.upper():
                    return ags_path
                if "Dividend" in strategy or "DIV" in strategy.upper():
                    return div_path
                # Try arbitrary suffix (e.g. "SHP" for FRO-SHP.md)
                suffix_path = positions_dir / f"{ticker}-{strategy.upper()}.md"
                if suffix_path.exists():
                    return suffix_path
            raise AmbiguousTicker(ticker, candidates)

        if len(candidates) == 1:
            return positions_dir / candidates[0]

    # Standard single-file resolution
    path = positions_dir / f"{ticker}.md"
    if path.exists():
        return path

    # Check for FERRO (ticker stored as FRO)
    ferro = positions_dir / "FERRO.md"
    if ferro.exists():
        post = frontmatter.load(ferro)
        if post.metadata.get("ticker", "").upper() == ticker:
            return ferro

    raise PositionNotFound(
        f"No position file found for ticker '{ticker}' in {positions_dir}. "
        "Check ticker spelling or use list_positions() to see active positions."
    )


def find_active_positions(positions_dir: Path) -> list[Path]:
    """Return all position files whose frontmatter status == 'active'."""
    result = []
    for p in sorted(positions_dir.glob("*.md")):
        try:
            post = frontmatter.load(p)
            if post.metadata.get("status") == "active":
                result.append(p)
        except Exception:
            pass
    return result


# ---------------------------------------------------------------------------
# Frontmatter I/O
# ---------------------------------------------------------------------------

def load_position(path: Path) -> tuple[frontmatter.Post, dict]:
    """Load a position file. Returns (post, metadata_dict)."""
    post = frontmatter.load(path)
    return post, post.metadata


def save_position(path: Path, post: frontmatter.Post) -> None:
    """Atomically write a position file back."""
    frontmatter.dump(post, path)


# ---------------------------------------------------------------------------
# File rename for re-entry
# ---------------------------------------------------------------------------

def rename_for_reentry(path: Path, exit_date: str) -> Path:
    """
    Rename TICKER.md → TICKER-YYYYMMDD.md using exit_date (YYYY-MM-DD).
    Returns the new path.
    Raises ValueError if the file is not a simple TICKER.md (i.e. already has a suffix).
    """
    stem = path.stem  # e.g. "QCOM"
    if "-" in stem:
        raise ValueError(
            f"File '{path.name}' already has a date/strategy suffix — "
            "rename it manually if needed."
        )
    date_str = exit_date.replace("-", "")  # YYYYMMDD
    new_path = path.parent / f"{stem}-{date_str}.md"
    path.rename(new_path)
    return new_path


# ---------------------------------------------------------------------------
# _Watchlist.md
# ---------------------------------------------------------------------------

_WATCHLIST_SECTION = "## Closed Positions with Re-Entry Conditions"


def append_watchlist_task(
    watchlist_path: Path,
    ticker: str,
    strategy: str,
    exit_date: str,
    reentry_condition: str,
    closed_filename: str,
) -> None:
    """
    Append a re-entry task to the Closed Positions section of _Watchlist.md.

    Format (from CLAUDE.md):
        - [ ] **TICKER** (Strategy, exited YYYY-MM-DD) — <condition> → [[TICKER-YYYYMMDD]]
    """
    stem = Path(closed_filename).stem  # e.g. "NAK-20260316"
    task_line = (
        f"- [ ] **{ticker}** ({strategy}, exited {exit_date}) "
        f"— {reentry_condition} → [[{stem}]]"
    )

    text = watchlist_path.read_text(encoding="utf-8")

    if _WATCHLIST_SECTION in text:
        # Insert after the section heading (and the blank line after it)
        idx = text.index(_WATCHLIST_SECTION) + len(_WATCHLIST_SECTION)
        text = text[:idx] + "\n\n" + task_line + text[idx:]
    else:
        # Section not found — append at the end
        text = text.rstrip() + f"\n\n{_WATCHLIST_SECTION}\n\n{task_line}\n"

    watchlist_path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Position History table
# ---------------------------------------------------------------------------

def append_position_history_row(
    post: frontmatter.Post,
    date_str: str,
    action: str,
    shares_delta: int,
    price: float,
    notes: str = "",
) -> frontmatter.Post:
    """
    Append a row to the markdown Position History table in post.content.

    Expected table header:
        | Date | Action | Shares Δ | Price | Notes |
    """
    shares_str = f"+{shares_delta}" if shares_delta > 0 else str(shares_delta)
    new_row = f"| {date_str} | {action} | {shares_str} | {price} | {notes} |"

    content = post.content
    # Find the Position History table — look for the header row pattern
    header_pattern = re.compile(
        r"(\|\s*Date\s*\|[^\n]*\n\|[-| :]+\n)",
        re.IGNORECASE,
    )
    m = header_pattern.search(content)
    if m:
        # Find the end of the existing table rows
        table_end = m.end()
        row_pattern = re.compile(r"\|[^\n]*\n?")
        pos = table_end
        while pos < len(content):
            rm = row_pattern.match(content, pos)
            if not rm:
                break
            pos = rm.end()
        # Insert new row at pos (after last existing row)
        content = content[:pos] + new_row + "\n" + content[pos:]
    else:
        # No table found — append section
        content = content.rstrip() + "\n\n## Position History\n"
        content += "| Date | Action | Shares Δ | Price | Notes |\n"
        content += "|------|--------|----------|-------|-------|\n"
        content += new_row + "\n"

    post.content = content
    return post
