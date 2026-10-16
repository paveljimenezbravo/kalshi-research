# Handoff: start here

Where this project stands, how to run it, and how to build on it. Last updated Oct 16, 2026.
Read this first, then [`RESEARCH_LOG.md`](RESEARCH_LOG.md) (every idea tested, with results) and
its **Candidates** section (the one live lead).

## Where things stand

- **34 ideas tested** against Kalshi's real history, each written into the research log
  *before* its test ran. Nearly all are dead.
- **One candidate, #23:** at 11am ET, sell the tails of Kalshi's daily 5pm "BTC above $X"
  market (buy NO on strikes whose YES is under 20c, YES on strikes over 80c), hold to
  settlement. Backtest: **+2.48c per contract** over 252 days (about +2.7% per dollar staked),
  both halves positive, audited (#34). It repeats more weakly on ETH/SOL/XRP/DOGE (#28). It is
  absent on stock indexes (#29), Kalshi's range brackets (#30) and Polymarket (#33), so it
  lives in Kalshi's own order flow.
- **Status: paper trading** (started Oct 16). No real money is in it.
- **Honest odds:** ~70% that a real edge exists right now; plan on live results around half
  the backtest. It behaves like selling insurance: small wins, rare large losses. No crash day
  happened in the sample.

## Daily routine

| When | What | How |
|---|---|---|
| 10:30-11:30am ET | Log today's #23 paper trades and settle earlier ones | double-click `7_paper_tails.bat` |
| Any time | See your account, trades, deposits | `5_my_trades.bat` (localhost only) |
| Optional, leave running | Record live order books for the maker test (#13) | `3_start_recorder.bat`, then `4_morning_report.bat` the next day |

The paper ledger is `%LOCALAPPDATA%\kalshi-btc15\paper\tails.csv`. It records the price and
the size available at that price, so fills can be judged later.

**Go / stop rules for #23** (also in the log): stop if the paper average is below zero after
~300 trades. Go to small live size only if, after ~1,000 trades (2-3 months across five
coins), the average is above +0.5c with a 95% range above zero and the logged sizes were
enough. Live sizing: about 15-25% of the bankroll a day (quarter Kelly), with a daily cap.
Practical capacity is about $10,000 staked a day on BTC.

**Going live isn't built yet.** A live version of `paper_tails.py` would place
immediate-or-cancel buys through the V2 order endpoint (see gotchas) with a size cap and a
kill switch. **Don't run `6_autosell.bat` while holding #23 positions:** its stop-loss,
size cap and daily stop would sell normal swings of a hold-to-settlement strategy and change
it into something that was never tested.

## Map of the code

| Area | Files |
|---|---|
| Shared plumbing | `config.py` (paths, series), `fetch_data.py` (Kalshi/Coinbase downloads, retries, **signed reads**), `kalshi_auth.py` (API-key signing) |
| BTC 15-minute research | `fetch_data.py` (`1_download_data.bat`), `backtest.py`, `fairvalue.py`, `app.py` (dashboard, `2_open_dashboard.bat`), `edge.py`, `flow.py`, `live_edge.py`, `makersim.py`, `oos_tests.py`, `morning_report.py`, `recorder.py` |
| Indicators | `indicator_test.py` (#17), `indicator_combo.py` (#19), `coin_prices.py` + `trend_fade_test.py` (#20) |
| Weather | `weather_fetch.py`, `weather_test.py` (#16), `weather_obs.py`, `weather_intraday.py` (#22) |
| Daily BTC (the candidate) | `btc_daily_fetch.py`, `btc_daily_test.py` (#23, #24), `audit_23.py` (#34), `deribit_compare.py` (live vs Deribit) |
| Replications of #23 | `alt_daily_test.py` (#28 coins), `index_daily_test.py` (#29 stocks), `range_tails_test.py` (#30), `maxmin_test.py` (#31), `polymarket_tails_test.py` (#33) |
| Other markets | `gas_test.py` (#26), `calibration_atlas.py` (#25), `sports_longshots.py` (#27), `arb_scan.py` (#21, live) |
| Your account | `my_trades.py` (`5_my_trades.bat`), `autosell.py` (`6_autosell.bat`), `paper_tails.py` (`7_paper_tails.bat`) |

Each script's docstring says exactly what it tests and what counts as a pass.

## Where the data lives (not in git)

Everything bulky is under `%LOCALAPPDATA%\kalshi-btc15\`, outside OneDrive and outside the repo:

- `data\*.sqlite`: one database per study (`btc15`, `btc_daily`, `weather`, `alt_daily`,
  `index_daily`, `range_daily`, `polymarket`, `atlas`, `sports`, `gas`, `maxmin`,
  `audit23`, and the per-coin 15-minute ones). All of them can be rebuilt by re-running the
  scripts, which skip what's already downloaded.
- `live\`: the recorder's hourly files. `logs\`: script logs. `paper\`: the paper ledger.
- `secrets\`: `key_id.txt` and `private_key.pem` for your Kalshi API key. **Never commit,
  paste or share these.** The `.gitignore` blocks them, and they sit outside the repo anyway.
- `.venv`: the Python environment.

Rough rebuild times with signed reads: BTC 15-minute history ~1 h; weather ~25 min; daily
BTC ~15 min; each replication test 5-12 min.

## Setting up on a new PC

1. Install Python 3.12+ and run `setup.bat` (makes the environment, installs `requirements.txt`).
2. For anything touching your account (and for faster downloads), create a Kalshi API key and
   save its two files in the `secrets` folder above.
3. Re-run the download scripts you need; they resume where they stopped.

## How research is done here (keep doing it this way)

1. **Write the idea, the test and the pass rule into `RESEARCH_LOG.md` before running.**
2. Judge only on data the idea was never tuned on: a later period (the halves split at
   2026-05-12), other coins or markets, or forward paper trading.
3. The pass bar (Acceptance criteria in the log): edge above +0.5c per contract after fees
   with a 95% range excluding zero, holding out of sample, at least 1,000 trades for
   results-at-settlement tests, surviving realistic execution, and a written bear case.
4. Resample whole days or events for the ranges (markets on the same day move together).
5. Count the tries. With ~35 ideas tested, also check a strict (99.8%) range.
6. Log the result either way, commit, and push. Dead ideas are as valuable as live ones.

## Already ruled out (don't repeat without a genuinely new angle)

- **BTC 15-minute markets:** price levels, fair-value models, speed (even against Binance
  futures), thinner coins, time of day, YES overbetting, heavy favourites late, ~45 chart
  indicators alone and in combination with machine learning, the 15-minute reversal
  (real but priced in), fading trend signals.
- **Weather:** day-old forecasts (#16) and same-day observations (#22): Kalshi's weather
  traders are faster and better informed.
- **Gas prices** (#26): traders see fresher data than AAA's daily number.
- **Daily BTC vs Deribit options** (#24), **range brackets** (#30), **stock indexes** (#29),
  **Polymarket** (#33), **hedging #23 with brackets** (#32), **Kalshi contradicting itself**
  (#21, every open market).
- **Category longshots** (#25) and **sports longshots** (#27): artifacts of stale quotes.

## Open leads, ranked

1. **Big-news-day filter for #23:** on Fed/CPI days, are BTC's tails cheap rather than rich?
   If so, skipping those days would cut #23's crash risk. Uses data already downloaded.
2. **Maker version of #23:** post resting orders instead of taking the price (saves ~1c spread
   and the fee). Simulate a place in the queue with Kalshi's public trade history.
3. **#13, maker test on the 15-minute markets:** needs about a day of recorder data.
4. **Retail-heavy venues:** DraftKings Predictions, FanDuel Predicts, ForecastEx, Polymarket
   US. Check what they list and whether the same event is priced differently than on Kalshi.
   No public price history, so this starts with manual price logging.
5. **Long-dated crypto (yearly max/min) vs options-implied touch probabilities.**
6. **Watch list:** the RSI+Bollinger fade (#20) was positive in 6 of 6 samples but needs ~10,000
   trades to confirm. Round-number strikes within #23 are an untested hint.

## Kalshi API gotchas (learned the hard way)

- `/markets/{ticker}` prices lag the live book by ~10 s; read `/markets/{ticker}/orderbook`
  for decisions. (A stop-loss meant for -20% fired at -47% because of this.)
- **Fills:** every fill adds contracts of its `side` at that side's price, even when labelled
  "sell". Settled profit = revenue + $1 x min(YES, NO contracts) - costs - fees.
- **Orders:** V2 endpoint `POST /portfolio/events/orders`; `side` is `bid`/`ask` on the YES
  leg (closing a NO position = bidding for YES); `count` and `price` are strings;
  `reduce_only` requires `immediate_or_cancel`.
- **Rate limits:** signed reads (with the API key) get their own, larger limit; anonymous
  reads hit 429s quickly. `fetch_data.get_json` signs automatically when a key exists.
- Kalshi's all-series settled list is over a million multi-leg parlays (`KXMVE*`) deep: list
  per series instead.
- Settled markets move to `/historical/...` after roughly two months; the batch candlestick
  endpoint only serves live (recent) markets.
- Some series keep the strike only in the ticker (DOGE daily: `strike_type` "custom",
  `...-T0.0449999`). `expiration_value` is sometimes text ("No").
- Fees: taker `0.07 x P x (1 - P)` per contract, rounded up per order; makers pay nothing on
  standard series. Polymarket's crypto markets use the same formula.
- BTC daily/hourly contracts settle on the 60-second average of CF Benchmarks' BRTI before the
  hour. Position accountability: $25,000 per strike per member.
- Two scripts writing one SQLite file need a connection timeout (`timeout=300`).
- The public order book stores YES bids and NO bids; YES ask = 1 - best NO bid.

## Publishing

The repo is public. Commits use the GitHub no-reply email (set in this repo's local git
config). Never commit data, logs, the paper ledger or anything from `secrets`. Push after
each logged result.
