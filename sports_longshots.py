"""#27: are cheap sports contracts underpriced shortly before the close? A clean re-test.

#25's hint (sports YES priced ~11c won ~23% two hours before the close) used hourly quotes
that could be hours old. This re-test uses sports series #25 did NOT sample, minute-level
quotes no more than 5 minutes old, and only tight books (spread <= 3c). At 2 hours (the
pre-registered time), 1 hour and 30 minutes before the close: buy YES at the ask on every
contract priced 3-20c (mid), after that series' fee. Pass = the acceptance criteria
(including at least 1,000 trades), 95% ranges resampling whole events.
Data: data\\sports.sqlite. Run: python sports_longshots.py
"""
import sqlite3

import numpy as np
import pandas as pd

from calibration_atlas import DB as ATLAS_DB
from config import HOME, KALSHI_API
from fetch_data import Pacer, get_json, num, ts

DB = HOME / "data" / "sports.sqlite"
N_SERIES, MIN_VOLUME, MAX_MARKETS, PER_EVENT = 200, 1000, 4000, 2
DECISIONS_MIN = (120, 60, 30)
MAX_AGE, MAX_SPREAD = 5 * 60, 0.03
SCHEMA = """
CREATE TABLE IF NOT EXISTS markets (ticker TEXT PRIMARY KEY, event TEXT, series TEXT, fee_mult REAL, open_ts INTEGER,
    close_ts INTEGER, result TEXT, volume REAL, sampled INTEGER DEFAULT 0, candles_done INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS candles (ticker TEXT, ts INTEGER, bid REAL, ask REAL, PRIMARY KEY (ticker, ts));
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""
rng = np.random.default_rng(1)


def download(con, pacer):
    if not con.execute("SELECT 1 FROM meta WHERE key='listed'").fetchone():
        used = {r[0] for r in sqlite3.connect(ATLAS_DB).execute("SELECT DISTINCT series FROM markets")}
        series = [s for s in get_json(f"{KALSHI_API}/series", pacer).get("series") or []
                  if s.get("category") == "Sports" and s["ticker"] not in used and not s["ticker"].startswith("KXMVE")]
        for i in rng.permutation(len(series))[:N_SERIES]:
            s = series[i]
            r = get_json(f"{KALSHI_API}/markets?series_ticker={s['ticker']}&status=settled&limit=1000", pacer)
            rows = [(m["ticker"], m["event_ticker"], s["ticker"], float(s.get("fee_multiplier") or 1),
                     ts(m["open_time"]), ts(m["close_time"]), m["result"], num(m, "volume") or 0)
                    for m in r.get("markets") or []
                    if m.get("result") in ("yes", "no") and (num(m, "volume") or 0) >= MIN_VOLUME
                    and ts(m["close_time"]) - ts(m["open_time"]) >= 3 * 3600]
            con.executemany("INSERT OR IGNORE INTO markets (ticker, event, series, fee_mult, open_ts, close_ts, result, "
                            "volume) VALUES (?,?,?,?,?,?,?,?)", rows)
        m = pd.read_sql("SELECT ticker, event FROM markets", con).sample(frac=1, random_state=1)
        picked = m.groupby("event").head(PER_EVENT).ticker[:MAX_MARKETS]
        con.executemany("UPDATE markets SET sampled = 1 WHERE ticker = ?", [(t,) for t in picked])
        con.execute("INSERT OR REPLACE INTO meta VALUES ('listed', '1')")
        con.commit()
        print(f"  {len(m):,} qualifying markets, {len(picked):,} sampled", flush=True)
    todo = con.execute("SELECT ticker, close_ts FROM markets WHERE sampled = 1 AND candles_done = 0 "
                       "ORDER BY close_ts").fetchall()
    print(f"  minute quotes for {len(todo):,} markets", flush=True)
    i = 0
    while i < len(todo):
        j, lo = i + 1, todo[i][1] - 130 * 60
        while j < len(todo) and j - i < 100 and (j + 1 - i) * ((todo[j][1] - lo) // 60 + 1) <= 9000:
            j += 1
        chunk = todo[i:j]
        r = get_json(f"{KALSHI_API}/markets/candlesticks?market_tickers={','.join(t for t, _ in chunk)}"
                     f"&start_ts={lo}&end_ts={chunk[-1][1]}&period_interval=1", pacer)
        got = {d["market_ticker"]: d.get("candlesticks") or [] for d in r.get("markets", [])}
        for t, close in chunk:
            con.executemany("INSERT OR REPLACE INTO candles VALUES (?,?,?,?)", [
                (t, c["end_period_ts"], num(c.get("yes_bid") or {}, "close"), num(c.get("yes_ask") or {}, "close"))
                for c in got.get(t, []) if close - 130 * 60 <= c["end_period_ts"] <= close])
            con.execute("UPDATE markets SET candles_done = 1 WHERE ticker = ?", (t,))
        con.commit()
        i = j


def report(con):
    m = pd.read_sql("SELECT * FROM markets WHERE candles_done = 1", con)
    c = pd.read_sql("SELECT * FROM candles WHERE bid IS NOT NULL AND ask IS NOT NULL", con).sort_values("ts")
    print(f"\n{len(m):,} sports markets from {m.series.nunique()} series, {m.event.nunique():,} events")
    for minutes in DECISIONS_MIN:
        x = m.assign(t=m.close_ts - minutes * 60)
        q = pd.merge_asof(x.sort_values("t"), c.rename(columns={"ts": "tq"}), left_on="t", right_on="tq",
                          by="ticker", direction="backward")
        q = q[(q.t - q.tq <= MAX_AGE) & (q.bid > 0) & (q.ask < 1) & (q.ask - q.bid <= MAX_SPREAD)].copy()
        q["mid"] = (q.bid + q.ask) / 2
        q["won"] = (q.result == "yes").astype(float)
        cal = q.groupby(pd.cut(q.mid, [0, 0.03, 0.1, 0.2, 0.35, 0.65, 0.8, 0.9, 0.97, 1]), observed=True).agg(
            n=("won", "size"), price=("mid", "mean"), yes_won=("won", "mean"))
        t = q[(q.mid >= 0.03) & (q.mid <= 0.20)].copy()
        t["pnl"] = t.won - t.ask - 0.07 * t.fee_mult * t.ask * (1 - t.ask)
        star = " <- pre-registered" if minutes == 120 else ""
        print(f"\n=== {minutes} min before the close: {len(q):,} fresh tight quotes ===")
        print("  calibration: " + cal.round(3).to_string().replace("\n", "\n  "))
        if len(t) >= 20:
            g = t.groupby("event").pnl.agg(["sum", "count"])
            idx = rng.integers(0, len(g), (4000, len(g)))
            boots = g["sum"].to_numpy()[idx].sum(1) / g["count"].to_numpy()[idx].sum(1)
            lo, hi = np.percentile(boots, [2.5, 97.5]) * 100
            print(f"  buy YES at the ask on 3-20c: {len(t):,} trades on {t.event.nunique():,} events, won "
                  f"{t.won.mean():.1%} at {t.ask.mean():.3f}, {t.pnl.mean() * 100:+.2f}c [{lo:+.2f}, {hi:+.2f}]{star}")
        else:
            print(f"  buy YES at the ask on 3-20c: only {len(t)} trades")


def main():
    con = sqlite3.connect(DB, timeout=300)
    con.executescript(SCHEMA)
    download(con, Pacer(8))
    report(con)


if __name__ == "__main__":
    main()
