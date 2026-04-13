# investments-mcp

A local MCP server for managing an Obsidian-based investment vault. Exposes 12 tools covering trade operations and live market data via OpenBB + FMP.

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

### Market data (OpenBB)
| Tool | What it does |
|------|-------------|
| `get_quote` | Live price via FMP (falls back to yfinance) |
| `get_historical` | OHLCV history |
| `get_fx_rate_tool` | Live GBP cross-rate; updates config cache |
| `update_all_prices` | Batch price update for all active positions |
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

On the **host machine**:
```bash
claude mcp add investments-vault --transport sse http://localhost:8765/sse
```

From a **remote machine** (replace IP and token):
```bash
claude mcp add investments-vault \
  --transport sse \
  --header "Authorization: Bearer YOUR_TOKEN" \
  http://192.168.x.x:8765/sse
```

### 5. Run as a system service (optional)

```bash
# Edit investments-mcp.service — update WorkingDirectory and User if needed
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
