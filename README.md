# BTC 15-minute lab

A research toolkit for hunting a trading edge in Kalshi's 15-minute crypto
markets (`KXBTC15M` and the ETH/SOL/XRP/DOGE copies): a historical downloader and
backtester, a live order-book and trade recorder, and a set of edge tests.

**Result so far: no edge found.** Every idea was judged against acceptance
criteria written before seeing results (edge above +0.5¢ per contract after
costs with a 95% range excluding zero, holding out of sample, surviving realistic
execution). Pricing models, speed vs. Binance futures, thinner coins,
time-of-day effects and "traders overbet YES" all failed, several of them only
once tested on data they were never tuned on. The full record, including bear
cases, is in [`RESEARCH_LOG.md`](RESEARCH_LOG.md).

> Not financial advice. Not affiliated with or endorsed by Kalshi. Uses public
> market data only; no account or API key is needed to run anything here.

## The original question

The project started by testing one idea against Kalshi's real history. In the 15-minute
"BTC price up in next 15 mins?" markets (series `KXBTC15M`), if you buy the side
priced around 60¢:

1. Does it win more than 60% of the time, by enough to cover the fee?
2. What happens to $100 if you bet 20% of the bankroll on every trade?

No API key is needed for any of this. It only uses public market data.

## How to use it

1. Double-click **`1_download_data.bat`**. The first run downloads every window
   Kalshi has (back to Dec 2025, about 29,000 of them) plus BTC's 1-minute price
   from Coinbase. It takes about an hour. Run it again any time to add new
   windows; that only takes a minute.
2. Double-click **`2_open_dashboard.bat`**. The first time, it installs what it
   needs. Then the dashboard opens in your browser. Close the black window to
   stop it.

## What the dashboard shows

- **Does 60¢ win 60%?** The win rate, the win rate you needed to break even
  (price + fee), the edge per contract with a 95% range, and a plain verdict.
  Two charts split the edge by price paid and by minute of the window.
- **Model vs market.** A fair-value model (a Monte Carlo replay method,
  adapted to the 15-minute contract) prices every minute of every window from
  Coinbase's BTC price and buys whatever Kalshi priced too cheaply. It shows
  whose probabilities were more accurate, whether the model's confidence paid,
  and month-by-month results. Each month is priced using only earlier history.
- **$100 bankroll simulator.** Replays either bet's real trades 2,000 times and
  compares your 20% rule with ¼ Kelly and a flat bet.
- **Replay a window.** Pick any past 15 minutes to see BTC against its target
  and how the odds moved, with the bet's entry marked.

The controls on the left change everything: the price zone, which side, which
minutes, the largest spread you'd trade into, the fee, and the date range.

## The live recorder and the edge tests

- **`3_start_recorder.bat`** records Kalshi's live BTC and ETH 15-minute order
  books and trades, live BTC/ETH prices from Coinbase, Kraken and Bitstamp, and,
  for windows ending on the hour, the matching hourly markets. Leave its window
  open: the tests get sharper with every hour of data. No API key is needed. It
  keeps the PC awake and uses about 150–200 MB a day.
- `live_edge.py` tests whether Kalshi lags BTC (speed) and whether hourly and
  15-minute prices ever disagree by enough to lock in a profit.
- `flow.py` tests which kinds of trades lose money: markouts by size, side,
  price and time left.
- `edge.py` runs maker vs taker tests on the downloaded history. Set
  `KALSHI_SERIES=KXETH15M` (or SOL/XRP/DOGE) to test the other coins.
- **`RESEARCH_LOG.md`** lists every idea tested so far and how it came out.

## Your own trades

**`5_my_trades.bat`** opens a private page (localhost only, port 8502) with your
balance, open positions, fills and settled results. It needs a Kalshi API key:
the key ID in `%LOCALAPPDATA%\kalshi-btc15\secrets\key_id.txt` and the private
key in `...\secrets\private_key.pem`. Both stay outside this folder, so they can't
be committed. It only reads your portfolio; it can't place or cancel orders.

## Where things are

| What | Where |
|---|---|
| Code | this folder |
| Data | `%LOCALAPPDATA%\kalshi-btc15\data\` (kept out of OneDrive; one database per market) |
| Live recordings | `%LOCALAPPDATA%\kalshi-btc15\live\` (one file per hour) |
| API key files | `%LOCALAPPDATA%\kalshi-btc15\secrets\` (never in OneDrive, never in chat) |
| Python environment | `%LOCALAPPDATA%\kalshi-btc15\.venv` |

The files: `fetch_data.py` downloads the data, `backtest.py` has the math,
`fairvalue.py` is the model (run it directly for a text report), and `app.py` is
the dashboard.

## What this backtest can't see

- **Minute snapshots.** Kalshi's history has one price per minute. Real fills
  happen between those snapshots and can be better or worse.
- **Buying at the ask.** The test assumes you take the best ask in that
  snapshot. It ignores order size, which is fine for a $100 account.
- **The fee.** It uses Kalshi's general taker formula, `0.07 × price × (1 − price)`
  per contract. Check kalshi.com/fee-schedule, because fees change.
- **The BTC line is Coinbase.** Kalshi settles on CF Benchmarks' BRTI, which is
  close to Coinbase but not identical. Wins and losses in the test come from
  Kalshi's official results, not from this line.
- **Trying lots of settings.** If you try many zones or minute ranges, one will
  look good by luck. A setting is only interesting if it also holds in the
  newest 30% of the data.
