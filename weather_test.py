"""Does a forecast-based probability beat Kalshi's daily high-temperature prices?

For every settled bracket: the forecast high (average of NBM, GFS and ECMWF, as issued
before the decision time) plus a walk-forward error model (each city's forecast errors
over the previous 90 days, ending two days before) gives a probability. If that
probability beats Kalshi's ask (or bid, for NO) by more than a margin after the taker
fee, we "buy" one contract and settle it on the official result.

Two decision times, both using only forecasts that were already published:
  A. 12:00 UTC on the day (7-8am Eastern), forecast issued about 1 day earlier
  B. 18:00 UTC the day before, forecast issued about 2 days earlier

In sample = before 2026-04-01; out of sample = from 2026-04-01 on. Run: python weather_test.py
"""
import math
import sqlite3
from datetime import date, datetime, time as dtime, timedelta, timezone

import numpy as np
import pandas as pd

from config import HOME

DB = HOME / "data" / "weather.sqlite"
SPLIT = "2026-04-01"
WINDOW, MIN_DAYS = 90, 30
MAX_SPREAD = 0.06
MARGINS = (0.03, 0.05, 0.10)
FEE = lambda p: 0.07 * p * (1 - p)  # taker fee per contract (Kalshi rounds the order total up to the cent)
rng = np.random.default_rng(0)


def ncdf(x, mu, sd):
    return 0.5 * (1 + math.erf((x - mu) / (sd * math.sqrt(2)))) if np.isfinite(x) else float(x > 0)


def load():
    con = sqlite3.connect(DB)
    m = pd.read_sql("SELECT * FROM markets WHERE candles_done = 1 AND high IS NOT NULL", con)
    c = pd.read_sql("SELECT * FROM candles WHERE bid IS NOT NULL AND ask IS NOT NULL", con)
    f = pd.read_sql("SELECT * FROM forecasts", con)
    return m, c, f


def bracket_prob(row, mu, sd):
    """Highs are whole degrees: 'between 84 and 85' is [83.5, 85.5); 'greater 85' is >= 85.5; 'less 78' is < 77.5."""
    lo = -np.inf if row.strike_type == "less" else (row.floor - 0.5 if row.strike_type == "between" else row.floor + 0.5)
    hi = np.inf if row.strike_type == "greater" else (row.cap + 0.5 if row.strike_type == "between" else row.cap - 0.5)
    return ncdf(hi, mu, sd) - ncdf(lo, mu, sd)


def error_model(days):
    """Per city and day: mean and sd of (actual - forecast) over the WINDOW days ending 2 days before."""
    out = {}
    for series, g in days.groupby("series"):
        g = g.set_index("day").sort_index()
        for d in g.index:
            past = g.loc[(g.index <= (date.fromisoformat(d) - timedelta(days=2)).isoformat())
                         & (g.index > (date.fromisoformat(d) - timedelta(days=WINDOW + 2)).isoformat()), "err"].dropna()
            if len(past) >= MIN_DAYS:
                out[(series, d)] = (past.mean(), max(past.std(), 1.0))
    return out


def quotes_at(c, markets):
    """Each market's last hourly quote at or before its decision time (within 3 hours)."""
    c = c.sort_values("ts")
    q = pd.merge_asof(markets[["ticker", "t_dec"]].sort_values("t_dec"), c.rename(columns={"ts": "t_q"}),
                      left_on="t_dec", right_on="t_q", by="ticker", direction="backward")
    return q[(q.t_dec - q.t_q) <= 3 * 3600]


def run(m, c, f, lead, when_label, dec_fn):
    fc = f[f.lead == lead].groupby(["series", "day"]).high.mean().rename("fc").reset_index()
    days = m.groupby(["series", "day"]).high.first().reset_index().merge(fc, on=["series", "day"])
    days["err"] = days.high - days.fc
    em = error_model(days)
    mk = m.merge(fc, on=["series", "day"])
    mk = mk[[(s, d) in em for s, d in zip(mk.series, mk.day)]].copy()
    mk["mu"] = [mk_fc + em[(s, d)][0] for s, d, mk_fc in zip(mk.series, mk.day, mk.fc)]
    mk["sd"] = [em[(s, d)][1] for s, d in zip(mk.series, mk.day)]
    mk["p"] = [bracket_prob(r, r.mu, r.sd) for r in mk.itertuples()]
    mk["t_dec"] = [dec_fn(d) for d in mk.day]
    q = quotes_at(c, mk)
    mk = mk.merge(q[["ticker", "bid", "ask"]], on="ticker")
    mk = mk[(mk.bid > 0) & (mk.ask < 1) & (mk.ask - mk.bid <= MAX_SPREAD)]
    mk["won_yes"] = (mk.result == "yes").astype(float)
    mk["mid"] = (mk.bid + mk.ask) / 2

    print(f"\n=== {when_label} (forecast issued ~{lead} day(s) earlier) ===")
    print(f"{len(mk):,} quoted brackets on {mk.groupby(['series', 'day']).ngroups:,} city-days, "
          f"{mk.day.min()} to {mk.day.max()}")
    for name, sub in (("in sample", mk[mk.day < SPLIT]), ("out of sample", mk[mk.day >= SPLIT])):
        if sub.empty:
            continue
        brier_model = ((sub.p - sub.won_yes) ** 2).mean()
        brier_mkt = ((sub.mid - sub.won_yes) ** 2).mean()
        print(f"  {name}: accuracy (Brier, lower is better) model {brier_model:.4f} vs Kalshi mid {brier_mkt:.4f}")
    rows = []
    for margin in MARGINS:
        yes = mk[mk.p - mk.ask - FEE(mk.ask) > margin].assign(side="YES", px=lambda d: d.ask,
                                                               pnl=lambda d: d.won_yes - d.ask - FEE(d.ask))
        no = mk[(1 - mk.p) - (1 - mk.bid) - FEE(1 - mk.bid) > margin].assign(
            side="NO", px=lambda d: 1 - d.bid, pnl=lambda d: (1 - d.won_yes) - (1 - d.bid) - FEE(1 - d.bid))
        t = pd.concat([yes, no])
        for name, sub in (("in sample", t[t.day < SPLIT]), ("out of sample", t[t.day >= SPLIT])):
            if len(sub) < 5:
                rows.append((margin, name, len(sub), *[np.nan] * 5))
                continue
            # 95% range by resampling whole city-days (brackets on the same day are linked)
            per_day = sub.groupby(["series", "day"]).pnl.agg(["sum", "count"])
            boots = [per_day.iloc[i]["sum"].sum() / per_day.iloc[i]["count"].sum()
                     for i in (rng.integers(0, len(per_day), len(per_day)) for _ in range(1000))]
            rows.append((margin, name, len(sub), sub.pnl.mean() * 100, np.percentile(boots, 2.5) * 100,
                         np.percentile(boots, 97.5) * 100, (sub.side == "YES").mean(), sub.px.mean()))
    out = pd.DataFrame(rows, columns=["margin", "period", "trades", "edge_c", "lo_c", "hi_c", "yes_share", "avg_px"])
    print(out.round(2).to_string(index=False))
    return mk


def main():
    m, c, f = load()
    print(f"{len(m):,} settled markets, {m.series.nunique()} cities, {len(f):,} forecast rows")
    utc = lambda d, h, back=0: int(datetime.combine(date.fromisoformat(d) - timedelta(days=back), dtime(h),
                                                    timezone.utc).timestamp())
    run(m, c, f, 1, "A: 12:00 UTC on the day", lambda d: utc(d, 12))
    run(m, c, f, 2, "B: 18:00 UTC the day before", lambda d: utc(d, 18, back=1))


if __name__ == "__main__":
    main()
