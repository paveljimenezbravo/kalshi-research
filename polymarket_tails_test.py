"""#33: does #23's tail mispricing exist on Polymarket's daily "Bitcoin above ___" markets?

Polymarket lists a daily event "Bitcoin above ___ on <date>?" with ~11 strikes, settling on
Binance's BTC/USDT 1-minute candle closing at noon ET. Same rule as #23, unchanged: 6 hours
before the close (6am ET), buy NO on every strike whose YES is priced under 20c, YES on
every strike priced over 80c, hold to settlement. Polymarket's public price history has one
price per hour but no bid/ask, so the cost is that price plus an assumed half-spread
(pre-registered: a 1c spread; 0c and 2c shown as checks), plus Polymarket's crypto taker fee
(0.07 x price x (1 - price), from the markets' fee schedule).
Pass (written before running): > +0.5c per contract with a 95% range above zero, resampling
whole days, at the 6 h price and an hour later, with the 1c spread. Public data only; this
says nothing about whether you can trade there. Run: python polymarket_tails_test.py
"""
import json
import sqlite3
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd

from config import HOME
from fetch_data import Pacer

DB = HOME / "data" / "polymarket.sqlite"
FIRST_DAY = date(2025, 12, 12)
SPREADS = (0.01, 0.0, 0.02)  # the first one is the pre-registered assumption
PACER = Pacer(6)
fee = lambda p: 0.07 * p * (1 - p)
SCHEMA = """
CREATE TABLE IF NOT EXISTS markets (token TEXT PRIMARY KEY, day TEXT, close_ts INTEGER, strike REAL, yes_won INTEGER,
    volume REAL, done INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS prices (token TEXT, ts INTEGER, p REAL, PRIMARY KEY (token, ts));
CREATE TABLE IF NOT EXISTS days (day TEXT PRIMARY KEY, found INTEGER);
"""


def get(url):
    PACER.wait()
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "btc15-research"}), timeout=60) as r:
        return json.load(r)


def event_for(d):
    for slug in (f"bitcoin-above-on-{d:%B}-{d.day}".lower(), f"bitcoin-above-on-{d:%B}-{d.day}-{d.year}".lower()):
        for e in get(f"https://gamma-api.polymarket.com/events?slug={slug}"):
            if (e.get("endDate") or "").startswith(d.isoformat()):
                return e
    return None


def download(con):
    d = FIRST_DAY
    while d <= datetime.now(timezone.utc).date() - timedelta(days=1):
        if not con.execute("SELECT 1 FROM days WHERE day=?", (d.isoformat(),)).fetchone():
            e = event_for(d)
            rows = []
            for m in (e or {}).get("markets") or []:
                prices = json.loads(m.get("outcomePrices") or "[]")
                if prices not in (["1", "0"], ["0", "1"]):
                    continue  # unresolved or split
                try:
                    strike = float((m.get("groupItemTitle") or "").replace(",", "").replace("$", ""))
                except ValueError:
                    continue
                close = int(datetime.fromisoformat(m["endDate"].replace("Z", "+00:00")).timestamp())
                rows.append((json.loads(m["clobTokenIds"])[0], d.isoformat(), close, strike, int(prices[0] == "1"),
                             float(m.get("volume") or 0)))
            con.executemany("INSERT OR IGNORE INTO markets (token, day, close_ts, strike, yes_won, volume) VALUES "
                            "(?,?,?,?,?,?)", rows)
            con.execute("INSERT OR REPLACE INTO days VALUES (?,?)", (d.isoformat(), int(bool(rows))))
            con.commit()
        d += timedelta(days=1)
    print(f"  {con.execute('SELECT SUM(found) FROM days').fetchone()[0]} days with a resolved event", flush=True)
    todo = con.execute("SELECT token, close_ts FROM markets WHERE done = 0").fetchall()
    print(f"  price history for {len(todo):,} markets", flush=True)

    def one(x):
        token, close = x
        try:
            h = get(f"https://clob.polymarket.com/prices-history?market={token}&startTs={close - 9 * 3600}"
                    f"&endTs={close}&fidelity=60").get("history") or []
            return token, h
        except Exception as ex:
            print(f"  skipped a market: {ex}", flush=True)
            return token, None

    with ThreadPoolExecutor(4) as pool:
        for token, h in pool.map(one, todo):
            if h is None:
                continue
            con.executemany("INSERT OR REPLACE INTO prices VALUES (?,?,?)", [(token, int(x["t"]), float(x["p"])) for x in h])
            con.execute("UPDATE markets SET done = 1 WHERE token = ?", (token,))
    con.commit()


def report(con):
    m = pd.read_sql("SELECT * FROM markets WHERE done = 1", con)
    pr = pd.read_sql("SELECT * FROM prices", con).sort_values("ts")
    print(f"\n{len(m):,} strikes on {m.day.nunique()} days; median volume per strike ${m.volume.median():,.0f}")
    rng = np.random.default_rng(0)
    for label, back in (("6 h before the close (as pre-registered)", 6), ("5 h before (an hour later)", 5)):
        x = m.assign(t=m.close_ts - back * 3600)
        q = pd.merge_asof(x.sort_values("t"), pr.rename(columns={"ts": "tp"}), left_on="t", right_on="tp",
                          by="token", direction="backward")
        q = q[(q.t - q.tp <= 7200) & (q.p > 0) & (q.p < 1)].copy()
        if label.startswith("6"):
            cal = q.groupby(pd.cut(q.p, [0, 0.03, 0.05, 0.1, 0.2, 0.8, 0.9, 0.95, 0.97, 1]), observed=True).agg(
                n=("yes_won", "size"), price=("p", "mean"), yes_won=("yes_won", "mean"))
            print("  calibration 6 h before: " + cal.round(3).to_string().replace("\n", "\n  "))
        for spread in SPREADS:
            t = pd.concat([q[q.p < 0.20].assign(price=lambda z: 1 - z.p + spread / 2, won=lambda z: 1 - z.yes_won),
                           q[q.p > 0.80].assign(price=lambda z: z.p + spread / 2, won=lambda z: z.yes_won)])
            t["pnl"] = t.won - t.price - fee(t.price)
            g = t.groupby("day").pnl.agg(["sum", "count"])
            idx = rng.integers(0, len(g), (4000, len(g)))
            boots = g["sum"].to_numpy()[idx].sum(1) / g["count"].to_numpy()[idx].sum(1)
            lo, hi = np.percentile(boots, [2.5, 97.5]) * 100
            star = "  <- pre-registered" if spread == SPREADS[0] else ""
            verdict = ("PASS" if t.pnl.mean() > 0.005 and lo > 0 else "fail") if spread == SPREADS[0] else ""
            print(f"  {label}, {spread * 100:.0f}c spread: {len(t):,} trades on {t.day.nunique()} days, won "
                  f"{t.won.mean():.1%} at {t.price.mean():.3f} -> {t.pnl.mean() * 100:+.2f}c [{lo:+.2f}, {hi:+.2f}] "
                  f"{verdict}{star}")


def main():
    con = sqlite3.connect(DB, timeout=300)
    con.executescript(SCHEMA)
    download(con)
    report(con)


if __name__ == "__main__":
    main()
