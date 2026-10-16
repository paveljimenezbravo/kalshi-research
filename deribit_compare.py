"""Kalshi's daily BTC markets vs the probabilities implied by Deribit's options, right now.

Deribit (the biggest crypto options exchange) prices options at many strikes; the slope
of call prices across strikes is the market's probability that BTC ends above each
strike. For Kalshi's next 5pm ET "BTC above $X" event (KXBTCD) and its range brackets
(KXBTC), this interpolates Deribit's volatility smile to Kalshi's exact settlement time
(linear in total variance between the two surrounding Deribit expiries) and prints
Kalshi's price next to the options-implied probability for every strike.

These are risk-neutral probabilities: option buyers usually pay a little extra for big
moves, so Deribit's tails are, if anything, slightly too fat. Run: python deribit_compare.py
"""
import json
import math
import urllib.request
from datetime import datetime, timezone

import numpy as np

from fetch_data import Pacer, get_json

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
DERIBIT = "https://www.deribit.com/api/v2/public"
YEAR = 365.25 * 24 * 3600
N = np.vectorize(lambda x: 0.5 * (1 + math.erf(x / math.sqrt(2))))
PACER = Pacer(2)  # Kalshi reads share a rate limit with any running downloads


def get(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "btc15-backtest/1.0"}),
                                timeout=60) as r:
        return json.load(r)


def smiles(now):
    """Per Deribit expiry: time to expiry (years), forward, and out-of-the-money mark vols by log-moneyness."""
    expiry_of = {i["instrument_name"]: i["expiration_timestamp"] / 1000
                 for i in get(f"{DERIBIT}/get_instruments?currency=BTC&kind=option&expired=false")["result"]}
    by = {}
    for o in get(f"{DERIBIT}/get_book_summary_by_currency?currency=BTC&kind=option")["result"]:
        name = o["instrument_name"]
        if name not in expiry_of or not o.get("mark_iv"):
            continue
        _, _, strike, kind = name.split("-")
        by.setdefault(expiry_of[name], []).append((float(strike), kind, o["mark_iv"] / 100, o["underlying_price"]))
    out = []
    for exp, rows in sorted(by.items()):
        fwd = float(np.median([r[3] for r in rows]))
        otm = sorted((math.log(k / fwd), iv) for k, kind, iv, _ in rows if (kind == "C") == (k >= fwd))
        if len(otm) >= 5 and exp > now:
            out.append(((exp - now) / YEAR, fwd, np.array([x for x, _ in otm]), np.array([v for _, v in otm])))
    return out


def implied_prob_above(strikes, t, sm):
    """P(BTC > K at time t), from the two Deribit expiries around t (total variance linear in time)."""
    (t1, f1, k1, v1), (t2, f2, k2, v2) = next((a, b) for a, b in zip(sm, sm[1:]) if a[0] <= t <= b[0])
    a = (t - t1) / (t2 - t1)
    fwd = f1 + a * (f2 - f1)

    def call(K):
        x = np.log(K / fwd)
        w = (1 - a) * np.interp(x, k1, v1) ** 2 * t1 + a * np.interp(x, k2, v2) ** 2 * t2
        d1 = (-x + w / 2) / np.sqrt(w)
        return fwd * N(d1) - K * N(d1 - np.sqrt(w))

    K = np.asarray(strikes, dtype=float)
    h = fwd * 1e-3
    return (call(K - h) - call(K + h)) / (2 * h), fwd


def main():
    now = datetime.now(timezone.utc).timestamp()
    sm = smiles(now)
    events = get_json(f"{KALSHI}/events?series_ticker=KXBTCD&status=open&limit=50", PACER)["events"]
    ev = max(events, key=lambda e: e["event_ticker"])  # the furthest-out (5pm daily) event
    ms = get_json(f"{KALSHI}/markets?event_ticker={ev['event_ticker']}&limit=200", PACER)["markets"]
    close = datetime.fromisoformat(ms[0]["close_time"].replace("Z", "+00:00")).timestamp()
    t = (close - now) / YEAR
    rows = sorted((m["floor_strike"], float(m["yes_bid_dollars"]), float(m["yes_ask_dollars"])) for m in ms
                  if m.get("floor_strike") and m.get("strike_type") in ("greater", "greater_or_equal"))
    p, fwd = implied_prob_above([r[0] for r in rows], t, sm)
    print(f"{ev['event_ticker']}: settles in {(close - now) / 3600:.1f} h; Deribit forward ${fwd:,.0f}")
    print(f"{'strike':>10} {'vs fwd':>7} {'Kalshi bid':>10} {'ask':>6} {'Deribit':>8}  verdict")
    edges = []
    for (k, bid, ask), q in zip(rows, p):
        if bid <= 0.005 and ask >= 0.995:
            continue
        note = ""
        if q - ask - 0.07 * ask * (1 - ask) > 0.02:
            note = f"Kalshi YES cheap by {(q - ask) * 100:.1f}c"
        elif bid - q - 0.07 * bid * (1 - bid) > 0.02:
            note = f"Kalshi YES rich by {(bid - q) * 100:.1f}c"
        edges.append(((bid + ask) / 2 - q, k))
        print(f"{k:>10,.0f} {k / fwd - 1:>+7.2%} {bid:>10.2f} {ask:>6.2f} {q:>8.3f}  {note}")
    if edges:
        d = np.array([e for e, _ in edges])
        print(f"\nKalshi mid minus Deribit: mean {d.mean() * 100:+.2f}c, mean |diff| {np.abs(d).mean() * 100:.2f}c "
              f"over {len(d)} strikes")


if __name__ == "__main__":
    main()
