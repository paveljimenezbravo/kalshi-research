"""#29: does #23's "sell the tails 6 hours before the close" hold on stock indexes?

Markets: Kalshi's daily 4pm ET "S&P 500 above $X" (KXINXU) and "Nasdaq-100 above $X"
(KXNASDAQ100U) events. The rule is #23's, unchanged: 6 hours before the close (10am ET),
buy NO on every strike whose YES is priced under 20c (at 1 - the YES bid) and YES on every
strike priced over 80c (at the YES ask), after the fee. Strikes: the 14 closest to the
previous trading day's settlement, within 3% of it (stocks move less than crypto; that
price is known at the decision time). Pass (written before running): pooled over both
indexes, more than +0.5c per contract with a 95% range above zero, resampling whole days,
at the 6 h quote and at the next hour's. Data: data\\index_daily.sqlite.
Run: python index_daily_test.py
"""
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd

from alt_daily_test import fee, strike
from btc_daily_fetch import event_markets
from config import HOME, KALSHI_API
from fetch_data import NotFound, Pacer, get_json, num, ts

DB = HOME / "data" / "index_daily.sqlite"
SERIES = ("KXINXU", "KXNASDAQ100U")
FIRST_DAY = date(2025, 12, 1)
BAND, PER_EVENT = 0.03, 14
SCHEMA = """
CREATE TABLE IF NOT EXISTS events (event TEXT PRIMARY KEY, series TEXT, day TEXT, close_ts INTEGER, value REAL);
CREATE TABLE IF NOT EXISTS markets (ticker TEXT PRIMARY KEY, event TEXT, series TEXT, close_ts INTEGER, strike REAL,
    result TEXT, source TEXT, candles_done INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS candles (ticker TEXT, ts INTEGER, bid REAL, ask REAL, PRIMARY KEY (ticker, ts));
"""


def download(con, pacer):
    for series in SERIES:
        ref, d, n = None, FIRST_DAY, 0
        while d <= datetime.now(timezone.utc).date() - timedelta(days=1):
            if d.weekday() >= 5:  # no index close on weekends
                d += timedelta(days=1)
                continue
            event = f"{series}-{d:%y%b%d}H1600".upper()
            row = con.execute("SELECT value, close_ts FROM events WHERE event=?", (event,)).fetchone()
            if row is None:
                try:
                    source, ms = event_markets(pacer, event)
                except Exception as e:
                    print(f"  {event}: skipped ({e})", flush=True)
                    d += timedelta(days=1)
                    continue
                value = None
                if ms:
                    try:
                        value = float(str(ms[0].get("expiration_value") or "").replace(",", ""))
                    except ValueError:
                        pass
                    if ref:
                        near = sorted((m for m in ms if m.get("result") in ("yes", "no") and strike(m)
                                       and abs(strike(m) / ref - 1) <= BAND),
                                      key=lambda m: abs(strike(m) - ref))[:PER_EVENT]
                        con.executemany("INSERT OR IGNORE INTO markets (ticker, event, series, close_ts, strike, result, "
                                        "source) VALUES (?,?,?,?,?,?,?)",
                                        [(m["ticker"], event, series, ts(m["close_time"]), strike(m), m["result"],
                                          source) for m in near])
                        n += len(near)
                con.execute("INSERT OR REPLACE INTO events VALUES (?,?,?,?,?)",
                            (event, series, d.isoformat(), ts(ms[0]["close_time"]) if ms else None, value))
                con.commit()
                row = (value, None)
            if row[0]:
                ref = row[0]  # carries over weekends and holidays: the last close known at decision time
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
            print(f"  quotes: {min(i + 300, len(todo)):,}/{len(todo):,}", flush=True)


def report(con):
    m = pd.read_sql("SELECT * FROM markets WHERE candles_done = 1", con)
    c = pd.read_sql("SELECT * FROM candles WHERE bid IS NOT NULL AND ask IS NOT NULL", con).sort_values("ts")
    m["day"] = pd.to_datetime(m.close_ts, unit="s").dt.date
    print(f"\n{len(m):,} markets on {m.day.nunique()} days, {m.series.nunique()} indexes")
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
        if len(t) < 20:
            print(f"\n  {label}: only {len(t)} trades")
            continue
        g = t.groupby("day").pnl.agg(["sum", "count"])
        idx = rng.integers(0, len(g), (4000, len(g)))
        boots = g["sum"].to_numpy()[idx].sum(1) / g["count"].to_numpy()[idx].sum(1)
        lo, hi = np.percentile(boots, [2.5, 97.5]) * 100
        verdict = "PASS" if t.pnl.mean() > 0.005 and lo > 0 else "fail"
        print(f"\n  {label}: {len(t):,} trades on {t.day.nunique()} days, won {t.won.mean():.1%} at {t.price.mean():.3f}"
              f" -> {t.pnl.mean() * 100:+.2f}c [{lo:+.2f}, {hi:+.2f}]  {verdict}")
        for s, gs in t.groupby("series"):
            print(f"    {s:13s} {len(gs):5,d} trades, {gs.pnl.mean() * 100:+.2f}c")
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
