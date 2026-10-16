"""Download Kalshi's 15-minute BTC markets and BTC's 1-minute price into a local database.

    python fetch_data.py             # everything Kalshi has (back to Dec 2025)
    python fetch_data.py --days 30   # just the last 30 days

Run it again any time to add the newest windows. It's safe to stop (Ctrl+C)
and re-run: anything already downloaded is skipped. No API key needed; this is
all public market data. Standard library only.
"""
import argparse
import json
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from config import COINBASE_API, DB_PATH, KALSHI_API, SERIES

SCHEMA = """
CREATE TABLE IF NOT EXISTS markets (       -- one row per 15-minute window
    ticker TEXT PRIMARY KEY,
    open_ts INTEGER NOT NULL,              -- window start, unix seconds
    close_ts INTEGER NOT NULL,             -- window end
    target REAL,                           -- BTC price to beat
    final REAL,                            -- BTC settlement value (60-second BRTI average)
    result TEXT NOT NULL,                  -- 'yes' = BTC finished at or above target
    volume REAL,
    source TEXT NOT NULL,                  -- which Kalshi API listed it: 'live' or 'historical'
    candles_done INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS candles (       -- one row per window per minute
    ticker TEXT NOT NULL,
    minute INTEGER NOT NULL,               -- 1..15: the minute of the window that just ended
    yes_bid REAL, yes_ask REAL,            -- best YES quotes at the end of that minute, dollars
    yes_bid_low REAL, yes_bid_high REAL,
    yes_ask_low REAL, yes_ask_high REAL,
    last REAL, volume REAL, open_interest REAL,
    PRIMARY KEY (ticker, minute)
);
CREATE TABLE IF NOT EXISTS btc_1m (        -- Coinbase BTC-USD 1-minute candles
    ts INTEGER PRIMARY KEY,                -- minute start, unix seconds
    open REAL, high REAL, low REAL, close REAL, volume REAL
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

UPSERT_MARKET = """
INSERT INTO markets (ticker, open_ts, close_ts, target, final, result, volume, source)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(ticker) DO UPDATE SET
    final = excluded.final, result = excluded.result,
    volume = excluded.volume, source = excluded.source
"""


class Pacer:
    """Spaces out requests so all threads together stay under `per_sec`."""

    def __init__(self, per_sec):
        self.gap = 1 / per_sec
        self.next = 0.0
        self.lock = threading.Lock()

    def wait(self):
        with self.lock:
            now = time.monotonic()
            slot = max(now, self.next)
            self.next = slot + self.gap
        time.sleep(slot - now)


class NotFound(Exception):
    pass


def kalshi_signer():
    """Signed reads get their own (larger) rate limit than anonymous ones. Uses the API key
    from kalshi_auth.py's secrets folder if there is one; anonymous otherwise."""
    try:
        from kalshi_auth import auth_headers, load_credentials
        creds = load_credentials()
    except Exception:  # no key, or the cryptography package isn't installed
        return None
    return (lambda path: auth_headers(creds, "GET", path)) if creds else None


SIGN = kalshi_signer()


def get_json(url, pacer, tries=6):
    """GET with retries on rate limits (429), server errors and dropped connections."""
    for attempt in range(tries):
        pacer.wait()
        headers = {"User-Agent": "btc15-backtest/1.0"}
        if SIGN and url.startswith(KALSHI_API):
            headers.update(SIGN(urllib.parse.urlparse(url).path))  # signed fresh on every try
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise NotFound(url) from e
            if e.code != 429 and e.code < 500:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            pass
        time.sleep(min(30, 2 ** attempt))
    raise RuntimeError(f"Gave up after {tries} tries: {url}")


def ts(iso_text):
    return int(datetime.fromisoformat(iso_text.replace("Z", "+00:00")).timestamp())


def iso(unix):
    return datetime.fromtimestamp(unix, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def num(d, key):
    """Kalshi sends numbers as strings like "0.6100". The live API names them
    key_dollars / key_fp; the historical API uses the bare key."""
    for k in (key + "_dollars", key + "_fp", key):
        v = d.get(k)
        if v not in (None, ""):
            return float(v)
    return None


def meta(con, key):
    row = con.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def market_row(m, source):
    if m.get("result") not in ("yes", "no"):
        return None
    final = str(m.get("expiration_value") or "").replace(",", "")  # sometimes "77,362.10"
    return (m["ticker"], ts(m["open_time"]), ts(m["close_time"]), m.get("floor_strike"),
            float(final) if final else None, m["result"], num(m, "volume"), source)


def list_markets(con, pacer, since):
    """Page through every settled window: the live API first, then the historical archive."""
    for source, path in (("live", "/markets?status=settled&"), ("historical", "/historical/markets?")):
        done_since = meta(con, "historical_listed_since")
        if source == "historical" and done_since is not None and int(done_since) <= since:
            print("  historical list: already downloaded", flush=True)
            continue
        cursor, count = "", 0
        while True:
            url = f"{KALSHI_API}{path}series_ticker={SERIES}&limit=1000"
            if cursor:
                url += f"&cursor={cursor}"
            page = get_json(url, pacer)
            markets = page.get("markets") or []
            rows = [r for r in (market_row(m, source) for m in markets) if r and r[2] >= since]
            con.executemany(UPSERT_MARKET, rows)
            count += len(rows)
            cursor = page.get("cursor")
            # Both lists come newest first, so stop once we're past `since`.
            if not cursor or not markets or ts(markets[-1]["close_time"]) < since:
                break
        if source == "historical":
            con.execute("INSERT OR REPLACE INTO meta VALUES ('historical_listed_since', ?)", (str(since),))
        con.commit()
        print(f"  {source} list: {count:,} settled windows", flush=True)


def save_candles(con, market, candles):
    ticker, open_ts, _close_ts = market
    rows = []
    for c in candles:
        minute = (c["end_period_ts"] - open_ts) // 60
        if 1 <= minute <= 15:
            bid, ask, last = c.get("yes_bid") or {}, c.get("yes_ask") or {}, c.get("price") or {}
            rows.append((ticker, minute, num(bid, "close"), num(ask, "close"),
                         num(bid, "low"), num(bid, "high"), num(ask, "low"), num(ask, "high"),
                         num(last, "close"), num(c, "volume"), num(c, "open_interest")))
    con.executemany("INSERT OR REPLACE INTO candles VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.execute("UPDATE markets SET candles_done = 1 WHERE ticker = ?", (ticker,))


def live_candles(con, pacer, markets):
    """The live API returns several windows' minute candles per request: at most
    100 windows and 10,000 candles, counted as windows x minutes spanned (about
    25 back-to-back windows). Returns the windows it had nothing for (they've
    moved to the archive)."""
    missing, i = [], 0
    while i < len(markets):
        lo, hi, j = markets[i][1], markets[i][2], i + 1
        while j < len(markets) and j - i < 100:
            lo2, hi2 = min(lo, markets[j][1]), max(hi, markets[j][2])
            if (j + 1 - i) * (hi2 - lo2) // 60 > 10000:
                break
            lo, hi, j = lo2, hi2, j + 1
        chunk = markets[i:j]
        url = (f"{KALSHI_API}/markets/candlesticks?market_tickers={','.join(m[0] for m in chunk)}"
               f"&start_ts={lo}&end_ts={hi}&period_interval=1")
        got = {d["market_ticker"]: d.get("candlesticks") or [] for d in get_json(url, pacer).get("markets", [])}
        for m in chunk:
            if got.get(m[0]):
                save_candles(con, m, got[m[0]])
            else:
                missing.append(m)
        con.commit()
        if j // 1000 > i // 1000 or j == len(markets):
            print(f"  recent windows: {j:,}/{len(markets):,}", flush=True)
        i = j
    return missing


def historical_candles(con, pacer, markets, workers=8):
    """The archive serves one window per request, so this is the slow part. Each request
    takes about a second, so several run at once; the pacer still caps the total rate."""
    def one(m):
        url = (f"{KALSHI_API}/historical/markets/{m[0]}/candlesticks"
               f"?start_ts={m[1]}&end_ts={m[2]}&period_interval=1")
        try:
            return m, get_json(url, pacer).get("candlesticks") or []
        except NotFound:
            return m, []
        except Exception as e:  # leave it for the next run
            print(f"  skipped {m[0]} for now: {e}", flush=True)
            return m, None

    start = time.monotonic()
    with ThreadPoolExecutor(workers) as pool:
        for i in range(0, len(markets), 200):
            for m, candles in pool.map(one, markets[i:i + 200]):
                if candles is not None:
                    save_candles(con, m, candles)
            con.commit()
            done = min(i + 200, len(markets))
            if done % 1000 == 0 or done == len(markets):
                left = (len(markets) - done) / (done / (time.monotonic() - start)) / 60
                print(f"  older windows: {done:,}/{len(markets):,}, about {left:.0f} min left", flush=True)


def btc_prices(con, pacer, start):
    """Coinbase BTC-USD 1-minute candles, 300 per request. Kalshi settles on
    CF Benchmarks' BRTI, which tracks Coinbase closely but isn't identical."""
    end = int(time.time()) // 60 * 60
    lo, hi = con.execute("SELECT MIN(ts), MAX(ts) FROM btc_1m").fetchone()
    ranges = [(start, end)] if lo is None else [(start, lo), (hi + 60, end)]
    added = 0
    for a, b in ranges:
        t = a
        while t + 60 <= b:
            t2 = min(t + 300 * 60, b)
            rows = get_json(f"{COINBASE_API}/candles?granularity=60&start={iso(t)}&end={iso(t2 - 60)}", pacer)
            # Coinbase rows are [time, low, high, open, close, volume]
            con.executemany("INSERT OR REPLACE INTO btc_1m VALUES (?,?,?,?,?,?)",
                            [(int(r[0]), r[3], r[2], r[1], r[4], r[5]) for r in rows])
            added += len(rows)
            t = t2
            if added and added % 30000 < 300:
                con.commit()
                print(f"  BTC minutes: {added:,}", flush=True)
    con.commit()
    print(f"  BTC minutes added: {added:,}", flush=True)


def main():
    ap = argparse.ArgumentParser(description="Download Kalshi 15-minute BTC data.")
    ap.add_argument("--days", type=float, help="only the last N days (default: all of it)")
    ap.add_argument("--skip-btc", action="store_true", help="don't download Coinbase BTC prices")
    ap.add_argument("--rate", type=float, default=10, help="Kalshi requests per second (lower it while "
                                                          "the recorder runs; default 10)")
    args = ap.parse_args()
    since = int(time.time() - args.days * 86400) if args.days else 0

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA journal_mode=WAL")  # lets the dashboard read while this writes
    con.executescript(SCHEMA)
    kalshi, coinbase = Pacer(args.rate), Pacer(6)
    print(f"Database: {DB_PATH}", flush=True)
    try:
        print("1/4 Listing settled windows...", flush=True)
        list_markets(con, kalshi, since)
        todo = con.execute("SELECT ticker, open_ts, close_ts, source FROM markets "
                           "WHERE candles_done = 0 AND close_ts >= ? ORDER BY close_ts DESC", (since,)).fetchall()
        print("2/4 Minute-by-minute odds for recent windows...", flush=True)
        moved = live_candles(con, kalshi, [m[:3] for m in todo if m[3] == "live"])
        if not args.skip_btc:
            print("3/4 BTC price from Coinbase (1-minute)...", flush=True)
            first = con.execute("SELECT MIN(open_ts) FROM markets WHERE close_ts >= ?", (since,)).fetchone()[0]
            btc_prices(con, coinbase, (first or since) // 60 * 60 - 86400)  # a day extra for volatility warm-up
        print("4/4 Minute-by-minute odds for older windows (the slow part)...", flush=True)
        historical_candles(con, kalshi, moved + [m[:3] for m in todo if m[3] == "historical"])
    except KeyboardInterrupt:
        con.commit()
        print("\nStopped. Run it again to pick up where it left off.")
        return

    n, done, first, last = con.execute(
        "SELECT COUNT(*), SUM(candles_done), MIN(open_ts), MAX(close_ts) FROM markets").fetchone()
    print(f"\nDone: {done:,} of {n:,} windows have minute data, "
          f"{datetime.fromtimestamp(first):%b %d, %Y} to {datetime.fromtimestamp(last):%b %d, %Y %I:%M %p}.")


if __name__ == "__main__":
    main()
