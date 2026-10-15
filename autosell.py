"""Auto-sell: lock in a winner once it has doubled.

Every few seconds this checks each open position on your Kalshi account:
  value = contracts x the price you could sell at right now (the best bid)
  gain  = value / what the position cost
Once gain reaches ARM_AT (2.0 = up 100%), the position is "armed" and its best
value is tracked. If the value then falls TRAIL (20%) below that best, the whole
position is sold immediately (an immediate-or-cancel order that can only reduce
the position, never open a new one).

Example: $80 in, it climbs to $400 (armed, peak $400), drops to $320 -> sold.

Start it with 6_autosell.bat and leave the window open; it only works while it
runs. `python autosell.py --dry-run` logs what it would do without selling.
"""
import argparse
import json
import time
import urllib.error
import uuid
from datetime import datetime

from config import HOME
from kalshi_auth import get, load_credentials, request

ARM_AT = 2.0       # arm once the position is worth 2x what it cost (+100%)
TRAIL = 0.20       # then sell if it falls 20% below its best value since arming
CONFIRM = 2        # the drop must show on this many checks in a row (ignores one-tick blips)
SLIPPAGE = 0.05    # accept fills up to 5 cents worse than the best bid, so the sell goes through
POLL_S = 1.0

STATE_FILE = HOME / "autosell_state.json"
LOG_FILE = HOME / "logs" / "autosell.log"


def log(msg):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    print(line, flush=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def num(d, name, cents=False):
    """Kalshi's newer *_dollars / *_fp string fields, falling back to the old cent integers."""
    for k in (name + "_dollars", name + "_fp"):
        if d.get(k) is not None:
            return float(d[k])
    v = d.get(name) or 0
    return v / 100 if cents else float(v)


def best_bids(book):
    """Best YES and NO bids from /orderbook (0 when that side is empty or the market is closed)."""
    ob = book.get("orderbook_fp") or {}
    best = lambda levels: max((float(px) for px, _ in levels or []), default=0.0)
    return best(ob.get("yes_dollars")), best(ob.get("no_dollars"))


def decide(st, value, cost, contracts):
    """Update one position's tracking state; return True when it should be sold.

    st holds: contracts (to notice when you add or trim), armed, peak, hits.
    """
    if st.get("contracts") != contracts:  # position changed size: start tracking it afresh
        st.clear()
        st.update(contracts=contracts, armed=False, peak=0.0, hits=0)
    if cost <= 0:
        return False
    if not st["armed"] and value >= ARM_AT * cost:
        st["armed"] = True
    if not st["armed"]:
        return False
    st["peak"] = max(st["peak"], value)
    st["hits"] = st["hits"] + 1 if value <= (1 - TRAIL) * st["peak"] else 0
    return st["hits"] >= CONFIRM


def sell(creds, ticker, contracts, yes_bid, yes_ask, dry_run):
    """Close the whole position with one immediate-or-cancel, reduce-only order.

    Orders are quoted on the YES leg: a YES position is sold with an ask, and a NO
    position is closed by bidding for YES (each YES+NO pair pays $1).
    """
    if contracts > 0:
        side, price = "ask", max(0.01, yes_bid - SLIPPAGE)
    else:
        side, price = "bid", min(0.99, yes_ask + SLIPPAGE)
    body = {
        "ticker": ticker,
        "side": side,
        "count": f"{abs(contracts):.2f}",
        "price": f"{price:.2f}",
        "time_in_force": "immediate_or_cancel",
        "self_trade_prevention_type": "taker_at_cross",
        "reduce_only": True,
        "client_order_id": str(uuid.uuid4()),
    }
    if dry_run:
        log(f"  DRY RUN, would send: {body}")
        return
    r = request(creds, "POST", "/portfolio/events/orders", body=body)
    log(f"  SOLD {r.get('fill_count')} of {abs(contracts):.2f} at avg ${r.get('average_fill_price', '-')}"
        f" (fee/contract ${r.get('average_fee_paid', '0')}); {r.get('remaining_count')} unfilled")


def check(creds, state, dry_run):
    positions = get(creds, "/portfolio/positions", {"count_filter": "position", "limit": 200})["market_positions"]
    held = set()
    for p in positions:
        ticker, contracts = p["ticker"], num(p, "position")
        cost = num(p, "market_exposure", cents=True)
        if contracts == 0:
            continue
        held.add(ticker)
        # The order book is live; /markets/{ticker} prices lag it by ~10 s, which let a
        # stop-loss meant for -20% fire at -47% on Oct 15.
        yes_bid, no_bid = best_bids(get(creds, f"/markets/{ticker}/orderbook", {"depth": 1}))
        yes_ask = 1 - no_bid if no_bid else 1.0
        bid = yes_bid if contracts > 0 else no_bid  # what one contract sells for now
        if bid <= 0:
            continue  # no buyers this instant: don't judge the position on an empty book
        value = abs(contracts) * bid
        st = state.setdefault(ticker, {})
        was_armed = st.get("armed")
        if decide(st, value, cost, contracts):
            log(f"{ticker}: {'YES' if contracts > 0 else 'NO'} x{abs(contracts):.2f} worth ${value:.2f} "
                f"(cost ${cost:.2f}, peak ${st['peak']:.2f}) fell {TRAIL:.0%} from its peak -> selling")
            sell(creds, ticker, contracts, yes_bid, yes_ask, dry_run)
            st["hits"] = 0
        elif st["armed"] and not was_armed:
            log(f"{ticker}: ARMED at ${value:.2f} on ${cost:.2f} cost (+{value / cost - 1:.0%}). "
                f"Will sell if it drops {TRAIL:.0%} from its best.")
    for ticker in set(state) - held:  # closed or settled
        del state[ticker]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="log what would be sold without selling")
    args = ap.parse_args()
    creds = load_credentials()
    if creds is None:
        raise SystemExit("No API key in %LOCALAPPDATA%\\kalshi-btc15\\secrets. See 5_my_trades.bat for setup.")
    try:
        state = json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        state = {}
    log(f"Auto-sell {'DRY RUN ' if args.dry_run else ''}started: arms at +{ARM_AT - 1:.0%}, "
        f"sells on a {TRAIL:.0%} drop from the peak. Checking every {POLL_S:g} s. Close this window to stop.")
    last_heartbeat = 0.0
    while True:
        try:
            check(creds, state, args.dry_run)
            STATE_FILE.write_text(json.dumps(state))
            if time.time() - last_heartbeat > 600:
                armed = [t for t, s in state.items() if s.get("armed")]
                log(f"watching {len(state)} position(s); armed: {', '.join(armed) or 'none'}")
                last_heartbeat = time.time()
        except urllib.error.HTTPError as e:
            log(f"Kalshi error {e.code}: {e.read()[:200]!r}")
            time.sleep(5)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            log(f"connection problem: {e}; retrying")
            time.sleep(5)
        time.sleep(POLL_S)


if __name__ == "__main__":
    main()
