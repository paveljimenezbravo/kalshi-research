"""#30: #23's rule on Kalshi's daily BTC range brackets (KXBTC, 5pm ET).

Each daily event has ~80 markets: $250-wide "between" brackets plus a "below" and an
"above" tail; exactly one settles YES. The rule is #23's, unchanged: 6 hours before the
close (11am ET), buy NO on every bracket whose YES is priced under 20c (at 1 - the YES
bid) and YES on any priced over 80c (at the YES ask), after the fee. Brackets: the 20
whose centres are closest to the previous day's settlement, within 5% (known at the
decision time). Pass (written before running): > +0.5c per contract with a 95% range
above zero, resampling whole days, at the 6 h quote and at the next hour's.
Data: data\\range_daily.sqlite. Run: python range_tails_test.py
"""
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd

from alt_daily_test import fee
from btc_daily_fetch import event_markets
from config import HOME, KALSHI_API
from fetch_data import NotFound, Pacer, get_json, num, ts

DB = HOME / "data" / "range_daily.sqlite"
FIRST_DAY = date(2025, 12, 12)
BAND, PER_EVENT = 0.05, 20
SCHEMA = """
CREATE TABLE IF NOT EXISTS events (event TEXT PRIMARY KEY, day TEXT, close_ts INTEGER, value REAL);
CREATE TABLE IF NOT EXISTS markets (ticker TEXT PRIMARY KEY, event TEXT, close_ts INTEGER, centre REAL,
    kind TEXT, result TEXT, source TEXT, candles_done INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS candles (ticker TEXT, ts INTEGER, bid REAL, ask REAL, PRIMARY KEY (ticker, ts));
"""


def centre(m):
    lo, hi = m.get("floor_strike"), m.get("cap_strike")
    if m.get("strike_type") == "between" and lo is not None and hi is not None:
        return (lo + hi) / 2
    return lo if m.get("strike_type") == "greater" else hi


def download(con, pacer):
    ref, d, n = None, FIRST_DAY, 0
    while d <= datetime.now(timezone.utc).date() - timedelta(days=1):
        event = f"KXBTC-{d:%y%b%d}17".upper()
        row = con.execute("SELECT value FROM events WHERE event=?", (event,)).fetchone()
        if row is None:
            try:
                source, ms = event_markets(pacer, event)
            except Exception as e:
                print(f"  {event}: skipped ({e})", flush=True)
                d, ref = d + timedelta(days=1), None
                continue
            value = None
            if ms:
                try:
                    value = float(str(ms[0].get("expiration_value") or "").replace(",", ""))
                except ValueError:
                    pass
                if ref:
                    near = sorted((m for m in ms if m.get("result") in ("yes", "no") and centre(m)
                                   and abs(centre(m) / ref - 1) <= BAND), key=lambda m: abs(centre(m) - ref))[:PER_EVENT]
                    con.executemany("INSERT OR IGNORE INTO markets (ticker, event, close_ts, centre, kind, result, source)"
                                    " VALUES (?,?,?,?,?,?,?)", [(m["ticker"], event, ts(m["close_time"]), centre(m),
                                                                 m.get("strike_type"), m["result"], source) for m in near])
                    n += len(near)
            con.execute("INSERT OR REPLACE INTO events VALUES (?,?,?,?)",
                        (event, d.isoformat(), ts(ms[0]["close_time"]) if ms else None, value))
            con.commit()
            row = (value,)
        ref = row[0]
        d += timedelta(days=1)
    print(f"  {n:,} brackets listed", flush=True)

    todo = con.execute("SELECT ticker, close_ts, source FROM markets WHERE candles_done = 0").fetchall()
    print(f"  hourly quotes for {len(todo):,} markets", flush=True)

    def one(m):
        ticker, close, source = m
        path = (f"/historical/markets/{ticker}/candlesticks" if source == "historical"
                else f"/series/KXBTC/markets/{ticker}/candlesticks")
        try:
            return ticker, get_json(f"{KALSHI_API}{path}?start_ts={close - 9 * 3600}&end_ts={close}"
                                    f"&period_interval=60", pacer).get("candlesticks") or []
        except NotFound:
            return ticker, []
        except Exception as e:
            print(f"  skipped {ticker}: {e}", flush=True)
            return ticker, None

    with ThreadPoolExecutor(12) as pool:
        for i in range(0, len(todo), 400):
            for ticker, cs in pool.map(one, todo[i:i + 400]):
                if cs is None:
                    continue
                con.executemany("INSERT OR REPLACE INTO candles VALUES (?,?,?,?)", [
                    (ticker, c["end_period_ts"], num(c.get("yes_bid") or {}, "close"),
                     num(c.get("yes_ask") or {}, "close")) for c in cs])
                con.execute("UPDATE markets SET candles_done = 1 WHERE ticker = ?", (ticker,))
            con.commit()
            print(f"  quotes: {min(i + 400, len(todo)):,}/{len(todo):,}", flush=True)


def report(con):
    m = pd.read_sql("SELECT * FROM markets WHERE candles_done = 1", con)
    c = pd.read_sql("SELECT * FROM candles WHERE bid IS NOT NULL AND ask IS NOT NULL", con).sort_values("ts")
    m["day"] = pd.to_datetime(m.close_ts, unit="s").dt.date
    print(f"\n{len(m):,} brackets on {m.day.nunique()} days")
    rng = np.random.default_rng(0)
    for label, delay in (("6 h quote (as pre-registered)", 0), ("next hour's quote", 3600)):
        x = m.assign(t=m.close_ts - 6 * 3600 + delay)
        q = pd.merge_asof(x.sort_values("t"), c.rename(columns={"ts": "tq"}), left_on="t", right_on="tq",
                          by="ticker", direction="backward")
        q = q[(q.t - q.tq <= 7200) & (q.bid > 0) & (q.ask < 1)].copy()
        q["won_yes"] = (q.result == "yes").astype(float)
        q["mid"] = (q.bid + q.ask) / 2
        t = pd.concat([q[q.mid < 0.20].assign(price=lambda z: 1 - z.bid, won=lambda z: 1 - z.won_yes, side="NO"),
                       q[q.mid > 0.80].assign(price=lambda z: z.ask, won=lambda z: z.won_yes, side="YES")])
        t["pnl"] = t.won - t.price - fee(t.price)
        g = t.groupby("day").pnl.agg(["sum", "count"])
        idx = rng.integers(0, len(g), (4000, len(g)))
        boots = g["sum"].to_numpy()[idx].sum(1) / g["count"].to_numpy()[idx].sum(1)
        lo, hi = np.percentile(boots, [2.5, 97.5]) * 100
        verdict = "PASS" if t.pnl.mean() > 0.005 and lo > 0 else "fail"
        print(f"\n  {label}: {len(t):,} trades on {t.day.nunique()} days (NO {np.mean(t.side == 'NO'):.0%}), won "
              f"{t.won.mean():.1%} at {t.price.mean():.3f} -> {t.pnl.mean() * 100:+.2f}c [{lo:+.2f}, {hi:+.2f}]  {verdict}")
        for name, sub in (("1st half", t[t.day < date(2026, 5, 12)]), ("2nd half", t[t.day >= date(2026, 5, 12)])):
            print(f"    {name}: {len(sub):,} trades, {sub.pnl.mean() * 100:+.2f}c")
        if not delay:
            cal = q.groupby(pd.cut(q.mid, [0, 0.03, 0.05, 0.1, 0.2, 0.35, 0.65, 1]), observed=True).agg(
                n=("won_yes", "size"), price=("mid", "mean"), yes_won=("won_yes", "mean"))
            print("    calibration: " + cal.round(3).to_string().replace("\n", "\n    "))


def main():
    con = sqlite3.connect(DB, timeout=300)
    con.executescript(SCHEMA)
    download(con, Pacer(10))
    report(con)


if __name__ == "__main__":
    main()
