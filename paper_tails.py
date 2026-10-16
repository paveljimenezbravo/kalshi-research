"""Paper trading for #23/#28: sell the tails of today's 5pm ET crypto markets, 6 hours before the close.

    python paper_tails.py                 # settle earlier paper trades, log today's, print the record
    python paper_tails.py log --force     # log even outside the 10:30am-11:30am ET window
    python paper_tails.py report          # just settle and print the record

At about 11am ET it reads the LIVE order book of today's 5pm events for BTC, ETH, SOL, XRP
and DOGE, using each coin's backtest strike selection (BTC: $500 multiples within 5% of
BTC's price, #23; the others: the 14 strikes closest to the previous day's settlement,
within 4%, #28), and records the trade the rule would make: NO when YES is priced under
20c (paying 1 - the best YES bid), YES when it's over 80c (paying the best YES ask).
Nothing is sent to Kalshi: it only writes %LOCALAPPDATA%\\kalshi-btc15\\paper\\tails.csv,
before the result is known, and fills in results after the close. Backtest expectation
after the fee: about +2.5c per contract on BTC and about +1c on the other coins.
"""
import csv
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from config import HOME, KALSHI_API
from fetch_data import Pacer, get_json

ET = ZoneInfo("America/New_York")
LEDGER = HOME / "paper" / "tails.csv"
FIELDS = ["logged_at", "event", "ticker", "strike", "btc", "side", "price", "fee", "yes_bid", "yes_ask",
          "size_at_price", "result", "won", "pnl"]  # "btc" holds the reference price (coin price or prior settle)
SERIES = ("KXBTCD", "KXETHD", "KXSOLD", "KXXRPD", "KXDOGED")
PACER = Pacer(4)
fee = lambda p: 0.07 * p * (1 - p)


def read():
    if not LEDGER.exists():
        return []
    with LEDGER.open(newline="") as f:
        return list(csv.DictReader(f))


def write(rows):
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)


def best(levels):
    """Best price and its size from an order-book side ([[price, size], ...])."""
    return max(((float(p), float(q)) for p, q in levels or []), default=(0.0, 0.0))


def strike(m):
    """floor_strike, or for 'custom' markets (DOGE) the number in the ticker: ...-T0.0449999 -> 0.0449999."""
    if m.get("floor_strike"):
        return m["floor_strike"]
    try:
        return float(m["ticker"].rsplit("-T", 1)[1])
    except (IndexError, ValueError):
        return None


def candidates(series, now):
    """Today's markets for one coin, chosen exactly as in that coin's backtest, plus the reference price."""
    event = f"{series}-{now:%y%b%d}17".upper()
    markets = get_json(f"{KALSHI_API}/markets?event_ticker={event}&limit=200", PACER).get("markets") or []
    if series == "KXBTCD":
        ref = float(get_json("https://api.exchange.coinbase.com/products/BTC-USD/ticker", PACER)["price"])
        picked = [m for m in markets if strike(m) and round(strike(m)) % 500 == 0 and abs(round(strike(m)) / ref - 1) <= 0.05]
    else:
        prev = f"{series}-{now - timedelta(days=1):%y%b%d}17".upper()
        pm = get_json(f"{KALSHI_API}/markets?event_ticker={prev}&limit=5", PACER).get("markets") or []
        try:
            ref = float(str(pm[0].get("expiration_value") or "").replace(",", ""))
        except (IndexError, ValueError):
            return event, None, []
        near = [m for m in markets if strike(m) and abs(strike(m) / ref - 1) <= 0.04]
        picked = sorted(near, key=lambda m: abs(strike(m) - ref))[:14]
    return event, ref, picked


def log(rows, force=False):
    now = datetime.now(ET)
    close = now.replace(hour=17, minute=0, second=0, microsecond=0)
    hours_left = (close - now).total_seconds() / 3600
    if not 5.5 <= hours_left <= 6.5 and not force:
        print(f"{hours_left:.1f} h before today's 5pm close; the rule trades at 6 h (10:30-11:30am ET). "
              f"Run again then, or add --force.")
        return rows
    logged = {r["event"] for r in rows}
    for series in SERIES:
        event, ref, markets = candidates(series, now)
        if event in logged:
            print(f"{event}: already logged today")
            continue
        if ref is None:
            print(f"{event}: no reference price (yesterday's event missing), skipped")
            continue
        new = []
        for m in markets:
            ob = get_json(f"{KALSHI_API}/markets/{m['ticker']}/orderbook?depth=1", PACER).get("orderbook_fp") or {}
            (yb, yq), (nb, nq) = best(ob.get("yes_dollars")), best(ob.get("no_dollars"))
            ya = 1 - nb if nb else 1.0
            if yb <= 0 or ya >= 1:
                continue
            mid = (yb + ya) / 2
            if mid < 0.20:
                side, price, size = "NO", round(1 - yb, 4), yq
            elif mid > 0.80:
                side, price, size = "YES", round(ya, 4), nq
            else:
                continue
            new.append({"logged_at": now.isoformat(timespec="seconds"), "event": event, "ticker": m["ticker"],
                        "strike": strike(m), "btc": ref, "side": side, "price": price, "fee": round(fee(price), 5),
                        "yes_bid": yb, "yes_ask": round(ya, 4), "size_at_price": size, "result": "", "won": "", "pnl": ""})
        print(f"{event}: logged {len(new)} paper trades (reference {ref:,.6g}), {hours_left:.1f} h before the close")
        for r in new:
            print(f"  {r['side']:3s} {r['ticker']}  at {r['price']:.2f} (size there {float(r['size_at_price']):,.0f})")
        rows = rows + new
    return rows


def settle(rows):
    for r in rows:
        if r["result"]:
            continue
        m = get_json(f"{KALSHI_API}/markets/{r['ticker']}", PACER).get("market") or {}
        if m.get("result") in ("yes", "no"):
            won = int((m["result"] == "yes") == (r["side"] == "YES"))
            r.update(result=m["result"], won=won, pnl=round(won - float(r["price"]) - float(r["fee"]), 5))
    return rows


def report(rows):
    done = [r for r in rows if r["result"]]
    if not done:
        print("No settled paper trades yet.")
        return
    by_coin = {}
    for r in done:
        by_coin.setdefault(r["event"].split("-")[0], []).append(float(r["pnl"]))
    pnl = [float(r["pnl"]) for r in done]
    days = len({r["event"].split("-")[1] for r in done})
    print(f"\nPaper record: {len(done)} trades over {days} days, won {sum(int(r['won']) for r in done)}, "
          f"average {sum(pnl) / len(pnl) * 100:+.2f}c per contract, total {sum(pnl):+.2f} per one contract per signal")
    for coin, xs in sorted(by_coin.items()):
        print(f"  {coin:8s} {len(xs):4d} trades, {sum(xs) / len(xs) * 100:+.2f}c each (backtest: "
              f"{'+2.5c' if coin == 'KXBTCD' else 'about +1c'})")


def main():
    rows = settle(read())
    if sys.argv[1:2] != ["report"]:
        rows = log(rows, force="--force" in sys.argv)
    write(rows)
    report(rows)


if __name__ == "__main__":
    main()
