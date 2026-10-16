"""#20: fade short-term trend signals, tested on markets #19 never saw.

The rules are exactly #19's (indicator_combo.rules), computed from each coin's own
Coinbase prices, decided at the end of minute 1, and traded the OPPOSITE way at
Kalshi's ask after the fee. Pass (written before running): pooled over ETH, SOL, XRP
and DOGE, more than +0.5c per contract with a 95% range above zero, at minute 1's
price and at the next minute's price. Windows at the same time on different coins
move together, so the range resamples whole time slots, not single trades.

    python trend_fade_test.py        # runs every coin, then pools them
"""
import os
import subprocess
import sys

import numpy as np
import pandas as pd

COINS = ("KXETH15M", "KXSOL15M", "KXXRP15M", "KXDOGE15M")
FADED = ("MACD + EMA9/21 + ADX>25 trend (5m)", "Donchian breakout + volume (15m)")


def one_coin():
    import indicator_combo as ic
    from config import HOME, SERIES
    meta, X = ic.dataset()
    m1 = (meta.minute == 1).to_numpy()
    mt, Xm = meta[m1].reset_index(drop=True), X[m1].reset_index(drop=True)
    out = []
    for name, side in ic.rules(Xm).items():
        for label, s in (("as #19", side), ("faded", -side)):
            if label == "faded" and name not in FADED:
                continue
            for delay in (False, True):
                t = ic.trade(mt, s, delay=delay)
                out.append(t.assign(series=SERIES, rule=name, way=label, price_at="next minute" if delay else "minute 1"))
    pd.concat(out).to_csv(HOME / "logs" / f"trend_fade_{SERIES}.csv", index=False)
    print(f"{SERIES}: {mt.ticker.nunique():,} windows", flush=True)


def pooled_range(t, rng, n=2000):
    g = t.groupby("open_ts").pnl.agg(["sum", "count"])
    s, c = g["sum"].to_numpy(), g["count"].to_numpy()
    idx = rng.integers(0, len(g), (n, len(g)))
    boots = s[idx].sum(1) / c[idx].sum(1)
    return np.percentile(boots, 2.5) * 100, np.percentile(boots, 97.5) * 100


def main():
    from config import HOME
    for coin in COINS:
        subprocess.run([sys.executable, __file__, "one"], env={**os.environ, "KALSHI_SERIES": coin}, check=True)
    t = pd.concat(pd.read_csv(HOME / "logs" / f"trend_fade_{c}.csv") for c in COINS)
    rng = np.random.default_rng(0)
    for (rule, way, price_at), g in t.groupby(["rule", "way", "price_at"], sort=False):
        lo, hi = pooled_range(g, rng)
        per = ", ".join(f"{c[2:-3]} {x.pnl.mean() * 100:+.1f}c (n={len(x)})" for c, x in g.groupby("series", sort=False))
        verdict = "PASS" if way == "faded" and g.pnl.mean() > 0.005 and lo > 0 else ""
        print(f"{rule[:38]:38s} {way:6s} {price_at:11s} n={len(g):5,d} won {g.won.mean():.1%} at {g.price.mean():.3f} "
              f"-> {g.pnl.mean() * 100:+.2f}c [{lo:+.2f}, {hi:+.2f}] {verdict}\n    {per}")


if __name__ == "__main__":
    one_coin() if sys.argv[1:] == ["one"] else main()
