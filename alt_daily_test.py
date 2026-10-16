"""#28: does #23 ("sell the tails" 6 hours before the 5pm ET close) hold on other coins?

The rule is #23's, unchanged: at 11am ET, buy NO on every strike whose YES is priced under
20c (at 1 - the YES bid) and YES on every strike priced over 80c (at the YES ask), after
the fee, and hold to the 5pm settlement. Markets: Kalshi's daily 5pm events for ETH, SOL,
XRP and DOGE (KXETHD, KXSOLD, KXXRPD, KXDOGED), which #23 never saw. Strikes: the 14
closest to the previous day's settlement price, within 4% of it (that price is known at
the decision time, so the choice of strikes can't peek at the outcome).
Pass (written before running): pooled over the four coins, more than +0.5c per contract
with a 95% range above zero, resampling whole days (the coins move together), at the 6 h
quote and at the next hour's. Data: data\\alt_daily.sqlite. Run: python alt_daily_test.py
"""
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd

from btc_daily_fetch import event_markets
from config import HOME, KALSHI_API
from fetch_data import NotFound, Pacer, get_json, num, ts

DB = HOME / "data" / "alt_daily.sqlite"
SERIES = ("KXETHD", "KXSOLD", "KXXRPD", "KXDOGED")
FIRST_DAY = date(2025, 12, 12)
BAND, PER_EVENT = 0.04, 14
SCHEMA = """
CREATE TABLE IF NOT EXISTS events (event TEXT PRIMARY KEY, series TEXT, day TEXT, close_ts INTEGER, value REAL);
CREATE TABLE IF NOT EXISTS markets (ticker TEXT PRIMARY KEY, event TEXT, series TEXT, close_ts INTEGER, strike REAL,
    result TEXT, source TEXT, candles_done INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS candles (ticker TEXT, ts INTEGER, bid REAL, ask REAL, PRIMARY KEY (ticker, ts));
"""
fee = lambda p: 0.07 * p * (1 - p)


def download(con, pacer):
    for series in SERIES:
        prev_value, d, n = None, FIRST_DAY, 0
        while d <= datetime.now(timezone.utc).date() - timedelta(days=1):
            event = f"{series}-{d:%y%b%d}17".upper()
            row = con.execute("SELECT value FROM events WHERE event=?", (event,)).fetchone()
            if row is None:
                try:
                    source, ms = event_markets(pacer, event)
                except Exception as e:
                    print(f"  {event}: skipped ({e})", flush=True)
                    d += timedelta(days=1)
                    prev_value = None
                    continue
                value = None
                if ms:
                    try:
                        value = float(str(ms[0].get("expiration_value") or "").replace(",", ""))
                    except ValueError:
                        pass
                    con.execute("INSERT OR REPLACE INTO events VALUES (?,?,?,?,?)",
                                (event, series, d.isoformat(), ts(ms[0]["close_time"]), value))
                    if prev_value:
                        near = sorted((m for m in ms if m.get("result") in ("yes", "no") and m.get("floor_strike")
                                       and abs(m["floor_strike"] / prev_value - 1) <= BAND),
                                      key=lambda m: abs(m["floor_strike"] - prev_value))[:PER_EVENT]
                        con.executemany("INSERT OR IGNORE INTO markets (ticker, event, series, close_ts, strike, result, "
                                        "source) VALUES (?,?,?,?,?,?,?)",
                                        [(m["ticker"], event, series, ts(m["close_time"]), m["floor_strike"],
                                          m["result"], source) for m in near])
                        n += len(near)
                else:
                    con.execute("INSERT OR REPLACE INTO events VALUES (?,?,?,?,?)", (event, series, d.isoformat(), None, None))
                con.commit()
                prev_value = value
            else:
                prev_value = row[0]
            d += timedelta(days=1)
        print(f"  {series}: {n:,} markets listed", flush=True)

    todo = con.execute("SELECT ticker, series, close_ts, source FROM markets WHERE candles_done = 0").fetchall()
    print(f"  hourly quotes for {len(todo):,} markets", flush=True)

    def one(m):
        ticker, series, close, source = m
        path = (f"/historical/markets/{ticker}/candlesticks" if source == "historical"
                else f"/series/{series}/markets/{ticker}/candlesticks")
        try:
            return ticker, get_json(f"{KALSHI_API}{path}?start_ts={close - 9 * 3600}&end_ts={close}"
                                    f"&period_interval=60", pacer).get("candlesticks") or []
        except NotFound:
            return ticker, []
        except Exception as e:
            print(f"  skipped {ticker}: {e}", flush=True)
            return ticker, None

    start = time.monotonic()
    with ThreadPoolExecutor(12) as pool:
        for i in range(0, len(todo), 300):
            for ticker, cs in pool.map(one, todo[i:i + 300]):
                if cs is None:
                    continue
                con.executemany("INSERT OR REPLACE INTO candles VALUES (?,?,?,?)", [
                    (ticker, c["end_period_ts"], num(c.get("yes_bid") or {}, "close"),
                     num(c.get("yes_ask") or {}, "close")) for c in cs])
                con.execute("UPDATE markets SET candles_done = 1 WHERE ticker = ?", (ticker,))
            con.commit()
            done = min(i + 300, len(todo))
            print(f"  quotes: {done:,}/{len(todo):,}, about "
                  f"{(len(todo) - done) / (done / (time.monotonic() - start)) / 60:.0f} min left", flush=True)


def report(con):
    m = pd.read_sql("SELECT * FROM markets WHERE candles_done = 1", con)
    c = pd.read_sql("SELECT * FROM candles WHERE bid IS NOT NULL AND ask IS NOT NULL", con).sort_values("ts")
    m["day"] = pd.to_datetime(m.close_ts, unit="s").dt.date
    print(f"\n{len(m):,} markets on {m.day.nunique()} days across {m.series.nunique()} coins")
    rng = np.random.default_rng(0)
    for label, delay in (("6 h quote (as pre-registered)", 0), ("next hour's quote", 3600)):
        x = m.assign(t=m.close_ts - 6 * 3600 + delay)
        q = pd.merge_asof(x.sort_values("t"), c.rename(columns={"ts": "tq"}), left_on="t", right_on="tq",
                          by="ticker", direction="backward")
        q = q[(q.t - q.tq <= 7200) & (q.bid > 0) & (q.ask < 1)].copy()
        q["won_yes"] = (q.result == "yes").astype(float)
        q["mid"] = (q.bid + q.ask) / 2
        t = pd.concat([q[q.mid < 0.20].assign(price=lambda z: 1 - z.bid, won=lambda z: 1 - z.won_yes),
                       q[q.mid > 0.80].assign(price=lambda z: z.ask, won=lambda z: z.won_yes)])
        t["pnl"] = t.won - t.price - fee(t.price)
        g = t.groupby("day").pnl.agg(["sum", "count"])
        idx = rng.integers(0, len(g), (4000, len(g)))
        boots = g["sum"].to_numpy()[idx].sum(1) / g["count"].to_numpy()[idx].sum(1)
        lo, hi = np.percentile(boots, [2.5, 97.5]) * 100
        verdict = "PASS" if t.pnl.mean() > 0.005 and lo > 0 else "fail"
        print(f"\n  {label}: {len(t):,} trades on {t.day.nunique()} days, won {t.won.mean():.1%} at {t.price.mean():.3f}"
              f" -> {t.pnl.mean() * 100:+.2f}c [{lo:+.2f}, {hi:+.2f}]  {verdict}")
        for s, gs in t.groupby("series"):
            print(f"    {s:8s} {len(gs):5,d} trades, {gs.pnl.mean() * 100:+.2f}c")
        if not delay:
            cal = q.groupby(pd.cut(q.mid, [0, 0.05, 0.1, 0.2, 0.8, 0.9, 0.95, 1]), observed=True).agg(
                n=("won_yes", "size"), price=("mid", "mean"), yes_won=("won_yes", "mean"))
            print("    calibration: " + cal.round(3).to_string().replace("\n", "\n    "))


def main():
    con = sqlite3.connect(DB, timeout=300)
    con.executescript(SCHEMA)
    download(con, Pacer(12))
    report(con)


if __name__ == "__main__":
    main()
