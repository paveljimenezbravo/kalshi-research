"""#22: same-day weather. Does the day's observed high-so-far, plus how much a day like
this usually still warms up, beat Kalshi's intraday prices for the daily-high brackets?

At 11am, 1pm, 3pm, 5pm and 7pm local standard time, for each city:
  m   = highest temperature observed so far today (METAR, weather_obs.py)
  gap = the forecast high (issued the day before) minus m: how much warming is "left"
The model looks up the same city's earlier days at the same hour with a similar gap and
takes their actual rise after that hour (settlement high minus m). That gives a
probability for every bracket. Only days at least 2 days earlier are used.

A bracket is "bought" (YES at the ask or NO at the bid) when the model's edge after the
fee beats the margin, at most once per bracket per day (the first hour it qualifies).
Out of sample = from 2026-04-01. Also re-priced at the NEXT hour's quote, in case the
hourly snapshot is a little stale. Run: python weather_intraday.py
"""
import sqlite3
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd

from weather_fetch import DB
from weather_obs import STATIONS

HOURS = (11, 13, 15, 17, 19)
MARGINS = (0.03, 0.05, 0.10)
PRIMARY = 0.05
SPLIT = "2026-04-01"
NEIGHBORS, MIN_POOL, MAX_SPREAD = 150, 40, 0.06
fee = lambda p: 0.07 * p * (1 - p)


def load():
    con = sqlite3.connect(DB)
    m = pd.read_sql("SELECT ticker, series, day, strike_type, floor, cap, result, high FROM markets "
                    "WHERE candles_done = 1 AND high IS NOT NULL", con)
    c = pd.read_sql("SELECT ticker, ts, bid, ask FROM candles WHERE bid IS NOT NULL AND ask IS NOT NULL", con)
    s = pd.read_sql("SELECT * FROM so_far", con)
    f = pd.read_sql("SELECT series, day, AVG(high) fc FROM forecasts WHERE lead = 1 GROUP BY series, day", con)
    return m, c, s, f


def in_bracket(h, row):
    """Settlement highs are whole degrees. between 84-85 = {84, 85}; greater 85 = 86+; less 78 = 77-."""
    if row.strike_type == "between":
        return (h >= row.floor) & (h <= row.cap)
    if row.strike_type == "greater":
        return h > row.floor
    return h < row.cap


def decision_ts(series, day, hour):
    offset = STATIONS[series][1]
    return int((datetime.fromisoformat(day).replace(tzinfo=timezone.utc) + timedelta(hours=hour - offset)).timestamp())


def main():
    m, c, s, f = load()
    days = m.groupby(["series", "day"]).high.first().rename("H").reset_index()
    x = s[s.hour.isin(HOURS)].merge(days, on=["series", "day"]).merge(f, on=["series", "day"])
    x["gap"] = x.fc - x.max_f
    x["rise"] = x.H - x.max_f
    end = x[x.hour == 19]
    print(f"{x.groupby(['series', 'day']).ngroups:,} city-days with observations, forecasts and a settled high")
    print(f"settlement high minus METAR high-so-far at 7pm: median {end.rise.median():+.2f}F, "
          f"|diff| <= 1F on {(end.rise.abs() <= 1).mean():.0%} of days")

    # model probabilities, walk-forward
    rows = []
    for (series, hour), g in x.groupby(["series", "hour"]):
        g = g.sort_values("day").reset_index(drop=True)
        dd = pd.to_datetime(g.day)
        for i, r in g.iterrows():
            past = g[(dd <= dd[i] - pd.Timedelta(days=2)) & (dd > dd[i] - pd.Timedelta(days=730))]
            if len(past) < MIN_POOL:
                continue
            near = past.iloc[np.argsort((past.gap - r.gap).abs().to_numpy())[:NEIGHBORS]]
            rows.append((series, r.day, hour, r.max_f, r.gap, near.rise.to_numpy()))
    model = pd.DataFrame(rows, columns=["series", "day", "hour", "max_f", "gap", "rises"])

    # every bracket at every decision hour with Kalshi's quote at that hour and an hour later
    mk = m.merge(model, on=["series", "day"])
    mk["t"] = [decision_ts(se, d, h) for se, d, h in zip(mk.series, mk.day, mk.hour)]
    c = c.sort_values("ts")
    for name, lag in (("now", 0), ("later", 3600)):
        q = pd.merge_asof(mk[["ticker", "t"]].assign(tq=mk.t + lag).sort_values("tq"),
                          c.rename(columns={"ts": "tq_c"}), left_on="tq", right_on="tq_c", by="ticker",
                          direction="backward")
        q = q[(q.tq - q.tq_c) <= 3600].rename(columns={"bid": f"bid_{name}", "ask": f"ask_{name}"})
        mk = mk.merge(q[["ticker", "t", f"bid_{name}", f"ask_{name}"]], on=["ticker", "t"], how="left")
    mk = mk.dropna(subset=["bid_now", "ask_now"])
    mk = mk[(mk.bid_now > 0) & (mk.ask_now < 1) & (mk.ask_now - mk.bid_now <= MAX_SPREAD)].reset_index(drop=True)
    mk["p"] = [(in_bracket(np.round(r.max_f + r.rises), r).sum() + 0.5) / (len(r.rises) + 1) for r in mk.itertuples()]
    mk["won_yes"] = (mk.result == "yes").astype(float)
    mk["mid"] = (mk.bid_now + mk.ask_now) / 2
    print(f"{len(mk):,} quoted bracket-hours on {mk.groupby(['series', 'day']).ngroups:,} city-days")

    for name, sub in (("in sample", mk[mk.day < SPLIT]), ("out of sample", mk[mk.day >= SPLIT])):
        for h in HOURS:
            k = sub[sub.hour == h]
            if len(k):
                print(f"  {name:13s} {h:2d}:00  Brier model {((k.p - k.won_yes) ** 2).mean():.4f} "
                      f"vs Kalshi {((k.mid - k.won_yes) ** 2).mean():.4f}  ({len(k):,} brackets)")

    rng = np.random.default_rng(0)
    for margin in MARGINS:
        e_yes = mk.p - mk.ask_now - fee(mk.ask_now)
        e_no = (1 - mk.p) - (1 - mk.bid_now) - fee(1 - mk.bid_now)
        side = np.where((e_yes >= e_no) & (e_yes > margin), 1, np.where((e_no > e_yes) & (e_no > margin), -1, 0))
        t = mk[side != 0].assign(side=side[side != 0]).sort_values("hour").drop_duplicates(["ticker"])
        for when in ("now", "later"):
            bid, ask = t[f"bid_{when}"], t[f"ask_{when}"]
            price = np.where(t.side > 0, ask, 1 - bid)
            won = np.where(t.side > 0, t.won_yes, 1 - t.won_yes)
            tt = t.assign(price=price, won=won, pnl=won - price - fee(price)).dropna(subset=["pnl"])
            for name, sub in (("in sample", tt[tt.day < SPLIT]), ("out of sample", tt[tt.day >= SPLIT])):
                if len(sub) < 20:
                    print(f"  margin {margin * 100:.0f}c {when:5s} {name}: {len(sub)} trades")
                    continue
                g = sub.groupby(["series", "day"]).pnl.agg(["sum", "count"])
                idx = rng.integers(0, len(g), (2000, len(g)))
                boots = g["sum"].to_numpy()[idx].sum(1) / g["count"].to_numpy()[idx].sum(1)
                star = " <- pre-registered" if margin == PRIMARY and when == "now" else ""
                print(f"  margin {margin * 100:.0f}c {when:5s} {name:13s}: {len(sub):5,d} trades "
                      f"(YES {(sub.side > 0).mean():.0%}), won {sub.won.mean():.1%} at {sub.price.mean():.3f}, "
                      f"{sub.pnl.mean() * 100:+.2f}c [{np.percentile(boots, 2.5) * 100:+.2f}, "
                      f"{np.percentile(boots, 97.5) * 100:+.2f}]{star}")
            if margin == PRIMARY and when == "now":
                oos = tt[tt.day >= SPLIT]
                print("    out of sample by hour: " + ", ".join(
                    f"{h}:00 {g.pnl.mean() * 100:+.1f}c (n={len(g)})" for h, g in oos.groupby("hour")))
                print("    out of sample by city: " + ", ".join(
                    f"{se[6:]} {g.pnl.mean() * 100:+.1f}c (n={len(g)})" for se, g in oos.groupby("series")))


if __name__ == "__main__":
    main()
