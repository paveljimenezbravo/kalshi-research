"""Out-of-sample tests for the two open leads, written down BEFORE looking at the results.

Both leads were found on BTC windows from about May 12 to Oct 13, 2026. These tests use
only windows that closed before May 10, 2026, which the leads have never seen.

  #8  The model (replay, buy when it expects >= 3c after the fee) on weeknights 0-8 ET.
      In sample: +0.65c [-0.96, +2.27]. Passes if positive here too.
  #9  YES overbetting: YES takers lose more than NO takers in every price zone, and a maker
      buying NO does better than one buying YES. In sample: YES worse by ~1.2c in all 4 zones.
      Passes if YES is worse in at least 3 of 4 zones here too.

    python oos_tests.py
"""
from datetime import datetime, timezone

import numpy as np
import pandas as pd

import backtest as bt
import edge
import fairvalue as fv

CUTOFF = datetime(2026, 5, 10, tzinfo=timezone.utc).timestamp()
ZONES = ((0.05, 0.30), (0.30, 0.50), (0.50, 0.70), (0.70, 0.95))


def seg(close_ts):
    d = pd.to_datetime(close_ts, unit="s", utc=True).dt.tz_convert("America/New_York")
    return np.select([d.dt.dayofweek >= 5, d.dt.hour.between(0, 7), d.dt.hour.between(8, 15)],
                     ["weekend", "weeknight 0-8 ET", "weekday 8-16 ET"], "weekday 16-24 ET")


def line(s):
    return f"{s['edge'] * 100:+.2f}c [{s['edge_lo'] * 100:+.2f}, {s['edge_hi'] * 100:+.2f}] n={s['n']:,}" if s else "no trades"


def main():
    markets, s, _ = fv.model_table()
    old = markets[markets.close_ts < CUTOFF]
    print(f"Out-of-sample windows: {len(old):,} ({datetime.fromtimestamp(old.close_ts.min()):%b %d} to "
          f"{datetime.fromtimestamp(old.close_ts.max()):%b %d %Y})")

    print("\n#8 model >= 3c by time of week (out of sample)")
    t = fv.model_trades(fv.candidates(s[s.close_ts < CUTOFF], "replay", 0), 0.03)
    for g, x in t.groupby(seg(t.close_ts)):
        print(f"  {g:18} {line(bt.summarize(x))}")

    print("\n#9 YES overbetting (out of sample): taker YES | taker NO | maker buys YES | maker buys NO")
    m2, c2 = bt.load()
    snap = bt.snapshots(m2, c2)
    snap = snap[snap.close_ts < CUTOFF]
    mk, cd = edge.load_extremes()
    fills, _ = edge.maker_fills(mk[mk.close_ts < CUTOFF], cd, fee_rate=0.0)
    worse = 0
    for lo, hi in ZONES:
        ty = bt.summarize(bt.first_trades(bt.in_zone(bt.side_quotes(snap, "yes"), lo, hi)))
        tn = bt.summarize(bt.first_trades(bt.in_zone(bt.side_quotes(snap, "no"), lo, hi)))
        my = bt.summarize(edge.zone_trades(fills[fills.side == "YES"], lo, hi))
        mn = bt.summarize(edge.zone_trades(fills[fills.side == "NO"], lo, hi))
        worse += ty["edge"] < tn["edge"]
        print(f"  zone {lo * 100:2.0f}-{hi * 100:2.0f}c  {ty['edge'] * 100:+.2f} | {tn['edge'] * 100:+.2f} | "
              f"{line(my)} | {line(mn)}")
    print(f"  YES takers did worse than NO takers in {worse} of 4 zones (passes at 3+)")


if __name__ == "__main__":
    main()
