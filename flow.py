"""Who loses money? Markouts for every recorded Kalshi trade.

For each trade, compare the price the taker paid with Kalshi's mid price 10, 30 and 60
seconds later. If the price tends to move against the taker, the taker was overpaying
and the maker on the other side earned it. Grouping trades by size, side, price and time
left shows which kinds of trades the market makers are profiting from. That's the
standard way trading firms find out who the uninformed flow is.

    python flow.py

Positive numbers are good for the MAKER (the one who posted the order the taker hit),
in cents per contract, before any maker fee. A taker's result is the negative of this,
minus their fee.
"""
import numpy as np
import pandas as pd

import livedata
from live_edge import asof, results, top_of_book

HORIZONS = (10, 30, 60)


def trade_table(data):
    """Each trade with Kalshi's mid H seconds later and the maker's view of it."""
    out = []
    windows = data["mkt"].groupby("m").last()
    for m, tr in data["trade"].groupby("m"):
        b = data["book"][data["book"].m == m]
        if len(b) < 100 or m not in windows.index:
            continue
        bid, ask = top_of_book(b)
        tb, mid = b.t.to_numpy(), (bid + ask) / 2
        t = tr.ts.to_numpy()
        sign = np.where(tr.side.to_numpy() == "bid", 1.0, -1.0)  # +1: taker bought YES
        f = pd.DataFrame({"m": m, "ts": t, "p": tr.p.to_numpy(), "q": tr.q.to_numpy(), "sign": sign,
                          "left": windows.loc[m, "close"] - t})
        for h in HORIZONS:
            later = asof(tb, mid, t + h, max_age=5)
            later[t + h > windows.loc[m, "close"]] = np.nan  # don't look past the close
            f[f"maker{h}"] = -sign * (later - f.p)  # what the maker made (YES-price terms)
        # The price of the side the taker bought: YES price if they bought YES, NO price otherwise
        f["taker_side_price"] = np.where(sign > 0, f.p, 1 - f.p)
        out.append(f)
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame()


def summarize(f, by, outcome=None):
    """Contract-weighted maker markouts per group, with a 95% range that treats each
    window-minute as one sample (trades seconds apart aren't independent)."""
    f = f.assign(cluster=f.m + ":" + (f.left // 60).astype(int).astype(str))
    rows = []
    for key, g in f.groupby(by, observed=True):
        row = {by: key, "trades": len(g), "contracts": round(g.q.sum())}
        for h in HORIZONS:
            col = f"maker{h}"
            gg = g.dropna(subset=[col])
            if gg.empty:
                continue
            per = gg.groupby("cluster").apply(lambda x: np.average(x[col], weights=x.q), include_groups=False)
            mean = np.average(gg[col], weights=gg.q)
            half = 1.96 * per.std() / np.sqrt(len(per)) if len(per) > 1 else np.nan
            row[f"{h}s"] = f"{mean * 100:+.2f} ± {half * 100:.2f}"
        if outcome:
            won = g.m.map(outcome)
            ok = won.notna()
            if ok.any():
                y = (won[ok] == "yes").astype(float)
                row["settled"] = f"{np.average(-g.sign[ok] * (y - g.p[ok]), weights=g.q[ok]) * 100:+.2f}"
        rows.append(row)
    return pd.DataFrame(rows)


def report():
    data = livedata.load(kinds=("mkt", "book", "trade"))
    f = trade_table(data)
    if f.empty:
        print("No trades with order-book coverage yet.")
        return
    hours = (f.ts.max() - f.ts.min()) / 3600
    print(f"{len(f):,} trades ({f.q.sum():,.0f} contracts) over {hours:.1f} hours, {f.m.nunique()} windows")
    outcome = results(sorted(f.m.unique()))
    f["everything"] = f.m.str.split("-").str[0]  # one row per market series (BTC, ETH)
    f = f[f.everything == "KXBTC15M"].copy() if f.everything.nunique() > 1 else f
    f["taker"] = np.where(f.sign > 0, "bought YES", "bought NO")
    f["size"] = pd.cut(f.q, [0, 5, 25, 100, 1e9], labels=["<5", "5-25", "25-100", "100+"], right=False)
    f["side_price"] = pd.cut(f.taker_side_price, [0, 0.1, 0.3, 0.5, 0.7, 0.9, 1.01],
                             labels=["<10c", "10-30c", "30-50c", "50-70c", "70-90c", "90c+"], right=False)
    f["time_left"] = pd.cut(f.left, [0, 60, 180, 420, 901], labels=["last minute", "1-3 min", "3-7 min", "7-15 min"],
                            right=False)
    for by in ("everything", "taker", "size", "side_price", "time_left"):
        print(f"\nMaker's markout by {by} (cents per contract, contract-weighted)")
        print(summarize(f, by, outcome).to_string(index=False))


if __name__ == "__main__":
    report()
