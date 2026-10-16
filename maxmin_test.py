"""#31: #23's rule on crypto max/min "touch" markets (will the price hit $X this month/week?).

A max market settles YES as soon as the price touches its level (the market closes then);
otherwise NO at the end of the period. Markets: the monthly max and min series for BTC,
ETH, SOL, XRP and DOGE, plus the weekly BTC and DOGE max series. Decision: 2 days before
a month ends (1 day before a week ends), for every market still open then: buy NO when
YES is priced under 20c (at 1 - the YES bid), YES when it's over 80c (at the YES ask),
after the fee. Pass (written before running): > +0.5c per contract with a 95% range above
zero, resampling whole events, AND at least 1,000 trades; with fewer, "inconclusive".
Data: data\\maxmin.sqlite. Run: python maxmin_test.py
"""
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd

from alt_daily_test import fee
from config import HOME, KALSHI_API
from fetch_data import NotFound, Pacer, get_json, num, ts

DB = HOME / "data" / "maxmin.sqlite"
MONTHLY = ("KXBTCMAXMON", "KXBTCMINMON", "KXETHMAXMON", "KXETHMINMON", "KXSOLMAXMON", "KXSOLMINMON",
           "KXXRPMAXMON", "KXXRPMINMON", "KXDOGEMAXMON", "KXDOGEMINMON")
WEEKLY = ("KXBTCMAXW", "KXDOGEMAXW")
SCHEMA = """
CREATE TABLE IF NOT EXISTS markets (ticker TEXT PRIMARY KEY, event TEXT, series TEXT, close_ts INTEGER,
    result TEXT, source TEXT);
CREATE TABLE IF NOT EXISTS decisions (ticker TEXT PRIMARY KEY, t INTEGER, done INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS candles (ticker TEXT, ts INTEGER, bid REAL, ask REAL, PRIMARY KEY (ticker, ts));
"""


def download(con, pacer):
    if not con.execute("SELECT COUNT(*) FROM markets").fetchone()[0]:
        for series in MONTHLY + WEEKLY:
            n = 0
            for source, path in (("live", "/markets?status=settled&"), ("historical", "/historical/markets?")):
                cursor = ""
                while True:
                    r = get_json(f"{KALSHI_API}{path}series_ticker={series}&limit=1000"
                                 + (f"&cursor={cursor}" if cursor else ""), pacer)
                    rows = [(m["ticker"], m["event_ticker"], series, ts(m["close_time"]), m["result"], source)
                            for m in r.get("markets") or [] if m.get("result") in ("yes", "no")]
                    con.executemany("INSERT OR IGNORE INTO markets VALUES (?,?,?,?,?,?)", rows)
                    n += len(rows)
                    cursor = r.get("cursor")
                    if not cursor or not r.get("markets"):
                        break
            con.commit()
            print(f"  {series}: {n} settled markets", flush=True)
        # the period ends when the markets that never touched closed; decide 2 days (1 for weeks) before that
        m = pd.read_sql("SELECT * FROM markets", con)
        end = m[m.result == "no"].groupby("event").close_ts.max()
        m = m.join(end.rename("end"), on="event").dropna(subset=["end"])
        m["t"] = m.end - np.where(m.series.isin(WEEKLY), 1, 2) * 86400
        open_then = m[m.close_ts > m.t]
        con.executemany("INSERT OR IGNORE INTO decisions (ticker, t) VALUES (?,?)",
                        [(r.ticker, int(r.t)) for r in open_then.itertuples()])
        con.commit()
    todo = con.execute("SELECT d.ticker, d.t, m.series, m.source FROM decisions d JOIN markets m USING (ticker) "
                       "WHERE d.done = 0").fetchall()
    print(f"  quotes for {len(todo):,} markets that were still open at the decision time", flush=True)

    def one(x):
        ticker, t, series, source = x
        path = (f"/historical/markets/{ticker}/candlesticks" if source == "historical"
                else f"/series/{series}/markets/{ticker}/candlesticks")
        try:
            return ticker, get_json(f"{KALSHI_API}{path}?start_ts={t - 3 * 3600}&end_ts={t + 3600}&period_interval=60",
                                    pacer).get("candlesticks") or []
        except NotFound:
            return ticker, []
        except Exception as e:
            print(f"  skipped {ticker}: {e}", flush=True)
            return ticker, None

    with ThreadPoolExecutor(8) as pool:
        for ticker, cs in pool.map(one, todo):
            if cs is None:
                continue
            con.executemany("INSERT OR REPLACE INTO candles VALUES (?,?,?,?)", [
                (ticker, c["end_period_ts"], num(c.get("yes_bid") or {}, "close"), num(c.get("yes_ask") or {}, "close"))
                for c in cs])
            con.execute("UPDATE decisions SET done = 1 WHERE ticker = ?", (ticker,))
    con.commit()


def report(con):
    m = pd.read_sql("SELECT * FROM decisions d JOIN markets m USING (ticker) WHERE d.done = 1", con)
    c = pd.read_sql("SELECT * FROM candles WHERE bid IS NOT NULL AND ask IS NOT NULL", con).sort_values("ts")
    q = pd.merge_asof(m.sort_values("t"), c.rename(columns={"ts": "tq"}), left_on="t", right_on="tq",
                      by="ticker", direction="backward")
    q = q[(q.t - q.tq <= 7200) & (q.bid > 0) & (q.ask < 1)].copy()
    q["won_yes"] = (q.result == "yes").astype(float)
    q["mid"] = (q.bid + q.ask) / 2
    print(f"\n{len(q):,} quoted markets on {q.event.nunique()} events at the decision time")
    cal = q.groupby(pd.cut(q.mid, [0, 0.03, 0.05, 0.1, 0.2, 0.35, 0.65, 0.8, 1]), observed=True).agg(
        n=("won_yes", "size"), price=("mid", "mean"), yes_won=("won_yes", "mean"))
    print("  calibration: " + cal.round(3).to_string().replace("\n", "\n  "))
    t = pd.concat([q[q.mid < 0.20].assign(price=lambda z: 1 - z.bid, won=lambda z: 1 - z.won_yes),
                   q[q.mid > 0.80].assign(price=lambda z: z.ask, won=lambda z: z.won_yes)])
    t["pnl"] = t.won - t.price - fee(t.price)
    if len(t) < 5:
        print(f"  only {len(t)} trades")
        return
    g = t.groupby("event").pnl.agg(["sum", "count"])
    rng = np.random.default_rng(0)
    idx = rng.integers(0, len(g), (4000, len(g)))
    boots = g["sum"].to_numpy()[idx].sum(1) / g["count"].to_numpy()[idx].sum(1)
    lo, hi = np.percentile(boots, [2.5, 97.5]) * 100
    verdict = ("PASS" if t.pnl.mean() > 0.005 and lo > 0 else "fail") if len(t) >= 1000 else "inconclusive (< 1,000 trades)"
    print(f"\n  sell the tails: {len(t):,} trades on {t.event.nunique()} events, won {t.won.mean():.1%} at "
          f"{t.price.mean():.3f} -> {t.pnl.mean() * 100:+.2f}c [{lo:+.2f}, {hi:+.2f}]  {verdict}")
    for s, gs in t.groupby("series"):
        print(f"    {s:13s} {len(gs):4d} trades, {gs.pnl.mean() * 100:+.2f}c")


def main():
    con = sqlite3.connect(DB, timeout=300)
    con.executescript(SCHEMA)
    download(con, Pacer(6))
    report(con)


if __name__ == "__main__":
    main()
