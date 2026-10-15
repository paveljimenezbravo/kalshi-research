"""Live recorder: Kalshi's 15-minute BTC and ETH order books and trades, side by side with
BTC's and ETH's prices on Coinbase and Kraken, plus Bitstamp's BTC trades (three of the
exchanges behind Kalshi's settlement index).

    python recorder.py

Writes one compressed file per hour to %LOCALAPPDATA%\\kalshi-btc15\\live\\. Leave it
running: the edge tests in live_edge.py get better with every hour it records. It keeps
the PC awake while it runs (the screen can still turn off). No API key needed; this is
all public data.

Each line is JSON with "k" (kind) and "t" (this PC's clock when it arrived):
  mkt   a window's details: m (ticker), open, close, target
  book  Kalshi order book when it changed: m, yes/no = [[price, size], ...] best last, rtt (ms)
  trade a Kalshi trade: m, ts (Kalshi's time), p (YES price), q (contracts),
        side ("bid" = taker bought YES, "ask" = taker sold YES). About 75 a second.
  cb    Coinbase best bid/ask after each trade: p (BTC-USD or ETH-USD), bid, ask, last, ts (Coinbase's time)
  kr    Kraken best bid/ask when it changes: s (BTC/USD or ETH/USD), bid, ask, last
  bs    a Bitstamp trade: p, q, ts (Bitstamp's time), side
  bn    Binance futures best bid/ask (BTCUSDT, ETHUSDT perpetuals, the most-watched BTC price in
        the world), at most 10 a second: s, bid, ask, ts (Binance's time). Read-only public data.
  hmkt  an hourly "BTC above $X" market recorded alongside the window that ends on the hour:
        m, for (the 15-minute window), strike, close
  hbook its order book when it changed: m, yes/no
  err   something went wrong (the recorder retries on its own)
"""
import asyncio
import ctypes
import gzip
import json
import sys
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import aiohttp

from config import HOME, KALSHI_API

LIVE_DIR = HOME / "live"
POLL_EVERY = {"KXBTC15M": 0.25, "KXETH15M": 0.5}  # 15-minute series recorded, seconds between book polls
HOURLY_SERIES = "KXBTCD"  # "BTC above $X at the top of the hour": settles on the same 60-second average
ET = ZoneInfo("America/New_York")  # Kalshi's tickers use New York time
TRADES_EVERY = 1.0    # seconds between Kalshi trade polls
DEPTH = 10            # order-book levels kept per side
STATUS_EVERY = 60     # seconds between status lines


def ts(iso_text):
    return datetime.fromisoformat(iso_text.replace("Z", "+00:00")).timestamp()


class Writer:
    """Appends JSON lines to one gzip file per hour (UTC), flushing every few seconds so a
    crash or power cut loses at most a few seconds."""

    def __init__(self):
        self.hour, self.file, self.last_flush = None, None, 0.0
        self.counts = {}

    def write(self, kind, rec):
        now = time.time()
        hour = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d_%H")
        if hour != self.hour:
            self.close()
            LIVE_DIR.mkdir(parents=True, exist_ok=True)
            path, n = LIVE_DIR / f"{hour}.jsonl.gz", 1
            while path.exists():  # never append to a file an earlier run may have left half-written
                path, n = LIVE_DIR / f"{hour}_{n}.jsonl.gz", n + 1
            self.file = gzip.open(path, "wt", encoding="utf-8")
            self.hour = hour
        line = {"k": kind, "t": round(rec.pop("t", now), 3), **rec}
        self.file.write(json.dumps(line, separators=(",", ":")) + "\n")
        self.counts[kind] = self.counts.get(kind, 0) + 1
        if now - self.last_flush > 5:
            self.file.flush()
            self.last_flush = now

    def close(self):
        if self.file:
            self.file.close()
            self.file = None


class State:
    def __init__(self):
        self.windows = {}      # ticker -> details of windows open now (or just closed)
        self.book = {}         # ticker -> (best YES bid, best YES ask)
        self.btc = None        # latest Coinbase price
        self.hourly = {}       # 15-minute window ending on the hour -> hourly tickers recorded with it


async def get_json(session, url, tries=5):
    for attempt in range(tries):
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 429 and r.status < 500:
                    r.raise_for_status()
                    return await r.json()
        except (aiohttp.ClientConnectionError, asyncio.TimeoutError):
            pass
        await asyncio.sleep(0.5 * 2 ** attempt)
    raise RuntimeError(f"no answer after {tries} tries: {url}")


def live_tickers(state, grace=0.0):
    now = time.time()
    return [m for m, w in state.windows.items() if w["open"] <= now < w["close"] + grace]


async def kalshi_windows(state, session, writer):
    """Keeps track of which 15-minute window is open. Checks every 15 seconds, and right
    when the current window closes, so a new window is picked up within a second."""
    while True:
        try:
            for series in POLL_EVERY:
                data = await get_json(session, f"{KALSHI_API}/markets?series_ticker={series}&status=open&limit=20")
                for m in data.get("markets", []):
                    w = {"open": ts(m["open_time"]), "close": ts(m["close_time"]), "target": m.get("floor_strike")}
                    if state.windows.get(m["ticker"]) != w:
                        state.windows[m["ticker"]] = w
                        writer.write("mkt", {"m": m["ticker"], **w})
            now = time.time()
            for m in [m for m, w in state.windows.items() if w["close"] < now - 120]:
                del state.windows[m]
        except Exception as e:
            writer.write("err", {"src": "windows", "msg": str(e)[:300]})
        closes = [w["close"] for w in state.windows.values() if w["close"] > time.time()]
        wait = min([15.0] + [c - time.time() + 0.3 for c in closes])
        if any(state.windows[m]["target"] is None for m in live_tickers(state)):
            wait = min(wait, 1.0)  # a new window's target shows up a moment after it opens
        await asyncio.sleep(max(0.2, wait))


async def kalshi_book(state, session, writer):
    last, due = {}, {}
    while True:
        for m in live_tickers(state):
            if due.get(m, 0) > time.time():
                continue
            due[m] = time.time() + POLL_EVERY[m.split("-")[0]]
            try:
                t0 = time.time()
                data = await get_json(session, f"{KALSHI_API}/markets/{m}/orderbook?depth={DEPTH}")
                t1 = time.time()
                ob = data.get("orderbook_fp") or {}
                yes = [[float(p), float(q)] for p, q in ob.get("yes_dollars") or []]
                no = [[float(p), float(q)] for p, q in ob.get("no_dollars") or []]
                if last.get(m) != (yes, no):
                    last[m] = (yes, no)
                    writer.write("book", {"t": (t0 + t1) / 2, "m": m, "yes": yes, "no": no,
                                          "rtt": round((t1 - t0) * 1000)})
                    state.book[m] = (yes[-1][0] if yes else None, round(1 - no[-1][0], 4) if no else None)
            except Exception as e:
                writer.write("err", {"src": "book", "msg": str(e)[:300]})
        await asyncio.sleep(0.02)


async def kalshi_trades(state, session, writer):
    seen = {}  # ticker -> trade ids already written
    newest = {}  # ticker -> newest trade time seen
    while True:
        started = time.monotonic()
        for m in live_tickers(state, grace=30):  # keep polling 30 s past the close for late prints
            try:
                ids = seen.setdefault(m, set())
                since = int(newest.get(m, state.windows[m]["open"]) - 5)
                cursor, fresh = "", []
                while True:
                    url = f"{KALSHI_API}/markets/trades?ticker={m}&min_ts={since}&limit=1000"
                    data = await get_json(session, url + (f"&cursor={cursor}" if cursor else ""))
                    page = [x for x in data.get("trades", []) if x["trade_id"] not in ids]
                    fresh += page
                    cursor = data.get("cursor")
                    if not cursor or not page or len(data.get("trades", [])) < 1000:
                        break
                    await asyncio.sleep(0.25)  # catching up after a start mid-window: go easy on the API
                for x in sorted(fresh, key=lambda x: x["created_time"]):
                    ids.add(x["trade_id"])
                    t = ts(x["created_time"])
                    newest[m] = max(newest.get(m, 0), t)
                    writer.write("trade", {"m": m, "ts": t, "p": float(x["yes_price_dollars"]),
                                           "q": float(x["count_fp"]), "side": x.get("taker_book_side")})
            except Exception as e:
                writer.write("err", {"src": "trades", "msg": str(e)[:300]})
        for m in [m for m in seen if m not in state.windows]:
            seen.pop(m), newest.pop(m, None)
        await asyncio.sleep(max(0.0, TRADES_EVERY - (time.monotonic() - started)))


async def kalshi_hourly(state, session, writer):
    """For the 15-minute window that ends on the hour, also record the hourly 'BTC above $X'
    markets with the two strikes just below and just above the window's target. Both settle
    on the same 60-second average, so their prices have to line up."""
    last = {}
    while True:
        started = time.monotonic()
        for m in live_tickers(state):
            w = state.windows[m]
            if not m.startswith("KXBTC15M") or w["close"] % 3600 or w["target"] is None:
                continue
            if m not in state.hourly:
                event = f"{HOURLY_SERIES}-" + datetime.fromtimestamp(w["close"], ET).strftime("%y%b%d%H").upper()
                try:
                    data = await get_json(session, f"{KALSHI_API}/markets?event_ticker={event}&limit=1000")
                    same = [x for x in data.get("markets", []) if x.get("floor_strike") is not None]
                    below = sorted((x for x in same if x["floor_strike"] < w["target"]), key=lambda x: -x["floor_strike"])
                    above = sorted((x for x in same if x["floor_strike"] >= w["target"]), key=lambda x: x["floor_strike"])
                    picked = below[:2] + above[:2]
                    state.hourly[m] = [x["ticker"] for x in picked]
                    for x in picked:
                        writer.write("hmkt", {"m": x["ticker"], "for": m, "strike": x["floor_strike"], "close": w["close"]})
                except Exception as e:
                    writer.write("err", {"src": "hourly", "msg": str(e)[:300]})
                    continue
            for h in state.hourly[m]:
                try:
                    t0 = time.time()
                    data = await get_json(session, f"{KALSHI_API}/markets/{h}/orderbook?depth=5")
                    t1 = time.time()
                    ob = data.get("orderbook_fp") or {}
                    yes = [[float(p), float(q)] for p, q in ob.get("yes_dollars") or []]
                    no = [[float(p), float(q)] for p, q in ob.get("no_dollars") or []]
                    if last.get(h) != (yes, no):
                        last[h] = (yes, no)
                        writer.write("hbook", {"t": (t0 + t1) / 2, "m": h, "yes": yes, "no": no})
                except Exception as e:
                    writer.write("err", {"src": "hourly book", "msg": str(e)[:300]})
        for m in [m for m in state.hourly if m not in state.windows]:
            del state.hourly[m]
        await asyncio.sleep(max(0.0, 1.0 - (time.monotonic() - started)))


async def stream(session, writer, name, url, subscribe, handle):
    """Keeps one exchange websocket connected (reconnecting on any drop) and hands each message to `handle`."""
    backoff = 1
    while True:
        try:
            async with session.ws_connect(url, heartbeat=20, timeout=aiohttp.ClientWSTimeout(ws_close=10)) as ws:
                await ws.send_str(json.dumps(subscribe))
                backoff = 1
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        if handle(json.loads(msg.data)) == "reconnect":
                            break
                    elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        break
            writer.write("err", {"src": name, "msg": "disconnected, reconnecting"})
        except Exception as e:
            writer.write("err", {"src": name, "msg": str(e)[:300]})
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 60)


def exchange_tasks(state, session, writer):
    def coinbase(d):
        if d.get("type") == "ticker":
            if d.get("product_id") == "BTC-USD":
                state.btc = float(d["price"])
            writer.write("cb", {"p": d.get("product_id"), "bid": float(d["best_bid"]), "ask": float(d["best_ask"]),
                                "last": float(d["price"]), "ts": ts(d["time"]) if d.get("time") else None})

    def kraken(d):
        if d.get("channel") == "ticker" and d.get("data"):
            x = d["data"][0]
            writer.write("kr", {"s": x.get("symbol"), "bid": x["bid"], "ask": x["ask"], "last": x["last"]})

    def bitstamp(d):
        if d.get("event") == "bts:request_reconnect":
            return "reconnect"
        if d.get("event") == "trade":
            x = d["data"]
            writer.write("bs", {"p": x["price"], "q": x["amount"], "ts": int(x["microtimestamp"]) / 1e6,
                                "side": "buy" if x["type"] == 0 else "sell"})

    last_bn = {}

    def binance(d):
        x = d.get("data", d)  # combined streams wrap each message
        s, now = x.get("s"), time.time()
        if not s or now - last_bn.get(s, 0) < 0.1:  # ~270 updates/s: keep at most 10/s per symbol
            return
        last_bn[s] = now
        writer.write("bn", {"s": s, "bid": float(x["b"]), "ask": float(x["a"]), "ts": x.get("T", x.get("E", 0)) / 1000})

    return [
        stream(session, writer, "binance", "wss://fstream.binance.com/stream?streams=btcusdt@bookTicker/"
                                           "ethusdt@bookTicker", {"method": "LIST_SUBSCRIPTIONS", "id": 1}, binance),
        stream(session, writer, "coinbase", "wss://ws-feed.exchange.coinbase.com",
               {"type": "subscribe", "product_ids": ["BTC-USD", "ETH-USD"], "channels": ["ticker"]}, coinbase),
        stream(session, writer, "kraken", "wss://ws.kraken.com/v2",
               {"method": "subscribe", "params": {"channel": "ticker", "symbol": ["BTC/USD", "ETH/USD"],
                                                  "event_trigger": "bbo"}}, kraken),
        stream(session, writer, "bitstamp", "wss://ws.bitstamp.net",
               {"event": "bts:subscribe", "data": {"channel": "live_trades_btcusd"}}, bitstamp),
    ]


async def status(state, writer):
    previous = {}
    while True:
        await asyncio.sleep(STATUS_EVERY)
        counts = {k: v - previous.get(k, 0) for k, v in writer.counts.items()}
        previous = dict(writer.counts)
        live = [m for m in live_tickers(state) if m.startswith("KXBTC15M")]
        book = state.book.get(live[0]) if live else None
        where = (f"{live[0]} YES {book[0]:.3f}/{book[1]:.3f}" if live and book and None not in book
                 else "waiting for a window")
        btc = f"BTC ${state.btc:,.2f}" if state.btc else "BTC ?"
        per_min = ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
        try:
            print(f"{datetime.now():%H:%M:%S}  {where}  {btc}  last minute: {per_min}", flush=True)
        except OSError:
            pass  # the console or log went away; keep recording regardless


def keep_awake(on):
    """Ask Windows not to sleep while recording (the display may still turn off)."""
    if sys.platform == "win32":
        flags = 0x80000000 | (0x00000001 if on else 0)  # ES_CONTINUOUS | ES_SYSTEM_REQUIRED
        ctypes.windll.kernel32.SetThreadExecutionState(flags)


async def main():
    state, writer = State(), Writer()
    print(f"Recording to {LIVE_DIR}  (Ctrl+C or close this window to stop)", flush=True)
    keep_awake(True)
    try:
        async with aiohttp.ClientSession(headers={"User-Agent": "btc15-recorder/1.0"}) as session:
            await asyncio.gather(kalshi_windows(state, session, writer), kalshi_book(state, session, writer),
                                 kalshi_trades(state, session, writer), kalshi_hourly(state, session, writer),
                                 status(state, writer), *exchange_tasks(state, session, writer))
    finally:
        writer.close()
        keep_awake(False)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Stopped.")
