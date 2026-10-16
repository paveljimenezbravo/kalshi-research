"""Download Kalshi's daily high-temperature markets plus the forecasts that existed before them.

For each city: every settled market (bracket, result, official high), Kalshi's hourly
YES quotes over the market's life, and Open-Meteo's archived forecasts of each hour's
temperature as issued 1 and 2 days earlier (NBM, GFS, ECMWF). Safe to re-run; it only
fetches what's missing. Data goes to %LOCALAPPDATA%\\kalshi-btc15\\data\\weather.sqlite.
"""
import sqlite3
import time
import urllib.parse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

from config import HOME, KALSHI_API
from fetch_data import NotFound, Pacer, get_json, num, ts

DB = HOME / "data" / "weather.sqlite"
SINCE = date(2025, 1, 1)  # Open-Meteo's archived forecasts start around here
# Settlement stations (Kalshi settles on the official daily high at these spots).
CITIES = {
    "KXHIGHNY": ("New York (Central Park)", 40.7790, -73.9693, "America/New_York"),
    "KXHIGHCHI": ("Chicago (Midway)", 41.7868, -87.7522, "America/Chicago"),
    "KXHIGHMIA": ("Miami", 25.7881, -80.3169, "America/New_York"),
    "KXHIGHAUS": ("Austin", 30.1831, -97.6799, "America/Chicago"),
    "KXHIGHLAX": ("Los Angeles (LAX)", 33.9382, -118.3866, "America/Los_Angeles"),
    "KXHIGHDEN": ("Denver", 39.8466, -104.6562, "America/Denver"),
    "KXHIGHPHIL": ("Philadelphia", 39.8733, -75.2268, "America/New_York"),
}
MODELS = ["ncep_nbm_conus", "gfs_seamless", "ecmwf_ifs025"]
MONTHS = {m: i for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
                                      "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}

SCHEMA = """
CREATE TABLE IF NOT EXISTS markets (
    ticker TEXT PRIMARY KEY, series TEXT, event TEXT, day TEXT,  -- day = local date of the high
    strike_type TEXT, floor REAL, cap REAL,  -- between: floor..cap; greater: > floor; less: < cap
    result TEXT, high REAL,                  -- high = official settlement value
    open_ts INTEGER, close_ts INTEGER, volume REAL, source TEXT,
    candles_done INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS candles (     -- hourly YES quotes at the end of each hour
    ticker TEXT, ts INTEGER, bid REAL, ask REAL, volume REAL, PRIMARY KEY (ticker, ts)
);
CREATE TABLE IF NOT EXISTS forecasts (   -- forecast daily high (max of hourly), issued N days earlier
    series TEXT, day TEXT, model TEXT, lead INTEGER, high REAL, PRIMARY KEY (series, day, model, lead)
);
"""


def event_day(event):
    """KXHIGHNY-26OCT14 -> 2026-10-14"""
    code = event.split("-")[1]
    return date(2000 + int(code[:2]), MONTHS[code[2:5]], int(code[5:7])).isoformat()


def list_markets(con, pacer, series):
    n = 0
    for source, path in (("live", "/markets?status=settled&"), ("historical", "/historical/markets?")):
        cursor = ""
        while True:
            page = get_json(f"{KALSHI_API}{path}series_ticker={series}&limit=1000"
                            + (f"&cursor={cursor}" if cursor else ""), pacer)
            ms = page.get("markets") or []
            rows = []
            for m in ms:
                if m.get("result") not in ("yes", "no"):
                    continue
                day = event_day(m["event_ticker"])
                if day < SINCE.isoformat():
                    continue
                try:
                    high = float(str(m.get("expiration_value") or "").replace(",", ""))
                except ValueError:  # a few markets report text here instead of the temperature
                    high = None
                rows.append((m["ticker"], series, m["event_ticker"], day, m.get("strike_type"),
                             m.get("floor_strike"), m.get("cap_strike"), m["result"],
                             high, ts(m["open_time"]), ts(m["close_time"]),
                             num(m, "volume"), source))
            con.executemany("""INSERT INTO markets (ticker, series, event, day, strike_type, floor, cap, result,
                               high, open_ts, close_ts, volume, source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                               ON CONFLICT(ticker) DO NOTHING""", rows)
            n += len(rows)
            cursor = page.get("cursor")
            if not cursor or not ms or event_day(ms[-1]["event_ticker"]) < SINCE.isoformat():
                break
    con.commit()
    print(f"  {series}: {n:,} settled markets listed", flush=True)


def save_candles(con, ticker, candles):
    con.executemany("INSERT OR REPLACE INTO candles VALUES (?,?,?,?,?)", [
        (ticker, c["end_period_ts"], num(c.get("yes_bid") or {}, "close"), num(c.get("yes_ask") or {}, "close"),
         num(c, "volume")) for c in candles])
    con.execute("UPDATE markets SET candles_done = 1 WHERE ticker = ?", (ticker,))


def candles(con, pacer):
    todo = con.execute("SELECT ticker, series, open_ts, close_ts, source FROM markets WHERE candles_done = 0 "
                       "ORDER BY close_ts").fetchall()
    print(f"  hourly prices for {len(todo):,} markets", flush=True)

    def one(m):
        ticker, series, lo, hi, source = m
        path = (f"/historical/markets/{ticker}/candlesticks" if source == "historical"
                else f"/series/{series}/markets/{ticker}/candlesticks")
        try:
            return ticker, get_json(f"{KALSHI_API}{path}?start_ts={lo}&end_ts={hi}&period_interval=60",
                                    pacer).get("candlesticks") or []
        except NotFound:
            return ticker, []
        except Exception as e:
            print(f"  skipped {ticker} for now: {e}", flush=True)
            return ticker, None

    start = time.monotonic()
    with ThreadPoolExecutor(12) as pool:
        for i in range(0, len(todo), 200):
            for ticker, cs in pool.map(one, todo[i:i + 200]):
                if cs is not None:
                    save_candles(con, ticker, cs)
            con.commit()
            done = min(i + 200, len(todo))
            left = (len(todo) - done) / (done / (time.monotonic() - start)) / 60
            print(f"  prices: {done:,}/{len(todo):,}, about {left:.0f} min left", flush=True)


def forecasts(con, pacer):
    """Daily high = max of the hourly forecast over the local day, as issued 1 and 2 days before."""
    end = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
    for series, (name, lat, lon, tz) in CITIES.items():
        for model in MODELS:
            have = con.execute("SELECT MAX(day) FROM forecasts WHERE series=? AND model=?", (series, model)).fetchone()[0]
            start = (date.fromisoformat(have) + timedelta(days=1)).isoformat() if have else SINCE.isoformat()
            if start > end:
                continue
            q = urllib.parse.urlencode({
                "latitude": lat, "longitude": lon, "timezone": tz, "temperature_unit": "fahrenheit",
                "hourly": "temperature_2m_previous_day1,temperature_2m_previous_day2",
                "start_date": start, "end_date": end, "models": model})
            try:
                h = get_json(f"https://previous-runs-api.open-meteo.com/v1/forecast?{q}", pacer)["hourly"]
            except Exception as e:
                print(f"  {series} {model}: {e}", flush=True)
                continue
            highs = defaultdict(dict)
            for i, t in enumerate(h["time"]):
                for lead in (1, 2):
                    v = h[f"temperature_2m_previous_day{lead}"][i]
                    if v is not None:
                        d = highs[(t[:10], lead)]
                        d["max"] = max(d.get("max", -999), v)
                        d["n"] = d.get("n", 0) + 1
            rows = [(series, day, model, lead, d["max"]) for (day, lead), d in highs.items() if d["n"] >= 20]
            con.executemany("INSERT OR REPLACE INTO forecasts VALUES (?,?,?,?,?)", rows)
            con.commit()
            print(f"  {series} {model}: {len(rows):,} forecast days", flush=True)


def main():
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB, timeout=300)  # weather_obs.py may be writing at the same time
    con.executescript(SCHEMA)
    kalshi, meteo = Pacer(12), Pacer(2)
    print("Forecasts (Open-Meteo)...", flush=True)
    forecasts(con, meteo)
    print("Kalshi markets...", flush=True)
    for series in CITIES:
        list_markets(con, kalshi, series)
    candles(con, kalshi)
    print("Done.", flush=True)


if __name__ == "__main__":
    main()
