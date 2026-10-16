"""Kalshi's daily 5pm ET BTC markets (KXBTCD) plus Deribit option trades around each one.

For every day since mid-December 2025: the "BTC above $X at 5pm ET" markets at strikes
that are multiples of $500 within 5% of BTC's price 20 hours before the close, their
hourly YES quotes over the last 26 hours, and every Deribit BTC option trade in the two
hours before each decision time (20 h and 6 h before the close), from Deribit's free
history server. Safe to re-run. Data: %LOCALAPPDATA%\\kalshi-btc15\\data\\btc_daily.sqlite.
Run: python btc_daily_fetch.py
"""
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

from config import DB_PATH, HOME, KALSHI_API
from fetch_data import NotFound, Pacer, get_json, num, ts

DB = HOME / "data" / "btc_daily.sqlite"
FIRST_DAY = date(2025, 12, 12)
DECISIONS_H = (20, 6)      # hours before the close
BAND, STEP = 0.05, 500     # strikes within 5% of BTC, multiples of $500
DERIBIT = "https://history.deribit.com/api/v2/public/get_last_trades_by_currency_and_time"

SCHEMA = """
CREATE TABLE IF NOT EXISTS markets (ticker TEXT PRIMARY KEY, event TEXT, close_ts INTEGER, strike REAL,
    result TEXT, source TEXT, candles_done INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS candles (ticker TEXT, ts INTEGER, bid REAL, ask REAL, PRIMARY KEY (ticker, ts));
CREATE TABLE IF NOT EXISTS deribit (trade_id TEXT PRIMARY KEY, ts INTEGER, instrument TEXT, expiry TEXT,
    strike REAL, kind TEXT, iv REAL, index_price REAL, amount REAL);
CREATE TABLE IF NOT EXISTS done (key TEXT PRIMARY KEY);
"""


def btc_at(con_btc, t):
    row = con_btc.execute("SELECT close FROM btc_1m WHERE ts <= ? ORDER BY ts DESC LIMIT 1", (t,)).fetchone()
    return row[0] if row else None


def event_markets(pacer, event):
    for source, path in (("live", "/markets?"), ("historical", "/historical/markets?")):
        ms = get_json(f"{KALSHI_API}{path}event_ticker={event}&limit=1000", pacer).get("markets") or []
        if ms:
            return source, ms
    return None, []


def deribit_trades(con, pacer, t_end):
    """All BTC option trades in the 2 hours before t_end (unix s)."""
    key = f"deribit:{t_end}"
    if con.execute("SELECT 1 FROM done WHERE key=?", (key,)).fetchone():
        return
    start, rows = (t_end - 7200) * 1000, []
    while True:
        r = get_json(f"{DERIBIT}?currency=BTC&kind=option&start_timestamp={start}&end_timestamp={t_end * 1000}"
                     f"&count=1000&sorting=asc", pacer)["result"]
        for t in r["trades"]:
            _, expiry, strike, kind = t["instrument_name"].split("-")
            rows.append((t["trade_id"], t["timestamp"] // 1000, t["instrument_name"], expiry, float(strike), kind,
                         t["iv"], t["index_price"], t["amount"]))
        if not r.get("has_more") or not r["trades"]:
            break
        start = r["trades"][-1]["timestamp"] + 1
    con.executemany("INSERT OR IGNORE INTO deribit VALUES (?,?,?,?,?,?,?,?,?)", rows)
    con.execute("INSERT OR IGNORE INTO done VALUES (?)", (key,))
    con.commit()


def main():
    con = sqlite3.connect(DB, timeout=300)
    con.executescript(SCHEMA)
    con_btc = sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True)
    kalshi, deribit = Pacer(3), Pacer(4)
    last_btc = con_btc.execute("SELECT MAX(ts) FROM btc_1m").fetchone()[0]
    d = FIRST_DAY
    while d <= datetime.now(timezone.utc).date() - timedelta(days=1):
        event = f"KXBTCD-{d:%y%b%d}17".upper()
        if not con.execute("SELECT 1 FROM done WHERE key=?", (event,)).fetchone():
            source, ms = event_markets(kalshi, event)
            if ms:
                close = ts(ms[0]["close_time"])
                spot = btc_at(con_btc, close - 20 * 3600) if close - 20 * 3600 <= last_btc else None
                rows = []
                for m in ms:
                    k = round(m.get("floor_strike") or 0)
                    if m.get("result") in ("yes", "no") and k % STEP == 0 and spot and abs(k / spot - 1) <= BAND:
                        rows.append((m["ticker"], event, close, float(m["floor_strike"]), m["result"], source))
                con.executemany("INSERT OR IGNORE INTO markets (ticker, event, close_ts, strike, result, source) "
                                "VALUES (?,?,?,?,?,?)", rows)
                for h in DECISIONS_H:
                    deribit_trades(con, deribit, close - h * 3600)
            con.execute("INSERT OR IGNORE INTO done VALUES (?)", (event,))
            con.commit()
            print(f"  {event}: {len(ms)} markets listed", flush=True)
        d += timedelta(days=1)

    todo = con.execute("SELECT ticker, close_ts, source FROM markets WHERE candles_done = 0").fetchall()
    print(f"hourly prices for {len(todo):,} markets", flush=True)

    def one(m):
        ticker, close, source = m
        path = (f"/historical/markets/{ticker}/candlesticks" if source == "historical"
                else f"/series/KXBTCD/markets/{ticker}/candlesticks")
        try:
            return ticker, get_json(f"{KALSHI_API}{path}?start_ts={close - 26 * 3600}&end_ts={close}"
                                    f"&period_interval=60", kalshi).get("candlesticks") or []
        except NotFound:
            return ticker, []
        except Exception as e:
            print(f"  skipped {ticker}: {e}", flush=True)
            return ticker, None

    start = time.monotonic()
    with ThreadPoolExecutor(4) as pool:
        for i in range(0, len(todo), 100):
            for ticker, cs in pool.map(one, todo[i:i + 100]):
                if cs is None:
                    continue
                con.executemany("INSERT OR REPLACE INTO candles VALUES (?,?,?,?)", [
                    (ticker, c["end_period_ts"], num(c.get("yes_bid") or {}, "close"),
                     num(c.get("yes_ask") or {}, "close")) for c in cs])
                con.execute("UPDATE markets SET candles_done = 1 WHERE ticker = ?", (ticker,))
            con.commit()
            done = min(i + 100, len(todo))
            print(f"  prices: {done:,}/{len(todo):,}, about "
                  f"{(len(todo) - done) / (done / (time.monotonic() - start)) / 60:.0f} min left", flush=True)
    print("Done.", flush=True)


if __name__ == "__main__":
    main()
