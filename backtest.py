"""The math behind the dashboard: load the data, find the trades, simulate a bankroll.

Plain functions, so you can also use them from a script or notebook:

    import backtest as bt
    markets, candles = bt.load()
    quotes = bt.side_quotes(bt.snapshots(markets, candles))
    trades = bt.first_trades(bt.in_zone(quotes, 0.55, 0.65))
    print(bt.summarize(trades))

Prices are in dollars (0.60 = 60 cents). A contract pays $1 if its side wins.
"""
import math
import sqlite3
from contextlib import closing

import numpy as np
import pandas as pd

from config import DB_PATH, SERIES, TAKER_FEE_RATE  # noqa: F401 (SERIES is re-exported for reports)

LAST_ENTRY_MINUTE = 14  # minute 15 ends at the close, so there's nothing left to buy


def connect():
    return closing(sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True))


def load():
    """Settled windows that have their minute-by-minute odds downloaded."""
    with connect() as con:
        markets = pd.read_sql("SELECT ticker, open_ts, close_ts, target, final, result FROM markets "
                              "WHERE candles_done = 1 ORDER BY close_ts", con)
        candles = pd.read_sql("SELECT ticker, minute, yes_bid, yes_ask FROM candles", con)
    markets["yes_won"] = (markets.result == "yes").astype(int)
    return markets, candles


def snapshots(markets, candles):
    """One row per window per minute (1-14): the YES bid/ask at the end of that minute.
    A minute with no candle keeps the previous minute's quotes."""
    grid = pd.MultiIndex.from_product([markets.ticker, range(1, LAST_ENTRY_MINUTE + 1)],
                                      names=["ticker", "minute"])
    quotes = (candles.set_index(["ticker", "minute"])[["yes_bid", "yes_ask"]]
              .reindex(grid).groupby(level="ticker").ffill())
    snap = quotes.reset_index().merge(markets[["ticker", "close_ts", "yes_won"]], on="ticker")
    return snap.dropna(subset=["yes_bid", "yes_ask"])


def side_quotes(snap, side="either", max_spread=0.03, fee_rate=TAKER_FEE_RATE):
    """What each side cost to buy at each snapshot, and whether it won.

    YES costs the YES ask; NO costs 1 - YES bid (that's Kalshi's NO ask).
    Snapshots with an empty book or a spread wider than `max_spread` are skipped,
    because you couldn't have traded near those prices."""
    s = snap[(snap.yes_bid > 0) & (snap.yes_ask < 1) & (snap.yes_ask - snap.yes_bid <= max_spread + 1e-9)]
    parts = []
    if side != "no":
        parts.append(s.assign(side="YES", price=s.yes_ask.round(4), won=s.yes_won))
    if side != "yes":  # round so 1 - 0.45 lands on 0.55, not 0.5499999
        parts.append(s.assign(side="NO", price=(1 - s.yes_bid).round(4), won=1 - s.yes_won))
    q = pd.concat(parts, ignore_index=True)[["ticker", "minute", "close_ts", "side", "price", "won"]]
    q["fee"] = fee_rate * q.price * (1 - q.price)  # per contract, before Kalshi's round-up
    q["pnl"] = q.won - q.price - q.fee
    return q


def in_zone(quotes, lo, hi):
    """Snapshots where a side was priced lo..hi. If both sides were, keep the cheaper one."""
    z = quotes[(quotes.price >= lo - 1e-9) & (quotes.price <= hi + 1e-9)]
    return z.sort_values("price").drop_duplicates(["ticker", "minute"])


def first_trades(zone_quotes, minutes=(1, LAST_ENTRY_MINUTE)):
    """At most one trade per window: the first qualifying minute inside `minutes`."""
    z = zone_quotes[zone_quotes.minute.between(*minutes)]
    return (z.sort_values(["ticker", "minute"]).drop_duplicates("ticker")
            .sort_values("close_ts").reset_index(drop=True))


def summarize(trades):
    """Win rate vs what it cost, with a 95% range on the edge per contract."""
    n = len(trades)
    if n == 0:
        return None
    cost = trades.price + trades.fee
    edge = trades.pnl.mean()
    half = 1.96 * trades.pnl.std(ddof=1) / math.sqrt(n) if n > 1 else float("nan")
    days = max((trades.close_ts.max() - trades.close_ts.min()) / 86400, 1)
    return {
        "n": n,
        "win_rate": trades.won.mean(),
        "avg_price": trades.price.mean(),
        "avg_fee": trades.fee.mean(),
        "breakeven": cost.mean(),          # win rate needed to break even
        "edge": edge,                      # dollars per contract, after fees
        "edge_lo": edge - half,
        "edge_hi": edge + half,
        "roi": trades.pnl.sum() / cost.sum(),
        "per_day": n / days,
        "yes_share": (trades.side == "YES").mean(),
    }


def split_check(trades, older=0.7):
    """Summaries for the older 70% and the newest 30% of trades. A real edge
    should show up in both; one that only exists in the older part was luck."""
    cut = int(len(trades) * older)
    return summarize(trades.iloc[:cut]), summarize(trades.iloc[cut:])


def by_bucket(quotes, key):
    """Average edge per bucket. Each window counts once (its minutes share one
    outcome, so they aren't independent), which keeps the 95% range honest."""
    per_window = quotes.groupby([key, "ticker"]).agg(price=("price", "mean"), won=("won", "mean"),
                                                      pnl=("pnl", "mean"), snaps=("pnl", "size"))
    g = per_window.groupby(level=key).agg(price=("price", "mean"), win=("won", "mean"), edge=("pnl", "mean"),
                                          sd=("pnl", "std"), windows=("pnl", "size"), snapshots=("snaps", "sum"))
    g["half"] = (1.96 * g.sd / np.sqrt(g.windows)).fillna(0)
    return g.drop(columns="sd").reset_index()


def edge_by_price(quotes, minutes=(1, LAST_ENTRY_MINUTE), bin_cents=5):
    """The same bet as the headline, run with a narrow zone at every price level:
    for each 5-cent band, the first minute a side was priced in it, one trade per window."""
    q = quotes[quotes.minute.between(*minutes)]
    q = q.assign(bucket=(np.floor(q.price * 100 / bin_cents) * bin_cents).clip(upper=100 - bin_cents))
    q = q.sort_values("price").drop_duplicates(["bucket", "ticker", "minute"])  # cheaper side if both
    q = q.sort_values("minute", kind="stable").drop_duplicates(["bucket", "ticker"])
    return by_bucket(q, "bucket")


def edge_by_minute(zone_quotes, minutes=(1, LAST_ENTRY_MINUTE)):
    """The edge if you only bought in one particular minute, whenever a side was in your zone then."""
    return by_bucket(zone_quotes[zone_quotes.minute.between(*minutes)], "minute")


def kelly_fraction(stats):
    """Full-Kelly share of the bankroll to spend per trade, from the backtest's own
    win rate and average cost. 0 means don't bet."""
    if not stats:
        return 0.0
    q, unit = stats["win_rate"], stats["breakeven"]
    return max(0.0, (q - unit) / (1 - unit))


def simulate(trades, rule, days=30, start=100.0, paths=2000, bet_frac=0.20, kelly_mult=0.25,
             flat=5.0, fee_rate=TAKER_FEE_RATE, seed=7):
    """Bankroll after each day, for `paths` simulated futures (shape: days+1 x paths).

    Each future replays random real trades from the backtest (drawn with
    replacement) at the backtest's own trades-per-day pace. Rules:
      "pct"   - spend bet_frac of the current bankroll on every trade
      "kelly" - spend kelly_mult x the Kelly fraction (0 when there's no edge). If the
                trades carry a model's own win chance (p_win), each trade is sized from that.
      "flat"  - spend a fixed dollar amount
    Contracts are whole numbers and the fee is rounded up per order, like on Kalshi."""
    price, won = trades.price.to_numpy(), trades.won.to_numpy()
    p_win = trades.p_win.to_numpy() if "p_win" in trades else None
    stats = summarize(trades)
    per_day, kelly = stats["per_day"], kelly_fraction(stats) * kelly_mult
    steps = np.round(np.arange(days + 1) * per_day).astype(int)
    rng = np.random.default_rng(seed)
    bank = np.full(paths, float(start))
    out = np.empty((days + 1, paths))
    out[0] = bank
    for d in range(1, days + 1):
        for _ in range(steps[d] - steps[d - 1]):
            pick = rng.integers(0, len(price), paths)
            p, w = price[pick], won[pick]
            unit = p + fee_rate * p * (1 - p)  # one contract plus its share of the fee
            if rule == "pct":
                stake = bank * bet_frac
            elif rule == "kelly" and p_win is not None:
                stake = bank * kelly_mult * np.clip((p_win[pick] - unit) / (1 - unit), 0, 1)
            elif rule == "kelly":
                stake = bank * kelly
            else:
                stake = np.minimum(flat, bank)
            contracts = np.floor(stake / unit + 1e-9)
            fee = np.ceil(fee_rate * contracts * p * (1 - p) * 100 - 1e-9) / 100
            cost = contracts * p + fee
            ok = (contracts > 0) & (cost <= bank + 1e-9)
            bank = np.where(ok, bank - cost + w * contracts, bank)
        out[d] = bank
    return out


def sim_stats(paths, start=100.0):
    """Wiped out = lost 95% or more. A % rule never hits exactly $0; it gets stuck
    once its share of the bankroll can't buy a single contract."""
    final = paths[-1]
    return {
        "median": float(np.median(final)),
        "p10": float(np.percentile(final, 10)),
        "p90": float(np.percentile(final, 90)),
        "below_half": float((final < start / 2).mean()),
        "above_start": float((final > start).mean()),
        "wiped": float((final < start * 0.05).mean()),
    }


def window_candles(ticker):
    with connect() as con:
        return pd.read_sql("SELECT minute, yes_bid, yes_ask FROM candles WHERE ticker = ? ORDER BY minute",
                           con, params=(ticker,))


def btc_between(t0, t1):
    """Coinbase 1-minute closes, stamped at the END of each minute to line up with Kalshi's candles."""
    with connect() as con:
        return pd.read_sql("SELECT ts + 60 AS ts, close FROM btc_1m WHERE ts + 60 BETWEEN ? AND ? ORDER BY ts",
                           con, params=(int(t0), int(t1)))
