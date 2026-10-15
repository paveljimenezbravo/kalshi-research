"""Edge hunting on the downloaded history: tests that go beyond buying at the ask.

Maker test: instead of paying the ask, rest a bid at the best bid at the end of a minute
and let someone sell to you during the next minute. You skip the bid-ask spread and most
of the fee, but you tend to get filled when the price is moving against you. This
measures which effect wins.

    python edge.py

The live recorder's data gets its own, more precise tests in live_edge.py.
"""
import numpy as np
import pandas as pd

import backtest as bt

# Kalshi's maker formula where makers pay: 0.0175 x contracts x P x (1 - P), rounded up.
# Many Kalshi markets charge makers nothing, so the report shows both.
MAKER_FEE_RATE = 0.0175


def load_extremes():
    """Windows plus each minute's closing quotes and how far the quotes moved during it."""
    with bt.connect() as con:
        markets = pd.read_sql("SELECT ticker, close_ts, result FROM markets WHERE candles_done = 1", con)
        candles = pd.read_sql("SELECT ticker, minute, yes_bid, yes_ask, yes_bid_low, yes_ask_high FROM candles", con)
    markets["yes_won"] = (markets.result == "yes").astype(int)
    return markets, candles


def maker_fills(markets, candles, max_spread=0.03, fee_rate=MAKER_FEE_RATE):
    """Bids rested at the best price at the end of each minute (1-14), cancelled a minute later.

    A YES bid counts as filled only if the best YES bid fell BELOW it during that next minute.
    With your order resting there, the price couldn't drop through it without filling it (and
    if everyone else pulled their bids instead, the next seller would have hit yours). Fills
    where you simply wait your turn in the queue aren't counted. Those are the gentler fills,
    so this test is pessimistic. A NO bid works the same way through the YES ask rising.
    Returns the fills and how many bids were posted."""
    c = candles.sort_values(["ticker", "minute"]).reset_index(drop=True)
    nxt = c.groupby("ticker")[["minute", "yes_bid_low", "yes_ask_high"]].shift(-1)
    ok = ((nxt.minute == c.minute + 1) & (c.minute <= bt.LAST_ENTRY_MINUTE) & (c.yes_bid > 0)
          & (c.yes_ask < 1) & (c.yes_ask - c.yes_bid <= max_spread + 1e-9))
    s = c[ok].assign(next_bid_low=nxt.yes_bid_low[ok], next_ask_high=nxt.yes_ask_high[ok])
    s = s.merge(markets[["ticker", "close_ts", "yes_won"]], on="ticker")
    yes = s[s.next_bid_low < s.yes_bid - 1e-9]
    no = s[s.next_ask_high > s.yes_ask + 1e-9]
    fills = pd.concat([
        pd.DataFrame({"ticker": yes.ticker, "minute": yes.minute, "close_ts": yes.close_ts, "side": "YES",
                      "price": yes.yes_bid.round(4), "won": yes.yes_won}),
        pd.DataFrame({"ticker": no.ticker, "minute": no.minute, "close_ts": no.close_ts, "side": "NO",
                      "price": (1 - no.yes_ask).round(4), "won": 1 - no.yes_won}),
    ], ignore_index=True)
    fills["fee"] = fee_rate * fills.price * (1 - fills.price)
    fills["pnl"] = fills.won - fills.price - fills.fee
    return fills, 2 * int(ok.sum())


def zone_trades(fills, lo, hi):
    """One fill per window: the first minute a bid priced lo..hi got filled."""
    return bt.first_trades(bt.in_zone(fills, lo, hi))


ZONES = ((0.05, 0.30), (0.30, 0.50), (0.50, 0.70), (0.70, 0.95), (0.80, 0.97))


def describe(trades):
    s = bt.summarize(trades)
    if not s:
        return "no trades"
    older, newer = bt.split_check(trades)
    return (f"{s['edge'] * 100:+5.2f}c [{s['edge_lo'] * 100:+5.2f},{s['edge_hi'] * 100:+5.2f}] "
            f"old {older['edge'] * 100:+5.2f} new {newer['edge'] * 100:+5.2f} (n={s['n']:,})")


def report():
    """Per price zone, one trade per window: buying at the ask (taker) vs bidding and waiting (maker)."""
    markets, candles = load_extremes()
    m2, c2 = bt.load()
    taker = bt.side_quotes(bt.snapshots(m2, c2))
    makers = {rate: maker_fills(markets, candles, fee_rate=rate) for rate in (0.0, MAKER_FEE_RATE)}
    fills, posted = makers[0.0]
    print(f"{bt.SERIES}: {len(markets):,} windows, YES won {markets.yes_won.mean():.3f}, "
          f"maker bids filled {len(fills) / posted:.1%}")
    for lo, hi in ZONES:
        print(f"  zone {lo * 100:2.0f}-{hi * 100:2.0f}c  taker {describe(bt.first_trades(bt.in_zone(taker, lo, hi)))}")
        for rate, (f, _) in makers.items():
            print(f"               maker fee {rate:<6} {describe(zone_trades(f, lo, hi))}")


if __name__ == "__main__":
    report()
