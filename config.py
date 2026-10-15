"""Shared settings for the BTC 15-minute backtest."""
import os
from pathlib import Path

# Kalshi's "BTC price up in next 15 mins?" markets. Set KALSHI_SERIES to study the
# ETH/SOL/XRP/DOGE versions instead (KXETH15M, ...); each gets its own database.
SERIES = os.environ.get("KALSHI_SERIES", "KXBTC15M")
KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"
COINBASE_API = "https://api.exchange.coinbase.com/products/BTC-USD"

# Data and the Python environment live outside OneDrive: the database grows to
# ~100 MB and changes on every update, which would eat the free 5 GB.
HOME = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "kalshi-btc15"
DB_PATH = HOME / "data" / ("btc15.sqlite" if SERIES == "KXBTC15M" else f"{SERIES.lower()}.sqlite")

# Kalshi's general taker fee per order: ceil(0.07 * contracts * price * (1 - price)),
# rounded up to the cent. Fees change; check kalshi.com/fee-schedule.
TAKER_FEE_RATE = 0.07
