"""Do popular chart indicators predict BTC's next move? Tested on Coinbase 1-minute BTC.

- Squeeze (TTM / LazyBear "Squeeze Momentum"): Bollinger Bands (20, 2) inside Keltner
  Channels (20, 1.5 x average true range) = "squeeze on". When it releases ("fires"),
  the momentum line's sign is the predicted direction.
- ADX (14, Wilder): when ADX > 25 (a "strong trend"), +DI vs -DI is the predicted direction.
- Baseline: the sign of the previous bars' return (plain momentum).

For each, on 5-minute, 15-minute and 1-hour bars: how often the predicted direction is
right, and the average move in that direction, in the first and second half of the data.
Also: are moves after a squeeze fires bigger than usual (the squeeze's real claim)?
Run: python indicator_test.py
"""
import sqlite3

import numpy as np
import pandas as pd

from config import DB_PATH


def load():
    con = sqlite3.connect(DB_PATH)
    b = pd.read_sql("SELECT ts, open, high, low, close FROM btc_1m ORDER BY ts", con)
    b.index = pd.to_datetime(b.ts, unit="s", utc=True)
    return b.drop(columns="ts")


def resample(b, rule):
    return b.resample(rule).agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()


def true_range(d):
    prev = d.close.shift()
    return pd.concat([d.high - d.low, (d.high - prev).abs(), (d.low - prev).abs()], axis=1).max(axis=1)


def linreg_end(y, n):
    """Value at the last point of a least-squares line over the last n points (Pine's linreg(y, n, 0))."""
    x = np.arange(n) - (n - 1) / 2
    w = x / (x ** 2).sum()
    return y.rolling(n).apply(lambda v: v.mean() + (w * v).sum() * (n - 1) / 2, raw=True)


def squeeze(d, n=20, bb=2.0, kc=1.5):
    sma, sd = d.close.rolling(n).mean(), d.close.rolling(n).std(ddof=0)
    rng = true_range(d).rolling(n).mean()
    on = (sma - bb * sd > sma - kc * rng) & (sma + bb * sd < sma + kc * rng)
    mid = ((d.high.rolling(n).max() + d.low.rolling(n).min()) / 2 + sma) / 2
    mom = linreg_end(d.close - mid, n)
    fired = on.shift(1, fill_value=False) & ~on
    return on, mom, fired


def adx(d, n=14):
    up, dn = d.high.diff(), -d.low.diff()
    pdm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), d.index)
    mdm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), d.index)
    ew = lambda s: s.ewm(alpha=1 / n, adjust=False).mean()
    atr = ew(true_range(d))
    pdi, mdi = 100 * ew(pdm) / atr, 100 * ew(mdm) / atr
    return ew(100 * (pdi - mdi).abs() / (pdi + mdi)), pdi, mdi


def score(direction, fwd, halves):
    """Hit rate and mean move (basis points) in the predicted direction, per half of the data."""
    out = []
    for name, mask in halves:
        s = (np.sign(direction) * fwd)[mask & (direction != 0) & fwd.notna()].dropna()
        if len(s) < 30:
            out.append(f"{name}: n<30")
            continue
        hit = (s > 0).mean()
        mean, se = s.mean() * 1e4, s.std() / np.sqrt(len(s)) * 1e4
        out.append(f"{name}: n={len(s):6,d} right {hit:.1%}  move {mean:+.2f}bp [{mean - 1.96 * se:+.2f}, {mean + 1.96 * se:+.2f}]")
    return " | ".join(out)


def main():
    b = load()
    mid_date = b.index[0] + (b.index[-1] - b.index[0]) / 2
    print(f"BTC 1-minute data: {b.index[0]:%Y-%m-%d} to {b.index[-1]:%Y-%m-%d}; halves split at {mid_date:%Y-%m-%d}")
    for rule, label, horizons in (("5min", "5-min bars", (3, 12)), ("15min", "15-min bars", (1, 4)),
                                  ("1h", "1-hour bars", (1, 4))):
        d = resample(b, rule)
        halves = (("1st half", d.index < mid_date), ("2nd half", d.index >= mid_date))
        on, mom, fired = squeeze(d)
        a, pdi, mdi = adx(d)
        print(f"\n=== {label} ({len(d):,} bars; squeeze on {on.mean():.0%} of the time, fired {int(fired.sum()):,} times) ===")
        for k in horizons:
            fwd = d.close.shift(-k) / d.close - 1
            mins = int(pd.Timedelta(rule).total_seconds() // 60) * k
            span = f"{mins} min" if mins < 60 else f"{mins // 60} h"
            every_k = pd.Series(np.arange(len(d)) % k == 0, d.index)  # non-overlapping samples
            print(f"  next {span}:")
            print(f"    squeeze fires, momentum sign : {score(mom.where(fired, 0), fwd, halves)}")
            print(f"    ADX>25, +DI vs -DI           : {score((pdi - mdi).where((a > 25) & every_k, 0), fwd, halves)}")
            print(f"    plain momentum (last {span}) : {score((d.close / d.close.shift(k) - 1).where(every_k, 0), fwd, halves)}")
            big_after = fwd.abs()[fired].mean() / fwd.abs().mean()
            big_during = fwd.abs()[on].mean() / fwd.abs().mean()
            print(f"    size of the next move vs normal: after a squeeze fires x{big_after:.2f}, while squeeze is on x{big_during:.2f}")


if __name__ == "__main__":
    main()
