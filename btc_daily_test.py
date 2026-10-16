"""#23 and #24: Kalshi's daily 5pm BTC markets vs outcomes, and vs Deribit's options.

Two decision times: 20 hours and 6 hours before the 5pm ET close.
  #23 Calibration: do Kalshi's prices match how often each strike actually finished in
      the money? "Sell the tails": buy the favourite side (NO on YES priced under 20c,
      YES on YES priced over 80c) at the quote, after the fee.
  #24 Deribit relative value: the options-implied probability (a volatility smile fitted
      to the Deribit trades in the 2 hours before the decision, interpolated to Kalshi's
      settlement time) vs Kalshi's quote. Buy whichever side Deribit says is cheap by more
      than 2c after the fee.
All strikes of one day settle on the same BTC price, so the 95% ranges resample whole
days. Halves split at 2026-05-12 as in the BTC 15-minute work. Run: python btc_daily_test.py
"""
import math
import sqlite3
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from btc_daily_fetch import DB
from config import DB_PATH

SPLIT = pd.Timestamp("2026-05-12", tz="UTC").timestamp()
MARGIN = 0.02
YEAR = 365.25 * 24 * 3600
fee = lambda p: 0.07 * p * (1 - p)
N = np.vectorize(lambda x: 0.5 * (1 + math.erf(x / math.sqrt(2))))
rng = np.random.default_rng(0)


def load():
    con = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    m = pd.read_sql("SELECT * FROM markets WHERE candles_done = 1", con)
    c = pd.read_sql("SELECT * FROM candles WHERE bid IS NOT NULL AND ask IS NOT NULL", con)
    d = pd.read_sql("SELECT ts, expiry, strike, iv, index_price, amount FROM deribit WHERE iv > 0", con)
    b = pd.read_sql("SELECT ts, close FROM btc_1m ORDER BY ts", sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True))
    d["exp_ts"] = [int(datetime.strptime(e, "%d%b%y").replace(hour=8, tzinfo=timezone.utc).timestamp())
                   for e in d.expiry.str.title()]
    return m, c, d, b


def smile(trades):
    """Quadratic fit of implied vol on log-moneyness, flat outside the traded range."""
    x = np.log(trades.strike / trades.index_price).to_numpy()
    if len(trades) < 6 or trades.strike.nunique() < 3:
        return None
    coef = np.polyfit(x, trades.iv.to_numpy() / 100, 2, w=np.sqrt(trades.amount.to_numpy()))
    lo, hi = x.min(), x.max()
    return lambda k: np.clip(np.polyval(coef, np.clip(k, lo, hi)), 0.1, 3.0)


def deribit_probs(d, t, close, strikes):
    """P(BTC > K at close) from trades in the 2 h before t; None if there isn't enough to fit."""
    w = d[(d.ts > t - 7200) & (d.ts <= t)]
    if w.empty:
        return None
    fwd = w.sort_values("ts").index_price.iloc[-1]
    fits = []
    for exp, g in w.groupby("exp_ts"):
        if exp > t + 1800 and exp <= close + 3 * 86400:
            f = smile(g)
            if f:
                fits.append(((exp - t) / YEAR, f))
    if not fits:
        return None
    T = (close - t) / YEAR
    before = [x for x in fits if x[0] <= T]
    after = [x for x in fits if x[0] >= T]

    def total_var(k):
        if before and after:
            (t1, f1), (t2, f2) = max(before, key=lambda x: x[0]), min(after, key=lambda x: x[0])
            if t2 > t1:
                a = (T - t1) / (t2 - t1)
                return (1 - a) * f1(k) ** 2 * t1 + a * f2(k) ** 2 * t2
            return f1(k) ** 2 * T
        t1, f1 = min(after, key=lambda x: x[0]) if after else max(before, key=lambda x: x[0])
        return f1(k) ** 2 * T  # only one side: assume a flat term structure

    def call(K):
        k = np.log(K / fwd)
        v = np.sqrt(total_var(k))
        d1 = (-k + v ** 2 / 2) / v
        return fwd * N(d1) - K * N(d1 - v)

    K = np.asarray(strikes, dtype=float)
    h = fwd * 1e-3
    return (call(K - h) - call(K + h)) / (2 * h)


def day_range(t):
    g = t.groupby("event").pnl.agg(["sum", "count"])
    idx = rng.integers(0, len(g), (2000, len(g)))
    boots = g["sum"].to_numpy()[idx].sum(1) / g["count"].to_numpy()[idx].sum(1)
    return np.percentile(boots, 2.5) * 100, np.percentile(boots, 97.5) * 100


def show(t, label):
    out = []
    for name, sub in (("1st half", t[t.close_ts < SPLIT]), ("2nd half", t[t.close_ts >= SPLIT])):
        if len(sub) < 20:
            out.append(f"{name}: {len(sub)} trades")
            continue
        lo, hi = day_range(sub)
        out.append(f"{name}: {len(sub):4,d} trades on {sub.event.nunique()} days, won {sub.won.mean():.0%} "
                   f"at {sub.price.mean():.2f}, {sub.pnl.mean() * 100:+.2f}c [{lo:+.2f}, {hi:+.2f}]")
    print(f"  {label:40s} " + " | ".join(out))


def main():
    m, c, d, b = load()
    print(f"{m.event.nunique()} days, {len(m):,} markets, {len(d):,} Deribit trades")
    c = c.sort_values("ts")
    for hours in (20, 6):
        x = m.assign(t=m.close_ts - hours * 3600)
        q = pd.merge_asof(x.sort_values("t"), c.rename(columns={"ts": "tq"}), left_on="t", right_on="tq",
                          by="ticker", direction="backward")
        q = q[(q.t - q.tq <= 7200) & (q.bid > 0) & (q.ask < 1)].copy()
        q["won_yes"] = (q.result == "yes").astype(float)
        q["mid"] = (q.bid + q.ask) / 2
        q["p_deribit"] = np.nan
        for (ev, t, close), g in q.groupby(["event", "t", "close_ts"]):
            p = deribit_probs(d, t, close, g.strike)
            if p is not None:
                q.loc[g.index, "p_deribit"] = p
        print(f"\n=== {hours} hours before the close: {len(q):,} quoted strikes on {q.event.nunique()} days ===")
        k = q.dropna(subset=["p_deribit"])
        print(f"  Brier on the {len(k):,} strikes with a Deribit fit: Kalshi mid {((k.mid - k.won_yes) ** 2).mean():.4f}, "
              f"Deribit {((k.p_deribit - k.won_yes) ** 2).mean():.4f}")
        bins = [0, 0.05, 0.10, 0.20, 0.35, 0.65, 0.80, 0.90, 0.95, 1.0]
        cal = q.groupby(pd.cut(q.mid, bins), observed=True).agg(n=("won_yes", "size"), price=("mid", "mean"),
                                                                won=("won_yes", "mean"))
        print("  calibration (Kalshi mid -> how often YES won):")
        print("    " + cal.round(3).to_string().replace("\n", "\n    "))
        # 23: sell the tails
        tails = pd.concat([
            q[q.mid < 0.20].assign(price=lambda z: 1 - z.bid, won=lambda z: 1 - z.won_yes),
            q[q.mid > 0.80].assign(price=lambda z: z.ask, won=lambda z: z.won_yes)])
        tails["pnl"] = tails.won - tails.price - fee(tails.price)
        show(tails, "#23 sell the tails (favourite side)")
        for lo, hi in ((0.02, 0.10), (0.10, 0.20)):
            sub = tails[((tails.mid >= lo) & (tails.mid < hi)) | ((tails.mid > 1 - hi) & (tails.mid <= 1 - lo))]
            show(sub, f"    tails priced {lo * 100:.0f}-{hi * 100:.0f}c")
        # 24: Deribit relative value
        e_yes = k.p_deribit - k.ask - fee(k.ask)
        e_no = (1 - k.p_deribit) - (1 - k.bid) - fee(1 - k.bid)
        rv = pd.concat([k[(e_yes >= e_no) & (e_yes > MARGIN)].assign(price=lambda z: z.ask, won=lambda z: z.won_yes),
                        k[(e_no > e_yes) & (e_no > MARGIN)].assign(price=lambda z: 1 - z.bid,
                                                                    won=lambda z: 1 - z.won_yes)])
        rv["pnl"] = rv.won - rv.price - fee(rv.price)
        show(rv, "#24 Deribit says Kalshi is off by >2c")
        print(f"    of which buying YES: {(rv.price == rv.ask).mean():.0%}; average Deribit-vs-Kalshi gap "
              f"{(np.where(rv.price == rv.ask, rv.p_deribit - rv.ask, rv.bid - rv.p_deribit)).mean() * 100:.1f}c")


if __name__ == "__main__":
    main()
