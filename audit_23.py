"""#34: audit of #23 ("sell the tails" of the daily 5pm BTC market, 6 hours before the close).

Five checks, written down before running (RESEARCH_LOG #34):
  1. Recompute #23 from the raw tables with code that shares nothing with btc_daily_test.py.
  2. Re-fetch random trades from Kalshi's API (quotes and result) and compare.
  3. Stale quotes: re-test with minute-level quotes no more than 5 minutes old at 11:00 ET.
     (A stale, too-high YES bid on a tail makes NO look cheaper than it was: fake profit.)
  4. Without the 5 best days, is it still positive?
  5. Were the days with no trades unusually volatile BTC days (which would flatter #23)?
Run: python audit_23.py   (downloads ~3,700 short minute-quote histories the first time)
"""
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd

from btc_daily_fetch import DB as DAILY_DB
from config import DB_PATH, HOME, KALSHI_API
from fetch_data import NotFound, Pacer, get_json, num

AUDIT_DB = HOME / "data" / "audit23.sqlite"
DECISION = 6 * 3600
rng = np.random.default_rng(23)


def ro(path):
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)


def raw():
    con = ro(DAILY_DB)
    markets = pd.read_sql("SELECT ticker, event, close_ts, strike, result, source FROM markets WHERE candles_done = 1", con)
    hourly = pd.read_sql("SELECT ticker, ts, bid, ask FROM candles WHERE bid IS NOT NULL AND ask IS NOT NULL", con)
    return markets, hourly


def trades(markets, quotes, max_age):
    """The #23 rule, written from scratch: one decision per market at close - 6 h."""
    by = {t: g.sort_values("ts") for t, g in quotes.groupby("ticker")}
    rows = []
    for m in markets.itertuples():
        t = m.close_ts - DECISION
        g = by.get(m.ticker)
        if g is None:
            continue
        g = g[g.ts <= t]
        if g.empty or t - g.ts.iloc[-1] > max_age:
            continue
        bid, ask = float(g.bid.iloc[-1]), float(g.ask.iloc[-1])
        if not (0 < bid and ask < 1):
            continue
        mid = (bid + ask) / 2
        if mid < 0.20:
            side, price, won = "NO", 1 - bid, m.result == "no"
        elif mid > 0.80:
            side, price, won = "YES", ask, m.result == "yes"
        else:
            continue
        rows.append({"ticker": m.ticker, "event": m.event, "close_ts": m.close_ts, "side": side, "price": price,
                     "bid": bid, "ask": ask, "won": int(won), "age": t - int(g.ts.iloc[-1]),
                     "pnl": int(won) - price - 0.07 * price * (1 - price)})
    return pd.DataFrame(rows)


def day_ci(t, level=95, n=4000):
    g = t.groupby("event").pnl.agg(["sum", "count"])
    idx = rng.integers(0, len(g), (n, len(g)))
    b = g["sum"].to_numpy()[idx].sum(1) / g["count"].to_numpy()[idx].sum(1)
    return np.percentile(b, (100 - level) / 2) * 100, np.percentile(b, 100 - (100 - level) / 2) * 100


def line(t, label):
    lo, hi = day_ci(t)
    return (f"{label}: {len(t):,} trades on {t.event.nunique()} days, won {t.won.mean():.1%} at {t.price.mean():.3f}, "
            f"{t.pnl.mean() * 100:+.2f}c [{lo:+.2f}, {hi:+.2f}]")


def minute_quotes(markets):
    con = sqlite3.connect(AUDIT_DB, timeout=300)
    con.executescript("CREATE TABLE IF NOT EXISTS m1 (ticker TEXT, ts INTEGER, bid REAL, ask REAL, PRIMARY KEY (ticker, ts));"
                      "CREATE TABLE IF NOT EXISTS done (ticker TEXT PRIMARY KEY);")
    have = {r[0] for r in con.execute("SELECT ticker FROM done")}
    todo = [m for m in markets.itertuples() if m.ticker not in have]
    pacer = Pacer(10)

    def one(m):
        t = m.close_ts - DECISION
        path = (f"/historical/markets/{m.ticker}/candlesticks" if m.source == "historical"
                else f"/series/KXBTCD/markets/{m.ticker}/candlesticks")
        try:
            cs = get_json(f"{KALSHI_API}{path}?start_ts={t - 1200}&end_ts={t}&period_interval=1", pacer).get("candlesticks") or []
            return m.ticker, cs
        except NotFound:
            return m.ticker, []
        except Exception as e:
            print(f"  skipped {m.ticker}: {e}", flush=True)
            return m.ticker, None

    if todo:
        print(f"  downloading minute quotes for {len(todo):,} markets", flush=True)
        with ThreadPoolExecutor(10) as pool:
            for i, (ticker, cs) in enumerate(pool.map(one, todo), 1):
                if cs is None:
                    continue
                con.executemany("INSERT OR REPLACE INTO m1 VALUES (?,?,?,?)", [
                    (ticker, c["end_period_ts"], num(c.get("yes_bid") or {}, "close"), num(c.get("yes_ask") or {}, "close"))
                    for c in cs])
                con.execute("INSERT OR IGNORE INTO done VALUES (?)", (ticker,))
                if i % 500 == 0:
                    con.commit()
                    print(f"  {i:,}/{len(todo):,}", flush=True)
        con.commit()
    return pd.read_sql("SELECT * FROM m1 WHERE bid IS NOT NULL AND ask IS NOT NULL", con)


def main():
    markets, hourly = raw()
    print(f"{len(markets):,} markets, {markets.event.nunique()} days\n")

    print("1. Independent recomputation (hourly quotes, up to 2 h old, as in #23)")
    t1 = trades(markets, hourly, 7200)
    print("   " + line(t1, "recomputed"))
    print("   original #23 (btc_daily_test.py): 1,847 trades, +2.48c [+1.65, +3.27]")
    print(f"   quote age at the decision: {np.mean(t1.age == 0):.0%} exactly on time, "
          f"{np.mean(t1.age <= 3600):.0%} within 1 h, oldest {t1.age.max() / 60:.0f} min")

    print("\n2. Spot checks against fresh API data")
    pacer = Pacer(4)
    for r in t1.sample(6, random_state=7).itertuples():
        src = markets.set_index("ticker").loc[r.ticker, "source"]
        mk = get_json(f"{KALSHI_API}{'/historical' if src == 'historical' else ''}/markets/{r.ticker}", pacer)
        mk = mk.get("market") or mk
        t = r.close_ts - DECISION
        path = (f"/historical/markets/{r.ticker}/candlesticks" if src == "historical"
                else f"/series/KXBTCD/markets/{r.ticker}/candlesticks")
        cs = get_json(f"{KALSHI_API}{path}?start_ts={t - 7200}&end_ts={t}&period_interval=60", pacer).get("candlesticks") or []
        last = [c for c in cs if c["end_period_ts"] <= t][-1]
        b, a = num(last.get("yes_bid") or {}, "close"), num(last.get("yes_ask") or {}, "close")
        ok = mk.get("result") == ("yes" if (r.side == "YES") == bool(r.won) else "no") and abs(b - r.bid) < 1e-9 and abs(a - r.ask) < 1e-9
        print(f"   {r.ticker}: stored bid/ask {r.bid:.2f}/{r.ask:.2f} vs API {b:.2f}/{a:.2f}; {r.side} won={r.won} vs API "
              f"result {mk.get('result')} -> {'match' if ok else 'MISMATCH'}")

    print("\n3. Fresh quotes: minute candles at 11:00 ET, at most 5 minutes old")
    m1 = minute_quotes(markets)
    t3 = trades(markets, m1, 300)
    print("   " + line(t3, "fresh quotes"))
    both = t1.merge(t3[["ticker", "bid", "ask", "pnl"]], on="ticker", suffixes=("_h", "_m"))
    changed = (both.bid_h != both.bid_m) | (both.ask_h != both.ask_m)
    print(f"   same markets in both: {len(both):,}; quote differs on {changed.mean():.0%} of them; "
          f"P&L hourly {both.pnl_h.mean() * 100:+.2f}c vs minute {both.pnl_m.mean() * 100:+.2f}c")
    for name, sub in (("1st half", t3[t3.close_ts < 1778544000]), ("2nd half", t3[t3.close_ts >= 1778544000])):
        print("   " + line(sub, name))
    print("   by spread: " + ", ".join(f"{s}: {g.pnl.mean() * 100:+.2f}c (n={len(g)})" for s, g in
                                     t3.groupby(pd.cut(t3.ask - t3.bid, [0, 0.0101, 0.0201, 1], labels=["1c", "2c", "3c+"]),
                                                observed=True)))

    print("\n4. Without the best days (fresh-quote trades)")
    day = t3.groupby("event").pnl.sum().sort_values(ascending=False)
    for k in (5, 10):
        rest = t3[~t3.event.isin(day.index[:k])]
        print(f"   without the {k} best days: {rest.pnl.mean() * 100:+.2f}c per trade over {len(rest):,} trades")
    print(f"   days with a profit: {np.mean(day > 0):.0%}; share of total profit from the best 10 days: "
          f"{day.iloc[:10].sum() / day.sum():.0%}")

    print("\n5. Were the skipped days unusually volatile?")
    btc = pd.read_sql("SELECT ts, close FROM btc_1m", ro(DB_PATH)).set_index("ts").close
    traded = set(t3.event)
    alld = pd.date_range("2025-12-12", "2026-10-12", freq="D")
    rows = []
    for d in alld:
        close = int(pd.Timestamp(f"{d:%Y-%m-%d} 17:00", tz="America/New_York").timestamp())
        a = btc.asof(close - DECISION - 60) if close - DECISION - 60 >= btc.index[0] else np.nan
        b = btc.asof(close - 60)
        ev = f"KXBTCD-{d:%y%b%d}17".upper()
        rows.append((ev, ev in traded, abs(np.log(b / a)) * 100 if a and b else np.nan))
    v = pd.DataFrame(rows, columns=["event", "traded", "move"]).dropna()
    print(f"   BTC's move from 11am to 5pm ET: traded days median {v[v.traded].move.median():.2f}% "
          f"(n={v.traded.sum()}), skipped days median {v[~v.traded].move.median():.2f}% (n={(~v.traded).sum()}); "
          f"biggest skipped-day move {v[~v.traded].move.max():.2f}%")


if __name__ == "__main__":
    main()
