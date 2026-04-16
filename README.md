# investments-mcp

A local MCP server for managing an Obsidian-based investment vault. Exposes 15 tools covering trade operations, live market data via EODHD (yfinance fallback for SGX/Borsa Italiana), and dividend tracking.

Runs persistently on the machine that holds the vault. Accessible from any machine on your local network via SSE transport.

## Tools

### Trade operations
| Tool | What it does |
|------|-------------|
| `close_position` | Write all canonical CLAUDE.md closing fields; rename file; update _Watchlist.md |
| `open_position` | Create TICKER.md with correct template and computed target_allocation_gbp |
| `add_to_position` | Add shares; weighted-average entry_price |
| `trim_position` | Partial sell; compute realized P&L on lot |
| `get_position` | Read frontmatter (read-only) |
| `list_positions` | List positions by strategy/status |

### Dividend tracking
| Tool | What it does |
|------|-------------|
| `log_dividend` | Record a received dividend payment: appends to `_dividend_log.json` and increments `dividends_received_gbp` in the position's frontmatter. Accepts total broker amount (not per-share). Works for active and closed positions via `shares_at_payment`. |
| `get_dividend_history` | Query `_dividend_log.json` with optional filters for ticker, strategy, and year. Returns a formatted table with per-entry and total GBP figures. |
| `update_dividends` | Fetch trailing 12-month dividend data from EODHD (yfinance fallback for SGX) for all active positions and write four frontmatter fields: `div_per_share`, `div_yield_pct`, `div_income_gbp`, `next_ex_div_date`. Pass a list of tickers to update specific positions only. |

### Market data (EODHD)
| Tool | What it does |
|------|-------------|
| `get_quote` | Live price via EODHD (yfinance fallback for SGX/Borsa Italiana) |
| `get_historical` | OHLCV history via EODHD (yfinance fallback) |
| `get_fx_rate_tool` | Live GBP cross-rate via EODHD; updates config cache |
| `update_all_prices` | Batch price update for all active positions via EODHD |
| `get_portfolio_snapshot` | All active positions with last-cached values |
| `check_exits` | Exit alerts (stop-loss, drawdown, target proximity) |

## Setup

### 1. Install

```bash
git clone https://github.com/YOUR_USERNAME/investments-mcp
cd investments-mcp
pip install -e .
```

### 2. Configure

```bash
cp config.example.yaml config.local.yaml
# Edit config.local.yaml — fill in vault.root, fmp.api_key, server.auth_token
```

Generate an auth token:
```bash
openssl rand -hex 32
```

### 3. Run the server

```bash
python3 -m investments_mcp.server
```

### 4. Register with Claude Code

**On the host machine** — use stdio transport (no OAuth friction, no port needed):
```bash
claude mcp add investments-vault -- python3 -m investments_mcp.server
```

**From a remote machine** — SSH port-forward the HTTP endpoint, then register via HTTP:
```bash
# On remote machine: forward localhost:8765 → vault host:8765
ssh -L 8765:localhost:8765 user@vault-host -N &

# Register (token optional when tunnelled through SSH)
claude mcp add investments-vault \
  --transport http \
  --header "Authorization: Bearer YOUR_TOKEN" \
  http://localhost:8765/mcp
```

Start the HTTP listener on the vault host:
```bash
python3 -m investments_mcp.server --http
```

### 5. Run as a system service (optional)

The systemd service runs in **stdio mode** (Claude Code spawns it on demand).
For a persistent HTTP listener (remote access), pass `--http` in `ExecStart`:

```bash
# Edit investments-mcp.service — update WorkingDirectory and User if needed
# For HTTP mode: append --http to ExecStart line
sudo cp investments-mcp.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now investments-mcp
sudo systemctl status investments-mcp
```

## Migrating update_prices.py

After installing this package, `Scripts/update_prices.py` in the vault imports
from `investments_mcp.prices` instead of yfinance directly. Ensure the package
is installed in the same Python environment used by the vault's cron job.

## Config reference

See `config.example.yaml` for all supported fields.

`config.local.yaml` is **gitignored** — it contains your FMP API key, vault path,
and auth token. Never commit it.
