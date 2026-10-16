"""#25: where on Kalshi are prices most wrong? A calibration atlas by category.

Picks up to 80 random series per category (skipping multi-leg parlays, which flood
Kalshi's settled list, and the crypto/weather series studied elsewhere), lists each one's
settled markets (roughly the last two months), and samples up to 800 markets per category,
at most 2 per event, with at least 1,000 contracts traded. Downloads each
market's hourly quotes over its last 30 hours (100 markets per request), then, 24 hours
and 2 hours before the close:
  - calibration: Kalshi's price vs how often YES actually won
  - "sell longshots / buy favourites": NO on YES priced 3-20c, YES on YES priced 80-97c,
    at the quote, after that series' fee
A category passes only with a 99.5% range (about 12 categories tried) above zero at both
decision times; ranges resample whole events. Data: data\\atlas.sqlite.
Run: python calibration_atlas.py           (downloads what's missing, then reports)
"""
import sqlite3
import time
from datetime import datetime

import numpy as np
import pandas as pd

from config import HOME, KALSHI_API
from fetch_data import Pacer, get_json, num, ts

DB = HOME / "data" / "atlas.sqlite"
SERIES_PER_CATEGORY, MIN_VOLUME, PER_CATEGORY, PER_EVENT = 80, 1000, 800, 2
SKIP = ("KXMVE", "KXBTC", "KXETH", "KXSOL", "KXXRP", "KXDOGE", "KXHIGH")
SCHEMA = """
CREATE TABLE IF NOT EXISTS markets (ticker TEXT PRIMARY KEY, event TEXT, series TEXT, category TEXT, fee_mult REAL,
    open_ts INTEGER, close_ts INTEGER, result TEXT, volume REAL, sampled INTEGER DEFAULT 0, candles_done INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS candles (ticker TEXT, ts INTEGER, bid REAL, ask REAL, PRIMARY KEY (ticker, ts));
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""
rng = np.random.default_rng(0)


def list_markets(con, pacer):
    if con.execute("SELECT value FROM meta WHERE key='listed'").fetchone():
        return
    by_cat = {}
    for x in get_json(f"{KALSHI_API}/series", pacer).get("series") or []:
        if not x["ticker"].startswith(SKIP):
            by_cat.setdefault(x.get("category") or "?", []).append(x)
    kept = 0
    for cat, series in sorted(by_cat.items()):
        n = 0
        for i in rng.permutation(len(series))[:SERIES_PER_CATEGORY]:
            x = series[i]
            r = get_json(f"{KALSHI_API}/markets?series_ticker={x['ticker']}&status=settled&limit=1000", pacer)
            rows = []
            for m in r.get("markets") or []:
                vol = num(m, "volume") or 0
                if (m.get("result") in ("yes", "no") and vol >= MIN_VOLUME
                        and ts(m["close_time"]) - ts(m["open_time"]) >= 26 * 3600):
                    rows.append((m["ticker"], m["event_ticker"], x["ticker"], cat, float(x.get("fee_multiplier") or 1),
                                 ts(m["open_time"]), ts(m["close_time"]), m["result"], vol))
            con.executemany("INSERT OR IGNORE INTO markets (ticker, event, series, category, fee_mult, open_ts, "
                            "close_ts, result, volume) VALUES (?,?,?,?,?,?,?,?,?)", rows)
            n += len(rows)
        con.commit()
        kept += n
        print(f"  {cat}: {n:,} markets from {min(len(series), SERIES_PER_CATEGORY)} series", flush=True)
    con.execute("INSERT OR REPLACE INTO meta VALUES ('listed', ?)", (str(kept),))
    con.commit()


def sample(con):
    if con.execute("SELECT COUNT(*) FROM markets WHERE sampled = 1").fetchone()[0]:
        return
    m = pd.read_sql("SELECT ticker, event, category FROM markets", con)
    m = m.sample(frac=1, random_state=0).groupby("event").head(PER_EVENT)
    picked = m.groupby("category").head(PER_CATEGORY).ticker
    con.executemany("UPDATE markets SET sampled = 1 WHERE ticker = ?", [(t,) for t in picked])
    con.commit()


def candles(con, pacer):
    todo = con.execute("SELECT ticker, close_ts FROM markets WHERE sampled = 1 AND candles_done = 0 "
                       "ORDER BY close_ts").fetchall()
    print(f"  hourly prices for {len(todo):,} markets", flush=True)
    i = 0
    while i < len(todo):
        j, lo, hi = i + 1, todo[i][1] - 30 * 3600, todo[i][1]
        while j < len(todo) and j - i < 100 and (j + 1 - i) * ((todo[j][1] - lo) // 3600 + 1) <= 9000:
            hi = todo[j][1]
            j += 1
        chunk = todo[i:j]
        r = get_json(f"{KALSHI_API}/markets/candlesticks?market_tickers={','.join(t for t, _ in chunk)}"
                     f"&start_ts={lo}&end_ts={hi}&period_interval=60", pacer)
        got = {d["market_ticker"]: d.get("candlesticks") or [] for d in r.get("markets", [])}
        for t, close in chunk:
            con.executemany("INSERT OR REPLACE INTO candles VALUES (?,?,?,?)", [
                (t, c["end_period_ts"], num(c.get("yes_bid") or {}, "close"), num(c.get("yes_ask") or {}, "close"))
                for c in got.get(t, []) if close - 30 * 3600 <= c["end_period_ts"] <= close])
            con.execute("UPDATE markets SET candles_done = 1 WHERE ticker = ?", (t,))
        con.commit()
        i = j
    print("  prices done", flush=True)


def report(con):
    m = pd.read_sql("SELECT * FROM markets WHERE candles_done = 1", con)
    c = pd.read_sql("SELECT * FROM candles WHERE bid IS NOT NULL AND ask IS NOT NULL", con).sort_values("ts")
    first, last = (datetime.fromtimestamp(x).date() for x in (m.close_ts.min(), m.close_ts.max()))
    print(f"\n{len(m):,} sampled markets in {m.category.nunique()} categories, closing {first} to {last}")
    rows = []
    for hours in (24, 2):
        x = m.assign(t=m.close_ts - hours * 3600)
        q = pd.merge_asof(x.sort_values("t"), c.rename(columns={"ts": "tq"}), left_on="t", right_on="tq",
                          by="ticker", direction="backward")
        q = q[(q.t - q.tq <= 3 * 3600)].copy()
        q["mid"] = (q.bid + q.ask) / 2
        q["won_yes"] = (q.result == "yes").astype(float)
        fee = lambda p, k: 0.07 * k * p * (1 - p)
        long_ = q[(q.mid >= 0.03) & (q.mid <= 0.20) & (q.bid > 0)].assign(price=lambda z: 1 - z.bid,
                                                                             won=lambda z: 1 - z.won_yes)
        fav = q[(q.mid >= 0.80) & (q.mid <= 0.97) & (q.ask < 1)].assign(price=lambda z: z.ask, won=lambda z: z.won_yes)
        t = pd.concat([long_.assign(kind="NO on longshot"), fav.assign(kind="YES on favourite")])
        t["pnl"] = t.won - t.price - fee(t.price, t.fee_mult)
        for cat, g in t.groupby("category"):
            e = g.groupby("event").pnl.agg(["sum", "count"])
            idx = rng.integers(0, len(e), (2000, len(e)))
            boots = e["sum"].to_numpy()[idx].sum(1) / e["count"].to_numpy()[idx].sum(1)
            lo, hi = np.percentile(boots, [0.25, 99.75]) * 100
            ls = g[g.kind == "NO on longshot"]
            rows.append((cat, hours, len(g), g.event.nunique(), g.pnl.mean() * 100, lo, hi,
                         len(ls), ls.mid.mean() if len(ls) else np.nan, 1 - ls.won.mean() if len(ls) else np.nan))
        bins = [0, 0.03, 0.10, 0.20, 0.35, 0.65, 0.80, 0.90, 0.97, 1]
        cal = q.groupby(pd.cut(q.mid, bins), observed=True).agg(n=("won_yes", "size"), price=("mid", "mean"),
                                                                 yes_won=("won_yes", "mean"))
        print(f"\n  calibration {hours} h before the close, all categories:")
        print("    " + cal.round(3).to_string().replace("\n", "\n    "))
    out = pd.DataFrame(rows, columns=["category", "hours", "trades", "events", "edge_c", "lo_99.5", "hi_99.5",
                                      "longshots", "longshot_price", "longshot_yes_won"])
    print("\n  favourite side of everything priced <20c or >80c, after each series' fee (99.5% ranges):")
    print("  " + out.sort_values(["category", "hours"]).round(3).to_string(index=False).replace("\n", "\n  "))
    passed = out.groupby("category").apply(lambda g: (g["lo_99.5"] > 0).all() and (g.edge_c > 0.5).all() and len(g) == 2,
                                           include_groups=False)
    print("\n  categories that pass at both decision times:", ", ".join(passed[passed].index) or "none")


def main():
    con = sqlite3.connect(DB, timeout=300)
    con.executescript(SCHEMA)
    pacer = Pacer(8)
    list_markets(con, pacer)
    sample(con)
    candles(con, pacer)
    report(con)


if __name__ == "__main__":
    main()
