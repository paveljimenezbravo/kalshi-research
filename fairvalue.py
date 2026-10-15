"""Fair-value model: what was each YES contract really worth, minute by minute?

At every minute of every window, take BTC's price (Coinbase), the window's starting
level, the time left and how jumpy BTC has been lately, and work out the chance BTC
finishes at or above the target. Then buy whichever side Kalshi priced too cheaply
(by more than a margin, after the fee) and see how that went.

Two models:
  normal - the textbook bell-curve answer (what btc_mc.py prints as "lognormal")
  replay - btc_mc.py's Monte Carlo: replay real past BTC moves, rescaled to the
           current volatility. It's computed over every past stretch instead of
           random draws, which gives the same answer without simulation noise.
           Walk-forward: each month is priced using only history from before it.

    python fairvalue.py      # prints the report
"""
import math

import numpy as np
import pandas as pd

import backtest as bt
from config import TAKER_FEE_RATE

HALF_LIFE = 60      # minutes: how long the volatility estimate remembers (btc_mc.py's default)
AVERAGING = 2 / 3   # the result is a 60-second average, which is as uncertain as settling 40 s early
HORIZONS = 14       # longest stretch the replay needs, in whole minutes
BANDS = (0, 1, 2, 3, 5, 8, 12, 20, 100)  # expected-edge bands for the "did it pay?" check, in cents
_erf = np.vectorize(math.erf)


def normal_cdf(x):
    return 0.5 * (1 + _erf(np.asarray(x, dtype=float) / math.sqrt(2)))


def btc_minutes(half_life=HALF_LIFE):
    """Coinbase BTC on a complete 1-minute grid (gaps carry the last price), with a
    per-minute volatility estimate that only uses the past. Indexed by minute start."""
    with bt.connect() as con:
        df = pd.read_sql("SELECT ts, open, high, low, close FROM btc_1m ORDER BY ts", con)
    grid = np.arange(df.ts.iloc[0], df.ts.iloc[-1] + 60, 60)
    df = df.set_index("ts").reindex(grid)
    df["close"] = df.close.ffill()
    for col in ("open", "high", "low"):
        df[col] = df[col].fillna(df.close)
    df["avg"] = (df.open + df.high + df.low + df.close) / 4  # stand-in for the minute's 60-second average
    df["logp"] = np.log(df.close)
    ret = df.logp.diff().fillna(0)
    df["vol"] = np.sqrt((ret ** 2).ewm(halflife=half_life, adjust=False).mean()).clip(lower=1e-5)
    return df


def snapshot_inputs(markets, snap, btc):
    """Model inputs for each window-minute, as known at the end of that minute."""
    t0 = int(btc.index[0])
    s = snap.merge(markets[["ticker", "open_ts"]], on="ticker").sort_values(["ticker", "minute"])
    now = s.open_ts + 60 * s.minute                    # end of this minute
    i = (now - 60 - t0) // 60                          # the Coinbase minute that ends now
    j = (s.open_ts - 60 - t0) // 60                    # the minute before the window opened
    ok = (j >= 0) & (i < len(btc))
    s, now, i, j = s[ok].copy(), now[ok], i[ok].to_numpy(), j[ok].to_numpy()
    s["spot"] = btc.close.to_numpy()[i]
    # The target is the 60-second average before the open. Measuring it with Coinbase too
    # (rather than Kalshi's BRTI number) cancels the small gap between the two price feeds.
    s["start"] = btc.avg.to_numpy()[j]
    s["vol"] = btc.vol.to_numpy()[i]
    s["tau"] = (s.close_ts - now) / 60 - AVERAGING     # minutes of uncertainty left
    s["move"] = np.log(s.spot / s.start)               # how far BTC is from the start
    return s.reset_index(drop=True)


def p_normal(s):
    return normal_cdf(s.move / (s.vol * np.sqrt(s.tau)))


def p_replay(s, btc, warmup=3 * HALF_LIFE, min_days=7):
    """btc_mc.py's replay, month by month, using only BTC history from before each month."""
    logp, vol, t0 = btc.logp.to_numpy(), btc.vol.to_numpy(), int(btc.index[0])
    month = pd.to_datetime(s.open_ts, unit="s").dt.to_period("M")
    out = np.full(len(s), np.nan)
    for m in sorted(month.unique()):
        last = (int(m.start_time.timestamp()) - t0) // 60  # rows that finished before the month
        if last - warmup < min_days * 1440:
            continue
        mu = np.diff(logp[:last])[warmup:].mean()       # assume no trend, like btc_mc.py
        tables = {}
        for h in range(1, HORIZONS + 1):
            rows = np.arange(warmup, last - h)
            tables[h] = np.sort((logp[rows + h] - logp[rows] - h * mu) / vol[rows])
        mask = (month == m).to_numpy()
        sub = s[mask]
        whole = np.ceil(sub.tau.to_numpy()).astype(int).clip(1, HORIZONS)
        x = (-sub.move / (sub.vol * np.sqrt(sub.tau / whole))).to_numpy()
        p = np.empty(len(sub))
        for h in np.unique(whole):
            k = whole == h
            p[k] = 1 - np.searchsorted(tables[h], x[k], side="left") / len(tables[h])
        out[mask] = p
    return out


def model_table(half_life=HALF_LIFE):
    """Every window-minute with Kalshi's quotes (now and one minute later) and both models."""
    markets, candles = bt.load()
    btc = btc_minutes(half_life)
    s = snapshot_inputs(markets, bt.snapshots(markets, candles), btc)
    s["p_normal"] = p_normal(s)
    s["p_replay"] = p_replay(s, btc)
    nxt = s.groupby("ticker")[["minute", "yes_bid", "yes_ask"]].shift(-1)
    same = nxt.minute == s.minute + 1
    s["next_bid"], s["next_ask"] = nxt.yes_bid.where(same), nxt.yes_ask.where(same)
    return markets, s, btc


def coinbase_agreement(markets, btc):
    """How often Coinbase alone (minute averages) reproduces Kalshi's official result."""
    t0, avg = int(btc.index[0]), btc.avg.to_numpy()
    j, k = (markets.open_ts - 60 - t0) // 60, (markets.close_ts - 60 - t0) // 60
    ok = ((j >= 0) & (k < len(btc))).to_numpy()
    up = avg[k[ok]] >= avg[j[ok]]
    return float((up == markets.yes_won.to_numpy()[ok].astype(bool)).mean())


def accuracy(s, model, minutes=(1, bt.LAST_ENTRY_MINUTE)):
    """Brier score (average squared miss; lower is better) for Kalshi's mid price and the model."""
    p = s[f"p_{model}"]
    ok = p.notna() & (s.yes_bid > 0) & (s.yes_ask < 1) & s.minute.between(*minutes)
    y, mid = s.yes_won[ok], (s.yes_bid[ok] + s.yes_ask[ok]) / 2
    return {"market": float(((mid - y) ** 2).mean()), "model": float(((p[ok] - y) ** 2).mean()), "n": int(ok.sum())}


def candidates(s, model="replay", delay=0, minutes=(1, bt.LAST_ENTRY_MINUTE), max_spread=0.03,
               fee_rate=TAKER_FEE_RATE):
    """Every window-minute with the side the model likes better and its expected edge after the fee.
    delay=1 decides at this minute but pays the NEXT minute's price, to check the edge isn't just
    a few seconds of staleness in the minute snapshots."""
    p = s[f"p_{model}"]
    bid, ask = (s.next_bid, s.next_ask) if delay else (s.yes_bid, s.yes_ask)
    ok = (p.notna() & bid.notna() & ask.notna() & (bid > 0) & (ask < 1)
          & (ask - bid <= max_spread + 1e-9) & s.minute.between(*minutes))
    c = s[ok]
    p, yes_cost, no_cost = p[ok], ask[ok].round(4), (1 - bid[ok]).round(4)
    e_yes = p - yes_cost - fee_rate * yes_cost * (1 - yes_cost)
    e_no = (1 - p) - no_cost - fee_rate * no_cost * (1 - no_cost)
    buy_yes = (e_yes >= e_no).to_numpy()
    out = pd.DataFrame({
        "ticker": c.ticker, "minute": c.minute, "close_ts": c.close_ts,
        "side": np.where(buy_yes, "YES", "NO"),
        "price": np.where(buy_yes, yes_cost, no_cost),
        "p_win": np.where(buy_yes, p, 1 - p),           # the model's chance this side wins
        "pred_edge": np.where(buy_yes, e_yes, e_no),    # what the model expects to make, after fee
        "won": np.where(buy_yes, c.yes_won, 1 - c.yes_won),
    })
    out["fee"] = fee_rate * out.price * (1 - out.price)
    out["pnl"] = out.won - out.price - out.fee
    return out


def model_trades(cands, min_edge=0.03):
    """One trade per window: the first minute the model's expected edge reaches `min_edge`."""
    t = cands[cands.pred_edge >= min_edge]
    return (t.sort_values(["ticker", "minute"]).drop_duplicates("ticker")
            .sort_values("close_ts").reset_index(drop=True))


def by_predicted_edge(cands, bins=BANDS):
    """Realized edge vs the edge the model expected, first such minute per window per band."""
    c = cands[cands.pred_edge >= 0]
    c = c.assign(band=pd.cut(c.pred_edge * 100, bins, right=False, labels=bins[:-1]).astype(float))
    c = c.dropna(subset=["band"]).sort_values("minute", kind="stable").drop_duplicates(["band", "ticker"])
    out = bt.by_bucket(c, "band")
    out["pred"] = c.groupby("band").pred_edge.mean().to_numpy()
    return out


def by_month(trades):
    """Trades grouped by month, in the same columns as backtest.by_bucket."""
    g = trades.groupby(pd.to_datetime(trades.close_ts, unit="s").dt.to_period("M"))
    out = pd.DataFrame({"price": g.price.mean(), "win": g.won.mean(), "edge": g.pnl.mean(),
                        "half": (1.96 * g.pnl.std() / np.sqrt(g.size())).fillna(0), "windows": g.size()})
    out.index = out.index.strftime("%b %Y")
    return out.rename_axis("month").reset_index()


def report():
    markets, s, btc = model_table()
    print(f"{len(markets):,} windows, {len(s):,} window-minutes with Coinbase data")
    print(f"Coinbase alone reproduces Kalshi's result in {coinbase_agreement(markets, btc):.1%} of windows")
    for model in ("normal", "replay"):
        a = accuracy(s, model)
        print(f"Brier ({model}): market {a['market']:.4f} vs model {a['model']:.4f} over {a['n']:,} snapshots")
    for model in ("normal", "replay"):
        for delay in (0, 1):
            c = candidates(s, model, delay)
            for min_edge in (0.01, 0.02, 0.03, 0.05, 0.08):
                t = model_trades(c, min_edge)
                st = bt.summarize(t)
                if not st:
                    continue
                older, newer = bt.split_check(t)
                print(f"{model:6} delay {delay} min edge {min_edge * 100:.0f}c: {st['n']:6,} trades "
                      f"win {st['win_rate']:.3f} need {st['breakeven']:.3f} predicted {t.pred_edge.mean() * 100:+.2f}c "
                      f"got {st['edge'] * 100:+.2f}c [{st['edge_lo'] * 100:+.2f},{st['edge_hi'] * 100:+.2f}] "
                      f"older {older['edge'] * 100:+.2f} newer {newer['edge'] * 100:+.2f}")
    c = candidates(s, "replay", 0)
    print(by_predicted_edge(c).round(4).to_string(index=False))
    print(by_month(model_trades(c, 0.03)).round(4).to_string(index=False))


if __name__ == "__main__":
    report()
