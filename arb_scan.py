"""Scan every open Kalshi event for prices that contradict each other (riskless after fees).

1. Mutually exclusive events (at most one market can settle YES, e.g. temperature
   brackets): buying NO on every market pays at least n-1 dollars, so it locks in a
   profit when the YES bids add up to more than $1 plus fees. If the brackets also
   cover every possible outcome (a "below" market, contiguous "between" markets and an
   "above" market), exactly one settles YES, so buying YES on all of them locks in a
   profit when the asks add up to less than $1 minus fees.
2. Ladders ("above $X" markets on the same subject and expiry, e.g. one player's 2+, 3+,
   4+ receptions): a higher strike can't be more likely than a lower one. If the higher strike's YES bid beats the lower strike's YES
   ask, buy YES low and NO high: one of them always pays, often both.

Prices come from Kalshi's market list (which lags the live book by ~10 s), so any hit
is re-checked against the live order book before it's reported as real.
Run: python arb_scan.py
"""
import json
import re
from collections import defaultdict

from config import KALSHI_API
from fetch_data import Pacer, get_json, num

PACER = Pacer(4)  # stay well inside Kalshi's read limit while other downloads run


def fee(p, mult=1.0):
    return 0.07 * mult * p * (1 - p)


def open_events():
    cursor, events = "", []
    while True:
        page = get_json(f"{KALSHI_API}/events?status=open&with_nested_markets=true&limit=200"
                        + (f"&cursor={cursor}" if cursor else ""), PACER)
        events += page.get("events") or []
        cursor = page.get("cursor")
        if not cursor:
            return events


def quotes(m):
    return {"ticker": m["ticker"], "bid": num(m, "yes_bid") or 0.0, "ask": num(m, "yes_ask") or 1.0,
            "bid_size": num(m, "yes_bid_size") or 0.0, "ask_size": num(m, "yes_ask_size") or 0.0,
            "type": m.get("strike_type"), "floor": m.get("floor_strike"), "cap": m.get("cap_strike"),
            "close": m.get("close_time"), "title": m.get("yes_sub_title") or m.get("title", ""),
            # what the ladder is about: Kalshi's subject tag (team/player), else the title minus its numbers
            "subject": json.dumps(m["custom_strike"], sort_keys=True) if m.get("custom_strike")
            else re.sub(r"[\d.,$]+", "#", m.get("title") or "")}


def covers_everything(ms):
    """Numeric brackets that tile the whole line: one 'less', contiguous 'between's, one 'greater'."""
    less = [m for m in ms if m["type"] == "less"]
    greater = [m for m in ms if m["type"] == "greater"]
    between = sorted((m for m in ms if m["type"] == "between"), key=lambda m: m["floor"])
    if len(less) != 1 or len(greater) != 1 or len(less) + len(greater) + len(between) != len(ms):
        return False
    edges = [less[0]["cap"]] + [x for m in between for x in (m["floor"], m["cap"])] + [greater[0]["floor"]]
    # Kalshi's integer brackets: 'less 78' = 77 or below, 'between 78-79', ..., 'greater 85' = 86 or above
    steps = [(between[i]["floor"] - (between[i - 1]["cap"] if i else less[0]["cap"])) for i in range(len(between))]
    last = greater[0]["floor"] - (between[-1]["cap"] if between else less[0]["cap"] - 1)
    return all(s in (0, 1) for s in steps) and last in (0, 1) and None not in edges


def live_book(ticker):
    ob = get_json(f"{KALSHI_API}/markets/{ticker}/orderbook?depth=1", PACER).get("orderbook_fp") or {}
    best = lambda lv: max(((float(p), float(q)) for p, q in lv or []), default=(0.0, 0.0))
    (yb, yq), (nb, nq) = best(ob.get("yes_dollars")), best(ob.get("no_dollars"))
    return yb, yq, 1 - nb if nb else 1.0, nq  # yes bid, its size, yes ask, its size


def main():
    events = open_events()
    print(f"{len(events):,} open events, {sum(len(e.get('markets') or []) for e in events):,} markets")
    hits, near = [], []
    for e in events:
        ms = [quotes(m) for m in e.get("markets") or [] if m.get("status") in (None, "active", "open")]
        if len(ms) < 2:
            continue
        title = e.get("title", e["event_ticker"])
        if e.get("mutually_exclusive"):
            bids = sum(m["bid"] for m in ms)
            no_cost = sum((1 - m["bid"]) + fee(1 - m["bid"]) for m in ms if m["bid"] > 0) + \
                      sum(1.0 for m in ms if m["bid"] <= 0)  # NO with no YES bid costs $1 (no edge)
            profit_no = (len(ms) - 1) - no_cost
            row = (profit_no, "buy NO on all", e["event_ticker"], title, len(ms), bids, None, None)
            (hits if profit_no > 0 else near).append(row)
            if covers_everything(ms):
                yes_cost = sum(m["ask"] + fee(m["ask"]) for m in ms)
                row = (1 - yes_cost, "buy YES on all", e["event_ticker"], title, len(ms), sum(m["ask"] for m in ms),
                       None, None)
                (hits if 1 - yes_cost > 0 else near).append(row)
        # ladders: 'greater' markets on the same subject sharing a close time
        lad = defaultdict(list)
        for m in ms:
            if m["type"] in ("greater", "greater_or_equal") and m["floor"] is not None:
                lad[(m["close"], m["subject"])].append(m)
        for rungs in lad.values():
            rungs.sort(key=lambda m: m["floor"])
            for lo, hi in zip(rungs, rungs[1:]):
                if hi["floor"] <= lo["floor"]:
                    continue  # same strike twice means it isn't one ladder
                profit = hi["bid"] - lo["ask"] - fee(lo["ask"]) - fee(1 - hi["bid"])
                row = (profit, f"ladder: YES {lo['floor']} + NO {hi['floor']}", e["event_ticker"], title, 2,
                       hi["bid"] - lo["ask"], lo["ticker"], hi["ticker"])
                (hits if profit > 0 else near).append(row)

    print(f"\nCandidates from the (slightly stale) market list: {len(hits)}")
    confirmed = []
    for profit, kind, ev, title, n, gross, lo_t, hi_t in sorted(hits, key=lambda r: -r[0])[:25]:
        print(f"  {profit * 100:+.2f}c  {kind:28s} {ev}  ({title[:60]}), {n} markets, gross {gross:.3f}")
        if lo_t:
            _, _, ya_lo, qa_lo = live_book(lo_t)
            yb_hi, qb_hi, _, _ = live_book(hi_t)
            live = yb_hi - ya_lo - fee(ya_lo) - fee(1 - yb_hi)
            print(f"      live book: {live * 100:+.2f}c, size {min(qa_lo, qb_hi):,.0f}")
            if live > 0:
                confirmed.append((live, kind, ev))
    print(f"\nConfirmed on the live book: {len(confirmed)}")
    print("\nClosest misses (best 10 after fees):")
    for profit, kind, ev, title, n, gross, _, _ in sorted(near, key=lambda r: -r[0])[:10]:
        print(f"  {profit * 100:+.2f}c  {kind:28s} {ev}  ({title[:60]}), {n} markets, gross {gross:.3f}")


if __name__ == "__main__":
    main()
