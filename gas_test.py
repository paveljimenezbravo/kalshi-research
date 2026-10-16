"""#26: Kalshi's "AAA national average gas price tomorrow" markets (KXAAAGASD).

Retail gas prices move slowly and trend (they follow wholesale prices with a lag), so
tomorrow's AAA average is mostly today's plus the recent drift. Every settled event
records AAA's official price for its day, so Kalshi's own history is the AAA series.

Model, walk-forward (only days before the decision): tomorrow's change = a + b1*today's
change + b2*yesterday's + b3*the day before's, fitted by least squares on the previous
120 days; the spread of its past misses gives the uncertainty (normal). That gives
P(price > strike) for every strike. Decisions at 16:00 and 23:00 UTC the day before
(noon and 7pm Eastern), at the hourly quote; buy the side the model says is cheap by
more than 3c after the fee. Pass = acceptance criteria in both halves of the data
(days resampled whole). Data: data\\gas.sqlite. Run: python gas_test.py
"""
import math
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd

from config import HOME, KALSHI_API
from fetch_data import NotFound, Pacer, get_json, num, ts
from weather_fetch import MONTHS

DB = HOME / "data" / "gas.sqlite"
SERIES = "KXAAAGASD"
MARGIN, WINDOW, MIN_DAYS = 0.03, 120, 40
SCHEMA = """
CREATE TABLE IF NOT EXISTS markets (ticker TEXT PRIMARY KEY, event TEXT, day TEXT, strike REAL, strike_type TEXT,
    result TEXT, value REAL, open_ts INTEGER, close_ts INTEGER, source TEXT, candles_done INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS candles (ticker TEXT, ts INTEGER, bid REAL, ask REAL, PRIMARY KEY (ticker, ts));
"""
fee = lambda p: 0.07 * p * (1 - p)


def ncdf(x):
    return 0.5 * (1 + np.vectorize(math.erf)(np.asarray(x, dtype=float) / math.sqrt(2)))


def event_day(event):
    code = event.split("-")[1]
    return date(2000 + int(code[:2]), MONTHS[code[2:5]], int(code[5:7])).isoformat()


def download(con, pacer):
    for source, path in (("live", "/markets?status=settled&"), ("historical", "/historical/markets?")):
        cursor = ""
        while True:
            r = get_json(f"{KALSHI_API}{path}series_ticker={SERIES}&limit=1000" + (f"&cursor={cursor}" if cursor else ""),
                         pacer)
            rows = []
            for m in r.get("markets") or []:
                try:
                    value = float(str(m.get("expiration_value") or "").replace(",", ""))
                except ValueError:
                    value = None
                if m.get("result") in ("yes", "no"):
                    rows.append((m["ticker"], m["event_ticker"], event_day(m["event_ticker"]), m.get("floor_strike"),
                                 m.get("strike_type"), m["result"], value, ts(m["open_time"]), ts(m["close_time"]), source))
            con.executemany("INSERT OR IGNORE INTO markets (ticker, event, day, strike, strike_type, result, value, "
                            "open_ts, close_ts, source) VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
            cursor = r.get("cursor")
            if not cursor or not r.get("markets"):
                break
    con.commit()
    todo = con.execute("SELECT ticker, open_ts, close_ts, source FROM markets WHERE candles_done = 0").fetchall()
    print(f"  {con.execute('SELECT COUNT(*) FROM markets').fetchone()[0]:,} markets; hourly prices for {len(todo):,}",
          flush=True)

    def one(m):
        ticker, lo, hi, source = m
        path = (f"/historical/markets/{ticker}/candlesticks" if source == "historical"
                else f"/series/{SERIES}/markets/{ticker}/candlesticks")
        try:
            return ticker, get_json(f"{KALSHI_API}{path}?start_ts={lo}&end_ts={hi}&period_interval=60",
                                    pacer).get("candlesticks") or []
        except NotFound:
            return ticker, []
        except Exception as e:
            print(f"  skipped {ticker}: {e}", flush=True)
            return ticker, None

    with ThreadPoolExecutor(4) as pool:
        for i in range(0, len(todo), 200):
            for ticker, cs in pool.map(one, todo[i:i + 200]):
                if cs is None:
                    continue
                con.executemany("INSERT OR REPLACE INTO candles VALUES (?,?,?,?)", [
                    (ticker, c["end_period_ts"], num(c.get("yes_bid") or {}, "close"),
                     num(c.get("yes_ask") or {}, "close")) for c in cs])
                con.execute("UPDATE markets SET candles_done = 1 WHERE ticker = ?", (ticker,))
            con.commit()
            print(f"  prices: {min(i + 200, len(todo)):,}/{len(todo):,}", flush=True)


def forecasts(aaa):
    """For each day t+1 whose day t (and the 3 changes before it) are known: the predicted
    price and the spread of past misses. aaa is a calendar-daily series, NaN on days
    without a Kalshi event, so a change only counts between two consecutive days."""
    d = aaa.diff().to_numpy()
    out = {}
    for i in range(3, len(aaa) - 1):
        if np.isnan(aaa.iloc[i]) or np.isnan(d[i - 2:i + 1]).any():
            continue
        y = d[max(0, i - WINDOW):i + 1]
        rows = [j for j in range(3, len(y)) if not np.isnan(y[j - 3:j + 1]).any()]
        if len(rows) < MIN_DAYS:
            continue
        X = np.array([[1, y[j - 1], y[j - 2], y[j - 3]] for j in rows])
        Y = np.array([y[j] for j in rows])
        coef, *_ = np.linalg.lstsq(X, Y, rcond=None)
        resid = Y - X @ coef
        pred = coef @ [1, y[-1], y[-2], y[-3]]
        out[aaa.index[i + 1]] = (aaa.iloc[i] + pred, max(resid.std(ddof=4), 0.002))
    return out


def report(con):
    m = pd.read_sql("SELECT * FROM markets WHERE candles_done = 1 AND value IS NOT NULL", con)
    c = pd.read_sql("SELECT * FROM candles WHERE bid IS NOT NULL AND ask IS NOT NULL", con).sort_values("ts")
    aaa = m.groupby("day").value.first().sort_index()
    aaa.index = pd.to_datetime(aaa.index)
    aaa = aaa.asfreq("D")  # a missing day stays a gap (no forecast across it)
    fc = forecasts(aaa)
    print(f"\n{len(aaa.dropna())} days of AAA prices, {aaa.index.min():%Y-%m-%d} to {aaa.index.max():%Y-%m-%d}; "
          f"day-to-day change sd {aaa.diff().std() * 100:.2f}c; forecasts for {len(fc)} days")
    m["mu"] = [fc.get(pd.Timestamp(d), (np.nan, np.nan))[0] for d in m.day]
    m["sd"] = [fc.get(pd.Timestamp(d), (np.nan, np.nan))[1] for d in m.day]
    m = m.dropna(subset=["mu"])
    m["p"] = 1 - ncdf((m.strike - m.mu) / m.sd)  # 'greater' markets: YES if the price ends above the strike
    m.loc[m.strike_type == "less", "p"] = 1 - m.p
    split = m.day.sort_values().iloc[len(m) // 2]
    rng = np.random.default_rng(0)
    for hour in (16, 23):
        x = m.assign(t=[int((datetime.fromisoformat(d).replace(tzinfo=timezone.utc) - timedelta(days=1)
                             + timedelta(hours=hour)).timestamp()) for d in m.day])
        q = pd.merge_asof(x.sort_values("t"), c.rename(columns={"ts": "tq"}), left_on="t", right_on="tq",
                          by="ticker", direction="backward")
        q = q[(q.t - q.tq <= 3 * 3600) & (q.bid > 0) & (q.ask < 1)].copy()
        q["won_yes"] = (q.result == "yes").astype(float)
        q["mid"] = (q.bid + q.ask) / 2
        print(f"\n=== {hour}:00 UTC the day before: {len(q):,} quoted strikes on {q.day.nunique()} days ===")
        print(f"  Brier: model {((q.p - q.won_yes) ** 2).mean():.4f} vs Kalshi mid {((q.mid - q.won_yes) ** 2).mean():.4f}")
        e_yes = q.p - q.ask - fee(q.ask)
        e_no = (1 - q.p) - (1 - q.bid) - fee(1 - q.bid)
        t = pd.concat([q[(e_yes >= e_no) & (e_yes > MARGIN)].assign(price=lambda z: z.ask, won=lambda z: z.won_yes),
                       q[(e_no > e_yes) & (e_no > MARGIN)].assign(price=lambda z: 1 - z.bid, won=lambda z: 1 - z.won_yes)])
        t["pnl"] = t.won - t.price - fee(t.price)
        for name, sub in (("1st half", t[t.day < split]), ("2nd half", t[t.day >= split])):
            if len(sub) < 20:
                print(f"  {name}: {len(sub)} trades")
                continue
            g = sub.groupby("day").pnl.agg(["sum", "count"])
            idx = rng.integers(0, len(g), (2000, len(g)))
            boots = g["sum"].to_numpy()[idx].sum(1) / g["count"].to_numpy()[idx].sum(1)
            print(f"  {name}: {len(sub):,} trades on {sub.day.nunique()} days, won {sub.won.mean():.1%} at "
                  f"{sub.price.mean():.2f}, {sub.pnl.mean() * 100:+.2f}c "
                  f"[{np.percentile(boots, 2.5) * 100:+.2f}, {np.percentile(boots, 97.5) * 100:+.2f}]")


def main():
    con = sqlite3.connect(DB, timeout=300)
    con.executescript(SCHEMA)
    download(con, Pacer(4))
    report(con)


if __name__ == "__main__":
    main()
