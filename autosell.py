"""Auto-sell: the safety rules for your Kalshi account.

Every second this checks each open position:
  value = contracts x the price you could sell at right now (the best bid)
and applies these rules, selling with an immediate-or-cancel order that can only
reduce a position, never open one:

1. Stop-loss: sell if a position is worth STOP_LOSS (20%) less than it cost.
2. Lock in a winner: once a position is worth ARM_AT (2x) what it cost, sell if
   it falls TRAIL (20%) below its best value. $80 -> $400 -> $320: sold.
3. Size limit: a position that cost more than MAX_TRADE (20%) of the account
   (cash + what open positions cost) is trimmed back to that size right away.
4. Daily stop: once the account is down DAILY_STOP (25%) of the day's money
   (its value when this started that day, plus that day's deposits), everything
   is sold and any new position is closed as soon as it appears, until midnight ET.

Start it with 6_autosell.bat and leave the window open; it only works while it
runs. `python autosell.py --dry-run` logs what it would do without selling.
"""
import argparse
import json
import time
import urllib.error
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from config import HOME
from kalshi_auth import get, load_credentials, request

STOP_LOSS = 0.20   # sell a position worth 20% less than it cost
ARM_AT = 2.0       # arm once the position is worth 2x what it cost (+100%)
TRAIL = 0.20       # then sell if it falls 20% below its best value since arming
MAX_TRADE = 0.20   # no position may cost more than 20% of the account; the excess is sold
DAILY_STOP = 0.25  # down 25% of the day's money: sell everything and stay flat until midnight ET
CONFIRM = 2        # the drop must show on this many checks in a row (ignores one-tick blips)
SLIPPAGE = 0.05    # accept fills up to 5 cents worse than the best bid, so the sell goes through
POLL_S = 1.0
ET = ZoneInfo("America/New_York")

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
    """Update one position's tracking state; return why it should be sold, or None.

    st holds: contracts (to notice when you add or trim), armed, peak, hits.
    """
    if st.get("contracts") != contracts:  # position changed size: start tracking it afresh
        st.clear()
        st.update(contracts=contracts, armed=False, peak=0.0, hits=0)
    if cost <= 0:
        return None
    if not st["armed"] and value >= ARM_AT * cost:
        st["armed"] = True
    st["peak"] = max(st["peak"], value)
    if st["armed"]:
        hit, why = value <= (1 - TRAIL) * st["peak"], f"fell {TRAIL:.0%} from its ${st['peak']:.2f} peak"
    else:
        hit, why = value <= (1 - STOP_LOSS) * cost, f"is down {STOP_LOSS:.0%} (stop-loss)"
    st["hits"] = st["hits"] + 1 if hit else 0
    return why if st["hits"] >= CONFIRM else None


def sell(creds, ticker, contracts, yes_bid, yes_ask, dry_run):
    """Sell `contracts` (signed like the position) with one immediate-or-cancel, reduce-only order.

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


def account_day(creds, state, positions_value, cash, now):
    """Track the day's money for the daily stop; return True once the stop is on."""
    day = now.astimezone(ET).strftime("%Y-%m-%d")
    if state.get("day") != day:
        state.update(day=day, day_start=cash + positions_value, locked=False, moved=0.0, moved_at=0)
        log(f"New day: starting value ${state['day_start']:.2f}. Daily stop at "
            f"-{DAILY_STOP:.0%} = -${state['day_start'] * DAILY_STOP:.2f}.")
    if now.timestamp() - state["moved_at"] > 60:  # deposits/withdrawals since the day began
        start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=ET).timestamp()
        dep = sum(d["amount_cents"] - d.get("fee_cents", 0) for d in get(creds, "/portfolio/deposits")["deposits"]
                  if d.get("status") == "applied" and d["created_ts"] >= start)
        wd = sum(w["amount_cents"] for w in get(creds, "/portfolio/withdrawals")["withdrawals"]
                 if w.get("status") == "applied" and w["created_ts"] >= start)
        if (dep - wd) / 100 != state["moved"]:
            state["moved"] = (dep - wd) / 100
            log(f"Today's net deposits: ${state['moved']:.2f}")
        state["moved_at"] = now.timestamp()
    day_money = state["day_start"] + state["moved"]
    value = cash + positions_value
    if not state["locked"] and day_money > 0 and value <= (1 - DAILY_STOP) * day_money:
        state["locked"] = True
        log(f"DAILY STOP: account ${value:.2f} is down {1 - value / day_money:.0%} from today's ${day_money:.2f}. "
            "Selling everything; new positions will be closed until midnight ET.")
    return state["locked"]


def check(creds, state, dry_run, now=None):
    now = now or datetime.now(ET)
    pos_state = state.setdefault("positions", {})
    positions = get(creds, "/portfolio/positions", {"count_filter": "position", "limit": 200})["market_positions"]
    cash = num(get(creds, "/portfolio/balance"), "balance", cents=True)
    rows = []
    for p in positions:
        contracts = num(p, "position")
        if contracts == 0:
            continue
        # The order book is live; /markets/{ticker} prices lag it by ~10 s, which let a
        # stop-loss meant for -20% fire at -47% on Oct 15.
        yes_bid, no_bid = best_bids(get(creds, f"/markets/{p['ticker']}/orderbook", {"depth": 1}))
        bid = yes_bid if contracts > 0 else no_bid  # what one contract sells for now
        rows.append((p["ticker"], contracts, num(p, "market_exposure", cents=True), yes_bid, no_bid, bid))
    locked = account_day(creds, state, sum(abs(r[1]) * r[5] for r in rows), cash, now)
    account = cash + sum(r[2] for r in rows)
    for ticker, contracts, cost, yes_bid, no_bid, bid in rows:
        if bid <= 0:
            continue  # no buyers this instant: can't sell, and don't judge it on an empty book
        yes_ask = 1 - no_bid if no_bid else 1.0
        value = abs(contracts) * bid
        desc = f"{ticker}: {'YES' if contracts > 0 else 'NO'} x{abs(contracts):.2f} worth ${value:.2f} (cost ${cost:.2f})"
        if locked:
            log(f"{desc} -> daily stop is on, selling")
            sell(creds, ticker, contracts, yes_bid, yes_ask, dry_run)
            continue
        if cost > MAX_TRADE * account * 1.02:  # 2% slack so rounding doesn't trigger tiny trims
            excess = round(abs(contracts) * (1 - MAX_TRADE * account / cost), 2)
            log(f"{desc} is {cost / account:.0%} of the ${account:.2f} account; limit {MAX_TRADE:.0%} "
                f"-> selling {excess:.2f} contracts")
            sell(creds, ticker, excess if contracts > 0 else -excess, yes_bid, yes_ask, dry_run)
            continue
        st = pos_state.setdefault(ticker, {})
        was_armed = st.get("armed")
        why = decide(st, value, cost, contracts)
        if why:
            log(f"{desc} {why} -> selling")
            sell(creds, ticker, contracts, yes_bid, yes_ask, dry_run)
            st["hits"] = 0
        elif st["armed"] and not was_armed:
            log(f"{ticker}: ARMED at ${value:.2f} on ${cost:.2f} cost (+{value / cost - 1:.0%}). "
                f"Will sell if it drops {TRAIL:.0%} from its best.")
    for ticker in set(pos_state) - {r[0] for r in rows}:  # closed or settled
        del pos_state[ticker]


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
    if "positions" not in state:  # older state files held only positions
        state = {"positions": state}
    log(f"Auto-sell {'DRY RUN ' if args.dry_run else ''}started. Stop-loss -{STOP_LOSS:.0%}; after "
        f"+{ARM_AT - 1:.0%}, sell on a {TRAIL:.0%} drop from the peak; max {MAX_TRADE:.0%} of the account "
        f"per position; daily stop -{DAILY_STOP:.0%}. Checking every {POLL_S:g} s. Close this window to stop.")
    last_heartbeat = 0.0
    while True:
        try:
            check(creds, state, args.dry_run)
            STATE_FILE.write_text(json.dumps(state))
            if time.time() - last_heartbeat > 600:
                armed = [t for t, s in state["positions"].items() if s.get("armed")]
                log(f"watching {len(state['positions'])} position(s); armed: {', '.join(armed) or 'none'}"
                    + ("; DAILY STOP ON" if state.get("locked") else ""))
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
