"""Paper trading for #23: sell the tails of today's 5pm ET BTC market, 6 hours before the close.

    python paper_tails.py          # settle earlier paper trades, log today's, print the record
    python paper_tails.py log --force    # log even outside the 10:30am-11:30am ET window

At about 11am ET it reads the LIVE order book of every strike of today's KXBTCD 5pm event
that is a multiple of $500 within 5% of BTC (as in the backtest), and records the trade the
rule would make: NO when YES is priced under 20c (paying 1 - the best YES bid), YES when
it's over 80c (paying the best YES ask). Nothing is sent to Kalshi; this only writes to
%LOCALAPPDATA%\\kalshi-btc15\\paper\\tails.csv, before the result is known, and fills in
the result after the close. Backtest expectation: about +2.5c per contract after the fee.
"""
import csv
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from config import HOME, KALSHI_API
from fetch_data import Pacer, get_json

ET = ZoneInfo("America/New_York")
LEDGER = HOME / "paper" / "tails.csv"
FIELDS = ["logged_at", "event", "ticker", "strike", "btc", "side", "price", "fee", "yes_bid", "yes_ask",
          "size_at_price", "result", "won", "pnl"]
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


def log(rows, force=False):
    now = datetime.now(ET)
    event = f"KXBTCD-{now:%y%b%d}17".upper()
    if any(r["event"] == event for r in rows):
        print(f"{event}: already logged today")
        return rows
    close = now.replace(hour=17, minute=0, second=0, microsecond=0)
    hours_left = (close - now).total_seconds() / 3600
    if not 5.5 <= hours_left <= 6.5 and not force:
        print(f"{event}: {hours_left:.1f} h before the close; the rule trades at 6 h (10:30-11:30am ET). "
              f"Run again then, or add --force.")
        return rows
    btc = float(get_json("https://api.exchange.coinbase.com/products/BTC-USD/ticker", PACER)["price"])
    markets = get_json(f"{KALSHI_API}/markets?event_ticker={event}&limit=200", PACER).get("markets") or []
    new = []
    for m in markets:
        k = m.get("floor_strike")
        if not k or round(k) % 500 or abs(round(k) / btc - 1) > 0.05:
            continue
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
                    "strike": k, "btc": btc, "side": side, "price": price, "fee": round(fee(price), 5),
                    "yes_bid": yb, "yes_ask": round(ya, 4), "size_at_price": size, "result": "", "won": "", "pnl": ""})
    print(f"{event}: logged {len(new)} paper trades with BTC at ${btc:,.0f}, {hours_left:.1f} h before the close")
    for r in new:
        print(f"  {r['side']:3s} {r['ticker']}  at {r['price']:.2f} (size there {float(r['size_at_price']):,.0f})")
    return rows + new


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
    days = {}
    for r in done:
        days.setdefault(r["event"], []).append(float(r["pnl"]))
    pnl = [float(r["pnl"]) for r in done]
    print(f"\nPaper record: {len(done)} trades over {len(days)} days, won {sum(int(r['won']) for r in done)}, "
          f"average {sum(pnl) / len(pnl) * 100:+.2f}c per contract (backtest: about +2.5c), "
          f"total {sum(pnl):+.2f} per one contract per signal")
    for ev, xs in sorted(days.items())[-10:]:
        print(f"  {ev}: {len(xs)} trades, {sum(xs):+.2f}")


def main():
    rows = settle(read())
    if sys.argv[1:2] != ["report"]:
        rows = log(rows, force="--force" in sys.argv)
    write(rows)
    report(rows)


if __name__ == "__main__":
    main()
