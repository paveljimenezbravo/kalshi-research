"""Coinbase 1-minute candles for the other coins' 15-minute markets (ETH, SOL, XRP, DOGE).

Each coin's candles go into the `btc_1m` table of that coin's own database (the table
name is historical: for KXETH15M it holds ETH-USD), so every BTC tool works unchanged
with KALSHI_SERIES=KXETH15M and friends. Safe to re-run; it only adds missing minutes.
Run: python coin_prices.py
"""
import sqlite3
import time
from datetime import datetime, timezone

from config import HOME
from fetch_data import SCHEMA, Pacer, get_json, iso

COINS = {"kxeth15m": "ETH-USD", "kxsol15m": "SOL-USD", "kxxrp15m": "XRP-USD", "kxdoge15m": "DOGE-USD"}
START = int(datetime(2026, 8, 1, tzinfo=timezone.utc).timestamp())  # two weeks of warm-up before the markets


def main():
    pacer = Pacer(5)
    end = int(time.time()) // 60 * 60
    for db, product in COINS.items():
        con = sqlite3.connect(HOME / "data" / f"{db}.sqlite")
        con.executescript(SCHEMA)
        lo, hi = con.execute("SELECT MIN(ts), MAX(ts) FROM btc_1m").fetchone()
        ranges = [(START, end)] if lo is None else [(START, lo), (hi + 60, end)]
        added = 0
        for a, b in ranges:
            t = a
            while t + 60 <= b:
                t2 = min(t + 300 * 60, b)
                rows = get_json(f"https://api.exchange.coinbase.com/products/{product}/candles"
                                f"?granularity=60&start={iso(t)}&end={iso(t2 - 60)}", pacer)
                # Coinbase rows are [time, low, high, open, close, volume]
                con.executemany("INSERT OR REPLACE INTO btc_1m VALUES (?,?,?,?,?,?)",
                                [(int(r[0]), r[3], r[2], r[1], r[4], r[5]) for r in rows])
                added += len(rows)
                t = t2
            con.commit()
        n, p0, p1 = con.execute("SELECT COUNT(*), MIN(close), MAX(close) FROM btc_1m").fetchone()
        print(f"{product}: +{added:,} minutes, {n:,} total, price {p0} to {p1}", flush=True)


if __name__ == "__main__":
    main()
