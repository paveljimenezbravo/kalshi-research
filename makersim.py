"""Maker test on the recorder's trade-by-trade data, with a realistic place in line.

Every DECIDE_EVERY seconds, if no order is resting, post a bid for SIZE contracts at the
best price on one side (YES or NO). The order joins the BACK of the line: everything
already showing at that price is ahead of it, and only real recorded trades at that
price move the line forward. A trade at a worse price means the line was used up, so the
order is filled. Unfilled orders are cancelled after LIFETIME seconds. A second run puts
the order at the FRONT of the line (as if we'd been there first), so the truth lies
between the two.

Each fill is scored by Kalshi's mid 10/30/60 s later (markouts: lots of data, quick to
settle) and by the window's result. Makers pay no fee on these markets.

    python makersim.py
"""
from types import SimpleNamespace

import numpy as np
import pandas as pd

import livedata
from live_edge import asof, results, top_of_book

DECIDE_EVERY = 10   # seconds between decisions while no order is resting
LIFETIME = 30       # seconds an order rests before it's cancelled
LATENCY = 0.3       # seconds from decision until our order is resting on Kalshi
SIZE = 10           # contracts per order
HORIZONS = (10, 30, 60)
ZONES = {"all 5-95c": (0.05, 0.95), "favorites 70-95c": (0.70, 0.95), "long shots 5-30c": (0.05, 0.30)}


def window_fills(w, book, trades, side, lo, hi, front, fair=None, guard=None):
    """Fills for one window. YES bids get hit by takers selling YES; NO bids by takers buying YES.
    With `guard`, act like a real market maker: don't post right after the fast BTC price
    (Binance) moved against the order, and cancel a resting order once it moves against it by
    `guard` (in fair-value terms), the cancel taking effect LATENCY seconds later."""
    tb, yes_lv, no_lv = book.t.to_numpy(), book.yes.to_numpy(), book.no.to_numpy()
    hit = trades[trades.side == ("ask" if side == "YES" else "bid")]
    ts, tp, tq = hit.ts.to_numpy(), hit.p.to_numpy(), hit.q.to_numpy()
    against = 1.0 if side == "YES" else -1.0  # a falling fair value hurts a YES bid, a rising one a NO bid
    fills, posts, t = [], 0, w.open + 5
    while t < w.close - 30:
        arrive = t + LATENCY
        i = np.searchsorted(tb, arrive, side="right") - 1
        if i < 0 or arrive - tb[i] > 5 or not yes_lv[i] or not no_lv[i]:
            t += DECIDE_EVERY
            continue
        best = yes_lv[i][-1] if side == "YES" else no_lv[i][-1]   # [price, size] of our side's best bid
        price = best[0]
        if not lo <= price <= hi:
            t += DECIDE_EVERY
            continue
        end = arrive + LIFETIME
        if guard is not None:
            f_now, f_before = asof(fair[0], fair[1], np.array([t, t - 2.0]), max_age=2)
            if not np.isfinite(f_now) or not np.isfinite(f_before):
                t += 1.0
                continue
            if against * (f_now - f_before) < -guard:  # the fast price just moved against us: wait
                t += 1.0
                continue
            k0, k1 = np.searchsorted(fair[0], arrive), np.searchsorted(fair[0], end)
            moved = np.flatnonzero(against * (fair[1][k0:k1] - f_now) < -guard)
            if len(moved):
                end = min(end, fair[0][k0 + moved[0]] + LATENCY)  # our cancel lands LATENCY later
        posts += 1
        level = price if side == "YES" else round(1 - price, 4)   # the YES price trades must print at
        a, b = np.searchsorted(ts, arrive, side="right"), np.searchsorted(ts, end, side="right")
        p, q, tt = tp[a:b], tq[a:b], ts[a:b]
        through = p < level - 1e-9 if side == "YES" else p > level + 1e-9
        at_level = np.cumsum(np.where(np.abs(p - level) < 1e-9, q, 0.0))
        ahead = 0.0 if front else best[1]
        done = np.flatnonzero(through | (at_level >= ahead + SIZE))
        if len(done):
            k = done[0]
            fills.append((tt[k], price, SIZE))
            t = tt[k] + DECIDE_EVERY
        else:
            partial = min(SIZE, at_level[-1] - ahead) if len(at_level) else 0
            if partial > 0:
                k = np.flatnonzero(at_level > ahead)[0]
                fills.append((tt[k], price, partial))
            t = end
    return fills, posts


def fair_values(data, series, windows):
    """Fair value every quarter second from Binance futures (the fastest feed), per window."""
    import live_edge as le
    coin = "ETH" if series.startswith("KXETH") else "BTC"
    if not le.spot_rows(data, coin, "binance"):
        return {}
    g = le.btc_grid(data, coin, "binance")
    out = {}
    for m, w in windows.iterrows():
        f = le.window_frame(SimpleNamespace(m=m, open=w.open, close=w.close, target=w.target), g, data["book"])
        if f is not None:
            ok = f.fair.notna()
            out[m] = (f.t.to_numpy()[ok], f.fair.to_numpy()[ok])
    return out


MODES = (("back", False, None), ("front", True, None), ("back+guard", False, 0.01))


def run(data, series="KXBTC15M"):
    windows = data["mkt"].groupby("m").last()
    windows = windows[(windows.index.str.startswith(series)) & (windows.close < data["book"].t.max())]
    outcome = results(list(windows.index))
    fairs = fair_values(data, series, windows)
    rows = []
    for line, front, guard in MODES:
        for side in ("YES", "NO"):
            for zone, (lo, hi) in ZONES.items():
                recs, posts = [], 0
                for m, w in windows.iterrows():
                    book = data["book"][data["book"].m == m]
                    tr = data["trade"][data["trade"].m == m]
                    if len(book) < 100 or len(tr) < 100 or (guard is not None and m not in fairs):
                        continue
                    fills, n = window_fills(w, book, tr, side, lo, hi, front, fairs.get(m), guard)
                    posts += n
                    bid, ask = top_of_book(book)
                    mid = (bid + ask) / 2
                    for t_fill, price, qty in fills:
                        rec = {"m": m, "qty": qty, "price": price}
                        for h in HORIZONS:
                            v = asof(book.t.to_numpy(), mid, np.array([min(t_fill + h, w.close - 1)]), max_age=5)[0]
                            rec[f"mark{h}"] = (v if side == "YES" else 1 - v) - price
                        res = outcome.get(m)
                        rec["settled"] = (float((res == "yes") == (side == "YES")) - price) if res else np.nan
                        recs.append(rec)
                f = pd.DataFrame(recs)
                row = {"line": line, "side": side, "zone": zone, "posts": posts,
                       "filled": len(f), "windows": f.m.nunique() if len(f) else 0}
                for col in [f"mark{h}" for h in HORIZONS] + ["settled"]:
                    g = f.dropna(subset=[col]) if len(f) else f
                    if len(g):
                        per = g.groupby("m").apply(lambda x: np.average(x[col], weights=x.qty), include_groups=False)
                        half = 1.96 * per.std() / np.sqrt(len(per)) if len(per) > 1 else np.nan
                        row[col] = f"{np.average(g[col], weights=g.qty) * 100:+.2f}±{half * 100:.2f}"
                rows.append(row)
    return pd.DataFrame(rows)


def report():
    data = livedata.load(kinds=("mkt", "book", "trade"))
    for series in ("KXBTC15M", "KXETH15M"):
        print(f"===== {series} (cents per contract, ± 95% range with windows as the unit)")
        print(run(data, series).to_string(index=False), "\n")


if __name__ == "__main__":
    report()
