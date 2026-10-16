"""Official airport/park weather observations (METAR) for the 7 weather-market stations.

From Iowa State's free ASOS archive: every routine and special report, with the precise
temperature (the T-group, tenths of a degree C) and the 6-hour maximum groups. Builds,
for each station and climate day, the highest temperature observed so far at each hour.

The climate day behind Kalshi's settlement (the NWS daily climate report) runs midnight
to midnight LOCAL STANDARD time, so in summer it is 1am-1am on the clock.
Data goes into weather.sqlite (tables obs and so_far). Run: python weather_obs.py
"""
import csv
import io
import re
import sqlite3
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone

from weather_fetch import DB, SINCE

# series -> (IEM station id, hours from UTC to local STANDARD time)
STATIONS = {"KXHIGHNY": ("NYC", -5), "KXHIGHCHI": ("MDW", -6), "KXHIGHMIA": ("MIA", -5), "KXHIGHAUS": ("AUS", -6),
            "KXHIGHLAX": ("LAX", -8), "KXHIGHDEN": ("DEN", -7), "KXHIGHPHIL": ("PHL", -5)}
HOURS = range(9, 24)  # decision times, local standard time

SCHEMA = """
CREATE TABLE IF NOT EXISTS obs (series TEXT, ts INTEGER, temp_f REAL, max6_f REAL, PRIMARY KEY (series, ts));
CREATE TABLE IF NOT EXISTS so_far (   -- highest temperature observed in the climate day up to each hour
    series TEXT, day TEXT, hour INTEGER, max_f REAL, now_f REAL, n_obs INTEGER, PRIMARY KEY (series, day, hour));
"""
T_GROUP = re.compile(r"\bT([01])(\d{3})[01]\d{3}\b")
MAX6 = re.compile(r"\b1([01])(\d{3})\b")


def c_to_f(sign, tenths):
    c = (-1 if sign == "1" else 1) * int(tenths) / 10
    return c * 9 / 5 + 32


def download(station, start, end):
    url = ("https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py?"
           f"station={station}&data=tmpf&data=metar&tz=Etc/UTC&format=onlycomma&latlon=no&missing=M"
           f"&report_type=3&report_type=4&year1={start.year}&month1={start.month}&day1={start.day}"
           f"&year2={end.year}&month2={end.month}&day2={end.day}")
    for attempt in range(5):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "btc15-backtest/1.0"}),
                                        timeout=300) as r:
                return list(csv.DictReader(io.StringIO(r.read().decode())))
        except Exception as e:  # the archive is free and sometimes busy
            print(f"  retry {station}: {e}", flush=True)
            time.sleep(10 * (attempt + 1))
    raise RuntimeError(f"gave up on {station}")


def parse(rows):
    out = []
    for r in rows:
        ts = int(datetime.strptime(r["valid"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp())
        metar = r.get("metar") or ""
        rmk = metar.split(" RMK ", 1)[1] if " RMK " in metar else ""
        t = T_GROUP.search(rmk)
        temp = c_to_f(*t.groups()) if t else (float(r["tmpf"]) if r["tmpf"] not in ("M", "") else None)
        m6 = MAX6.search(rmk)
        out.append((ts, temp, c_to_f(*m6.groups()) if m6 else None))
    return out


def so_far(obs, offset):
    """Running max per climate day at each decision hour (local standard time)."""
    days = {}
    for ts, temp, max6 in sorted(obs):
        lst = datetime.fromtimestamp(ts, timezone.utc) + timedelta(hours=offset)
        day, hour = lst.date().isoformat(), lst.hour + lst.minute / 60
        days.setdefault(day, []).append((hour, temp, max6))
    rows = []
    for day, xs in days.items():
        for h in HOURS:
            seen = [(hr, t, m6) for hr, t, m6 in xs if hr <= h]
            temps = [t for _, t, _ in seen if t is not None]
            # a 6-hour max counts only if its whole window is inside the day (reported at 6:00 or later)
            m6s = [m6 for hr, _, m6 in seen if m6 is not None and hr >= 6]
            if temps:
                rows.append((day, h, max(temps + m6s), temps[-1], len(temps)))
    return rows


def main():
    con = sqlite3.connect(DB)
    con.executescript(SCHEMA)
    end = datetime.now(timezone.utc).date() + timedelta(days=1)
    for series, (station, offset) in STATIONS.items():
        have = con.execute("SELECT MAX(ts) FROM obs WHERE series=?", (series,)).fetchone()[0]
        start = (datetime.fromtimestamp(have, timezone.utc).date() if have else SINCE - timedelta(days=1))
        rows = []
        y = start
        while y < end:  # a year per request keeps each download modest
            y2 = min(date(y.year + 1, 1, 1), end)
            rows += parse(download(station, y, y2))
            y = y2
        con.executemany("INSERT OR REPLACE INTO obs VALUES (?,?,?,?)", [(series, *r) for r in rows])
        obs = con.execute("SELECT ts, temp_f, max6_f FROM obs WHERE series=?", (series,)).fetchall()
        con.executemany("INSERT OR REPLACE INTO so_far VALUES (?,?,?,?,?,?)",
                        [(series, *r) for r in so_far(obs, offset)])
        con.commit()
        print(f"{series} ({station}): {len(rows):,} new reports, {len(obs):,} total", flush=True)


if __name__ == "__main__":
    main()
