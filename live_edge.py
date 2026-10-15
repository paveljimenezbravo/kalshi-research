"""Speed test on the recorder's data: does Kalshi's price lag BTC, and could a fast trader profit?

For every quarter second of every recorded window, work out what YES is worth from the
live BTC price (average of Coinbase's and Kraken's mid prices), the window's starting
level, the time left and recent volatility. Then:

  1. Lag: after the fair value moves, how many seconds until Kalshi's price follows?
  2. Trading on it: when Kalshi's ask is below fair value by a margin, buy after a
     realistic reaction delay, and score each trade by where Kalshi's own price is
     10, 30 and 60 seconds later (and by the result, once the window settles).

Everything is timed by this PC's clock, so the delays include our own network delay,
which is the delay a bot running here would have.

    python live_edge.py
"""
import json
import math
import time
import urllib.request
import warnings

import numpy as np
import pandas as pd

import livedata
from config import KALSHI_API, TAKER_FEE_RATE

STEP = 0.25               # seconds per grid step (the recorder polls Kalshi's book this often)
AVERAGING_S = 40          # the 60-second settlement average is as uncertain as settling 40 s early
VOL_LOOKBACK = 20 * 60    # seconds of BTC history behind the volatility estimate
_erf = np.vectorize(math.erf)


def ncdf(x):
    return 0.5 * (1 + _erf(np.asarray(x, dtype=float) / math.sqrt(2)))


def asof(t_src, values, t_grid, max_age=None):
    """The latest value at or before each grid time (NaN before the first one or if stale)."""
    idx = np.searchsorted(t_src, t_grid, side="right") - 1
    ok = idx >= 0
    out = np.where(ok, values[np.clip(idx, 0, None)], np.nan)
    if max_age is not None:
        age = t_grid - np.where(ok, t_src[np.clip(idx, 0, None)], -np.inf)
        out[age > max_age] = np.nan
    return out


def spot_rows(data, coin="BTC", source="composite"):
    """Price rows for one coin: Coinbase + Kraken spot ("composite"), or Binance perpetual futures
    ("binance"), which usually move first. Older recordings are BTC only and have no coin label."""
    if source == "binance":
        d = data.get("bn")
        return {"bn": d[d.s == f"{coin}USDT"]} if d is not None and (d.s == f"{coin}USDT").any() else {}
    out = {}
    for kind, col, name in (("cb", "p", f"{coin}-USD"), ("kr", "s", f"{coin}/USD")):
        d = data.get(kind)
        if d is not None and len(d):
            out[kind] = d[d[col].isna() | (d[col] == name)] if col in d else d
    return out


def btc_grid(data, coin="BTC", source="composite"):
    """Mid price every STEP seconds (mean across the source's feeds), plus a per-second
    volatility estimate from the previous VOL_LOOKBACK seconds (past data only)."""
    spot = spot_rows(data, coin, source)
    t0 = min(d.t.iloc[0] for d in spot.values())
    t1 = max(d.t.iloc[-1] for d in spot.values())
    grid = np.arange(math.ceil(t0), t1, STEP)
    mids = [asof(d.t.to_numpy(), ((d.bid + d.ask) / 2).to_numpy(), grid, max_age=30) for d in spot.values()]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        btc = np.nanmean(np.vstack(mids), axis=0)
    g = pd.DataFrame({"t": grid, "btc": btc})
    r5 = np.log(g.btc).diff(int(5 / STEP))  # 5-second moves, less noisy than tick-to-tick
    g["vol_s"] = np.sqrt((r5 ** 2).rolling(int(VOL_LOOKBACK / STEP), min_periods=int(300 / STEP)).mean() / 5)
    return g


def windows(data, series="KXBTC15M"):
    w = data["mkt"].groupby("m").last().reset_index()
    return w[(w.close <= time.time()) & w.m.str.startswith(series)]  # finished windows only


def window_frame(w, g, book):
    """One row per STEP from the window's open to its close: BTC, Kalshi's bid/ask, and fair value
    (fair value stops when the final averaging minute starts)."""
    pre = g[(g.t >= w.open - 60) & (g.t < w.open)]
    if len(pre) == 0 or pre.btc.notna().mean() < 0.9:
        return None  # recording didn't cover the minute that sets the starting level
    b = book[book.m == w.m]
    if len(b) < 100:
        return None
    f = g[(g.t >= w.open) & (g.t < w.close)].copy()
    tb = b.t.to_numpy()
    f["bid"] = asof(tb, b.yes.map(lambda x: x[-1][0] if x else np.nan).to_numpy(), f.t.to_numpy(), max_age=10)
    f["ask"] = asof(tb, b.no.map(lambda x: round(1 - x[-1][0], 4) if x else np.nan).to_numpy(), f.t.to_numpy(),
                    max_age=10)
    f["mid"] = (f.bid + f.ask) / 2
    start = pre.btc.mean()
    tau = (w.close - f.t) - AVERAGING_S
    with np.errstate(invalid="ignore", divide="ignore"):
        fair = ncdf(np.log(f.btc / start) / (f.vol_s * np.sqrt(tau)))
    f["fair"] = np.where(f.t < w.close - 60, fair, np.nan)
    f["m"], f["start"], f["target"] = w.m, start, w.target
    return f.reset_index(drop=True)


def results(tickers):
    """Official results for finished windows, cached next to the recordings."""
    path = livedata.LIVE_DIR / "results.json"
    cache = json.loads(path.read_text()) if path.exists() else {}
    for m in tickers:
        if m in cache:
            continue
        try:
            req = urllib.request.Request(f"{KALSHI_API}/markets/{m}", headers={"User-Agent": "btc15-live-edge/1.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                d = json.load(r)["market"]
            if d.get("result") in ("yes", "no"):
                cache[m] = d["result"]
        except Exception:
            pass
        time.sleep(0.2)
    path.write_text(json.dumps(cache))
    return cache


def lag_profile(frames, lags=range(-3, 16), res=1.0):
    """How much of a fair-value change over `res` seconds shows up in Kalshi's mid `lag` steps
    later (regression slope), plus the running total: the share absorbed within that time."""
    k = int(round(res / STEP))
    pairs = [(f.fair.iloc[::k].diff().to_numpy(), f.mid.iloc[::k].diff().to_numpy()) for f in frames]
    rows, total = [], 0.0
    for lag in lags:
        xs, ys = [], []
        for dx, dy in pairs:
            if lag >= 0:
                x, y = dx[:len(dx) - lag], dy[lag:]
            else:
                x, y = dx[-lag:], dy[:lag]
            ok = np.isfinite(x) & np.isfinite(y)
            xs.append(x[ok]), ys.append(y[ok])
        x, y = np.concatenate(xs), np.concatenate(ys)
        slope = float((x * y).sum() / (x * x).sum())
        total += slope if lag >= 0 else 0
        rows.append({"lag_s": lag * res, "slope": slope, "absorbed": total if lag >= 0 else np.nan,
                     "corr": float(np.corrcoef(x, y)[0, 1])})
    return pd.DataFrame(rows)


def speed_test(frames, outcome, thresholds=(0.02, 0.03, 0.05, 0.08), latencies=(0.25, 0.5, 1.0, 2.0),
               horizons=(10, 30, 60), cooldown=10, fee_rate=TAKER_FEE_RATE):
    """Buy when Kalshi's ask is below fair value by `threshold` (after the fee). The order goes in
    `latency` seconds later as a limit at the price we saw, so it only fills if the price hasn't moved
    against us. Score: Kalshi's mid `horizon` seconds after the fill minus what we paid (incl. fee),
    and the settled result minus what we paid."""
    def fee(p):
        return fee_rate * p * (1 - p)

    rows = []
    for th in thresholds:
        for lat in latencies:
            marks, signals = [], 0
            for f in frames:
                t, fair, bid, ask, mid = (f[c].to_numpy() for c in ("t", "fair", "bid", "ask", "mid"))
                e_yes, e_no = fair - ask - fee(ask), (1 - fair) - (1 - bid) - fee(1 - bid)
                with np.errstate(invalid="ignore"):
                    hits = np.flatnonzero(np.fmax(e_yes, e_no) >= th)
                quiet_until, d = -np.inf, int(round(lat / STEP))
                for i in hits:
                    if t[i] < quiet_until or i + d >= len(t):
                        continue
                    signals += 1
                    quiet_until = t[i] + cooldown
                    j = i + d
                    yes = e_yes[i] >= e_no[i]
                    price = ask[j] if yes else 1 - bid[j]
                    if not np.isfinite(price) or price > (ask[i] if yes else 1 - bid[i]) + 1e-9:
                        continue  # the price moved against us before our order arrived: no fill
                    row = {"m": f.m.iloc[0], "edge_seen": max(e_yes[i], e_no[i]), "price": price}
                    for h in horizons:
                        k = j + int(h / STEP)
                        v = mid[k] if k < len(t) else np.nan
                        row[f"mark{h}"] = (v if yes else 1 - v) - price - fee(price)
                    res = outcome.get(f.m.iloc[0])
                    row["settled"] = ((res == "yes") if yes else (res == "no")) - price - fee(price) if res else np.nan
                    marks.append(row)
            m = pd.DataFrame(marks)
            out = {"threshold_c": th * 100, "latency_s": lat, "signals": signals, "fills": len(m)}
            for col in [f"mark{h}" for h in horizons] + ["settled"]:
                if len(m) and m[col].notna().any():
                    per_window = m.groupby("m")[col].mean().dropna()  # windows as the unit: honest error bars
                    half = 1.96 * per_window.std() / math.sqrt(len(per_window)) if len(per_window) > 1 else np.nan
                    out[col] = f"{m[col].mean() * 100:+.2f} ± {half * 100:.2f}"
            rows.append(out)
    return pd.DataFrame(rows)


def top_of_book(rows):
    """Best YES bid and ask from recorded order books (each side lists bids, best last)."""
    bid = rows.yes.map(lambda x: x[-1][0] if x else np.nan).to_numpy()
    ask = rows.no.map(lambda x: round(1 - x[-1][0], 4) if x else np.nan).to_numpy()
    return bid, ask


def cross_market(data, fee_rate=TAKER_FEE_RATE):
    """The 15-minute window that ends on the hour vs the hourly 'BTC above $X' markets for that hour.
    Both settle on the same 60-second average, so:
      - hourly strike X at or above the window's target: hourly YES can't be worth more than 15-min YES.
        If hourly YES bid > 15-min YES ask (plus both fees), buy 15-min YES + buy hourly NO:
        pays at least $1 whatever happens.
      - hourly strike X below the target: 15-min YES can't be worth more than hourly YES.
        If 15-min YES bid > hourly YES ask (plus fees), buy 15-min NO + hourly YES: same guarantee.
    Reports how often (share of quarter seconds) such a locked-in profit was available, and how big."""
    if "hmkt" not in data or "hbook" not in data:
        return None

    def fee(p):
        return fee_rate * p * (1 - p)

    out = []
    w15 = data["mkt"].groupby("m").last()
    for window, hm in data["hmkt"].groupby("for"):
        if window not in w15.index:
            continue
        w = w15.loc[window]
        grid = np.arange(w.open, w.close - 1, STEP)
        b = data["book"][data["book"].m == window]
        if len(b) < 100:
            continue
        bid15, ask15 = (asof(b.t.to_numpy(), v, grid, max_age=10) for v in top_of_book(b))
        for h in hm.drop_duplicates("m").itertuples():
            hb = data["hbook"][data["hbook"].m == h.m]
            if hb.empty:
                continue
            bid_h, ask_h = (asof(hb.t.to_numpy(), v, grid, max_age=10) for v in top_of_book(hb))
            if h.strike >= w.target:
                gain = bid_h - ask15 - fee(ask15) - fee(1 - bid_h)      # buy 15-min YES + hourly NO
                # same pair with the 15-min leg as a free resting bid at the 15-min YES bid
                maker = bid_h - bid15 - fee(1 - bid_h)
            else:
                gain = bid15 - ask_h - fee(1 - bid15) - fee(ask_h)      # buy 15-min NO + hourly YES
                maker = ask15 - ask_h - fee(ask_h)                      # 15-min NO resting at 1 - YES ask
            ok = np.isfinite(gain) & np.isfinite(maker)
            if not ok.any():
                continue
            g, mk = gain[ok], maker[ok]
            out.append({"window": window, "strike": h.strike, "vs_target": round(h.strike - w.target, 2),
                        "seconds": ok.sum() * STEP, "share_profitable": float((g > 0).mean()),
                        "best_c": float(g.max() * 100), "median_c": float(np.median(g) * 100),
                        "maker_share": float((mk > 0).mean()), "maker_best_c": float(mk.max() * 100)})
    return pd.DataFrame(out)


def report():
    data = livedata.load(kinds=("mkt", "book", "cb", "kr", "hmkt", "hbook"))
    cm = cross_market(data)
    if cm is not None and len(cm):
        print("Hourly vs 15-minute (cents per pair after both fees; positive = locked-in profit)")
        print(cm.round(3).to_string(index=False), "\n")
    for series, coin in (("KXBTC15M", "BTC"), ("KXETH15M", "ETH")):
        if not spot_rows(data, coin):
            continue
        g = btc_grid(data, coin)
        frames = [f for f in (window_frame(r, g, data["book"]) for r in windows(data, series).itertuples())
                  if f is not None]
        hours = (g.t.iloc[-1] - g.t.iloc[0]) / 3600
        print(f"===== {coin}: {hours:.1f} hours recorded, {len(frames)} complete windows")
        if not frames:
            print("Not enough recording yet: each window needs the minute before it opened.\n")
            continue
        basis = pd.Series([f.start.iloc[0] - f.target.iloc[0] for f in frames])
        print(f"Coinbase/Kraken starting level minus Kalshi's official target: {basis.mean():+.2f} $ "
              f"(spread {basis.std():.2f} $)")
        print("\nLag: share of a fair-value move that Kalshi's price reflects within L seconds")
        print(lag_profile(frames).round(3).to_string(index=False))
        print("\nSame, in quarter seconds (each step includes up to 0.25 s of our own polling delay)")
        print(lag_profile(frames, lags=range(-4, 13), res=STEP).round(3).to_string(index=False))
        outcome = results([f.m.iloc[0] for f in frames])
        print("\nSpeed trading (cents per contract after fee, ± 95% range, windows as the unit)")
        print(speed_test(frames, outcome).to_string(index=False), "\n")


if __name__ == "__main__":
    report()
