"""Indicators vs Kalshi: does any chart indicator, alone or combined, beat Kalshi's price?

The bar isn't "right more than 50%". Kalshi's price already includes whatever is
obvious (it had already priced the 15-minute reversal, #18), so every test asks
whether an indicator adds information *beyond* Kalshi's own price, and whether
trading on it pays after the taker fee.

Data: every KXBTC15M window with Kalshi's minute quotes, plus Coinbase 1-minute BTC.
Decisions at the end of minute 1 of each window (also 5 and 10), using only data up
to that moment; indicators on 5- and 15-minute bars use the last bar that had closed.

1. Single indicators: walk-forward logistic regression of the result on Kalshi's
   price alone vs price + one indicator; out-of-sample log-loss gain, judged against
   a Bonferroni bar for the number of indicators tried.
2. Classic rule combos (squeeze + ADX, RSI + Bollinger, MACD + EMA + ADX, Donchian
   breakout + volume, stochastic + trend, 1-minute RSI, previous-window reversal):
   buy the side the rule picks at Kalshi's ask, after the fee.
3. Everything combined: logistic regression and gradient-boosted trees, retrained
   each month on all earlier months, traded with a 2c margin after the fee. Also at
   the next minute's price, in case the minute snapshots are a little stale.

Run: python indicator_combo.py
"""
import numpy as np
import pandas as pd
from scipy.stats import norm
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.inspection import permutation_importance
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

import backtest as bt
import fairvalue as fv
from config import TAKER_FEE_RATE
from indicator_test import adx, squeeze

MINUTES = (1, 5, 10)
MARGINS = (0.01, 0.02, 0.03, 0.05)
PRIMARY_MARGIN = 0.02
MAX_SPREAD = 0.03
SPLIT = pd.Timestamp("2026-05-12", tz="UTC").timestamp()  # same halves as indicator_test.py
FIRST_TEST_MONTH = pd.Period("2026-02", "M")


def fee(p):
    return TAKER_FEE_RATE * p * (1 - p)


def logit(p):
    p = np.clip(np.asarray(p, dtype=float), 0.01, 0.99)
    return np.log(p / (1 - p))


def rsi(close, n=14):
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn)


def bars(btc, minutes):
    """Clock-aligned bars from the 1-minute grid, indexed by the time each bar ENDS (unix s)."""
    key = (btc.index.to_numpy() // (60 * minutes)) * (60 * minutes) + 60 * minutes
    return btc.groupby(key).agg(open=("open", "first"), high=("high", "max"), low=("low", "min"),
                                close=("close", "last"), volume=("volume", "sum"))


def bar_features(d, tag):
    c = d.close
    ema = lambda n: c.ewm(span=n, adjust=False).mean()
    f = pd.DataFrame(index=d.index)
    f[f"rsi_{tag}"] = rsi(c)
    macd = ema(12) - ema(26)
    f[f"macd_{tag}"] = (macd - macd.ewm(span=9, adjust=False).mean()) / c
    sma, sd = c.rolling(20).mean(), c.rolling(20).std(ddof=0)
    f[f"bb_pctb_{tag}"] = (c - (sma - 2 * sd)) / (4 * sd)
    f[f"bb_width_{tag}"] = np.log(4 * sd / sma)
    on, mom, fired = squeeze(d)
    f[f"sqz_on_{tag}"] = on.astype(float)
    f[f"sqz_fired_{tag}"] = fired.astype(float)
    f[f"sqz_mom_{tag}"] = mom / c
    a, pdi, mdi = adx(d)
    f[f"adx_{tag}"] = a
    f[f"di_{tag}"] = pdi - mdi
    lo14, hi14 = d.low.rolling(14).min(), d.high.rolling(14).max()
    f[f"stoch_{tag}"] = (100 * (c - lo14) / (hi14 - lo14)).rolling(3).mean()
    lo20, hi20 = d.low.rolling(20).min(), d.high.rolling(20).max()
    f[f"donch_{tag}"] = (c - lo20) / (hi20 - lo20)
    f[f"breakout_{tag}"] = (c > hi20.shift()).astype(float) - (c < lo20.shift()).astype(float)
    f[f"ema9_21_{tag}"] = (ema(9) - ema(21)) / c
    f[f"ema50_200_{tag}"] = (ema(50) - ema(200)) / c
    f[f"volz_{tag}"] = np.log((d.volume + 1) / (d.volume.rolling(96).mean() + 1))
    return f.replace([np.inf, -np.inf], np.nan)


def minute_features(btc):
    lp, c, v = btc.logp, btc.close, btc.volume
    r = lp.diff()
    f = pd.DataFrame(index=btc.index)
    for k in (1, 5, 15, 30, 60, 240):
        f[f"ret_{k}m"] = lp - lp.shift(k)
    f["vol_15m"] = r.rolling(15).std()
    f["vol_60m"] = r.rolling(60).std()
    f["vol_ratio"] = np.log(f.vol_15m / f.vol_60m)
    f["rsi_1m"] = rsi(c)
    f["vwap60_dev"] = np.log(c / ((c * v).rolling(60).sum() / v.rolling(60).sum()))
    v15 = v.rolling(15).sum()
    f["volz_1m"] = np.log((v15 + 1) / (v15.rolling(1440).mean() + 1))
    return f.replace([np.inf, -np.inf], np.nan)


def dataset():
    """One row per window per decision minute: Kalshi quotes, the result, and every indicator."""
    markets, s, btc = fv.model_table()
    with bt.connect() as con:
        vol = pd.read_sql("SELECT ts, volume FROM btc_1m", con).set_index("ts").volume
    btc["volume"] = vol.reindex(btc.index).fillna(0)
    s = s[s.minute.isin(MINUTES)].reset_index(drop=True)
    now = (s.open_ts + 60 * s.minute).to_numpy()
    t0 = int(btc.index[0])
    mf = minute_features(btc)
    X = pd.DataFrame(mf.to_numpy()[(now - 60 - t0) // 60], columns=mf.columns)
    for m, tag in ((5, "5m"), (15, "15m")):
        bf = bar_features(bars(btc, m), tag)
        pos = np.searchsorted(bf.index.to_numpy(), now, side="right") - 1  # last bar closed by now
        part = pd.DataFrame(bf.to_numpy()[pos.clip(0)], columns=bf.columns)
        part[pos < 0] = np.nan
        X = pd.concat([X, part], axis=1)
    # previous window (only when it ended exactly as this one opened)
    mk = markets.sort_values("close_ts")
    prev = pd.DataFrame({"ticker": mk.ticker, "prev_yes": mk.yes_won.shift().where(mk.open_ts == mk.close_ts.shift())})
    X["prev_yes"] = s[["ticker"]].merge(prev, on="ticker", how="left").prev_yes.to_numpy()
    local = pd.to_datetime(now, unit="s", utc=True).tz_convert("America/New_York")
    hour = local.hour + local.minute / 60
    X["hour_sin"], X["hour_cos"] = np.sin(2 * np.pi * hour / 24), np.cos(2 * np.pi * hour / 24)
    X["weekend"] = (local.dayofweek >= 5).astype(float)
    # what Kalshi and the textbook model already say
    X["logit_mid"] = logit((s.yes_bid + s.yes_ask) / 2)
    X["spread"] = s.yes_ask - s.yes_bid
    X["logit_p_normal"] = logit(s.p_normal)
    meta = s[["ticker", "minute", "open_ts", "close_ts", "yes_bid", "yes_ask", "next_bid", "next_ask", "yes_won"]]
    ok = (meta.yes_bid > 0) & (meta.yes_ask < 1) & (meta.yes_ask - meta.yes_bid <= MAX_SPREAD + 1e-9)
    meta = meta[ok].reset_index(drop=True).assign(month=lambda d: pd.to_datetime(d.close_ts, unit="s").dt.to_period("M"))
    return meta, X[ok.to_numpy()].reset_index(drop=True)


INDICATORS = None  # filled in main(): every column that isn't Kalshi's own price


def walk_forward(meta, X, make_model, cols):
    """Out-of-sample probability for every row from FIRST_TEST_MONTH on: each month is
    predicted by a model trained only on earlier months."""
    p = np.full(len(meta), np.nan)
    for month in sorted(m for m in meta.month.unique() if m >= FIRST_TEST_MONTH):
        tr, te = (meta.month < month).to_numpy(), (meta.month == month).to_numpy()
        model = make_model()
        model.fit(X.loc[tr, cols], meta.yes_won[tr])
        p[te] = model.predict_proba(X.loc[te, cols])[:, 1]
    return p


def lr(c=1.0):
    return lambda: make_pipeline(SimpleImputer(strategy="median"), StandardScaler(),
                                 LogisticRegression(C=c, max_iter=2000))


def hgb():
    return HistGradientBoostingClassifier(max_iter=200, learning_rate=0.05, max_leaf_nodes=15,
                                          min_samples_leaf=200, l2_regularization=1.0, random_state=0)


def logloss(p, y):
    p = np.clip(p, 1e-4, 1 - 1e-4)
    return -(y * np.log(p) + (1 - y) * np.log(1 - p))


def single_indicators(meta, X, minute):
    m = (meta.minute == minute).to_numpy()
    mt, Xm = meta[m].reset_index(drop=True), X[m].reset_index(drop=True)
    y = mt.yes_won.to_numpy()
    base = walk_forward(mt, Xm, lr(), ["logit_mid"])
    test = ~np.isnan(base)
    rows = []
    for col in INDICATORS:
        x = Xm[col].to_numpy()
        ok = test & ~np.isnan(x)
        raw = roc_auc_score(y[~np.isnan(x)], x[~np.isnan(x)]) if np.nanstd(x) > 0 else 0.5
        p = walk_forward(mt, Xm, lr(), ["logit_mid", col])
        d = logloss(base[ok], y[ok]) - logloss(p[ok], y[ok])  # > 0 = the indicator helped
        rows.append((col, raw, d.mean() * 1000, d.std() / np.sqrt(ok.sum()) * 1000, ok.sum()))
    out = pd.DataFrame(rows, columns=["indicator", "raw_auc", "gain_mnats", "se", "n"])
    out["z"] = out.gain_mnats / out.se
    bar = norm.ppf(1 - 0.05 / len(out))  # one-sided, 5% across all indicators tried
    out["passes_bonferroni"] = out.z > bar
    return out.sort_values("z", ascending=False), bar


def trade(meta, side, delay=False):
    """P&L per contract for buying `side` (+1 YES, -1 NO, 0 skip) at the ask (or next minute's ask)."""
    bid, ask = (meta.next_bid, meta.next_ask) if delay else (meta.yes_bid, meta.yes_ask)
    price = np.where(side > 0, ask, 1 - bid)
    won = np.where(side > 0, meta.yes_won, 1 - meta.yes_won)
    ok = (side != 0) & ~np.isnan(price) & (price > 0) & (price < 1)
    t = pd.DataFrame({"open_ts": meta.open_ts[ok], "price": price[ok], "won": won[ok]})
    t["pnl"] = t.won - t.price - fee(t.price)
    return t


def describe(t, label):
    parts = []
    for name, sub in (("1st half", t[t.open_ts < SPLIT]), ("2nd half", t[t.open_ts >= SPLIT])):
        if len(sub) < 30:
            parts.append(f"{name}: {len(sub)} trades")
            continue
        m, h = sub.pnl.mean() * 100, 1.96 * sub.pnl.std() / np.sqrt(len(sub)) * 100
        parts.append(f"{name}: {len(sub):5,d} trades, won {sub.won.mean():.1%} at {sub.price.mean():.3f}, "
                     f"{m:+.2f}c [{m - h:+.2f}, {m + h:+.2f}]")
    return f"  {label:42s} " + " | ".join(parts)


def rules(X):
    """Classic TradingView-style combos. +1 = buy YES (expect up), -1 = buy NO, 0 = no trade."""
    pick = lambda up, down: np.where(up, 1, np.where(down, -1, 0))
    return {
        "previous window reversal": pick(X.prev_yes == 0, X.prev_yes == 1),
        "squeeze fired + ADX>20 (5m)": pick((X.sqz_fired_5m == 1) & (X.adx_5m > 20) & (X.sqz_mom_5m > 0),
                                            (X.sqz_fired_5m == 1) & (X.adx_5m > 20) & (X.sqz_mom_5m < 0)),
        "RSI<30 below band / RSI>70 above (5m)": pick((X.rsi_5m < 30) & (X.bb_pctb_5m < 0),
                                                       (X.rsi_5m > 70) & (X.bb_pctb_5m > 1)),
        "MACD + EMA9/21 + ADX>25 trend (5m)": pick((X.macd_5m > 0) & (X.ema9_21_5m > 0) & (X.adx_5m > 25),
                                                    (X.macd_5m < 0) & (X.ema9_21_5m < 0) & (X.adx_5m > 25)),
        "Donchian breakout + volume (15m)": pick((X.breakout_15m == 1) & (X.volz_15m > 0.5),
                                                  (X.breakout_15m == -1) & (X.volz_15m > 0.5)),
        "stochastic dip in uptrend (5m/15m)": pick((X.stoch_5m < 20) & (X.ema50_200_15m > 0),
                                                    (X.stoch_5m > 80) & (X.ema50_200_15m < 0)),
        "1-min RSI extremes, fade": pick(X.rsi_1m < 25, X.rsi_1m > 75),
    }


def combined(meta, X, minute):
    m = (meta.minute == minute).to_numpy()
    mt, Xm = meta[m].reset_index(drop=True), X[m].reset_index(drop=True)
    cols = list(X.columns)
    preds = {"Kalshi mid": 1 / (1 + np.exp(-Xm.logit_mid.to_numpy())),
             "logistic (all)": walk_forward(mt, Xm, lr(0.05), cols),
             "boosted trees (all)": walk_forward(mt, Xm, hgb, cols)}
    test = ~np.isnan(preds["logistic (all)"])
    y = mt.yes_won.to_numpy()
    print(f"\n--- minute {minute}: {test.sum():,} out-of-sample windows "
          f"({mt.month[test].min()} to {mt.month[test].max()}) ---")
    for name, p in preds.items():
        for half, h in (("1st", mt.open_ts < SPLIT), ("2nd", mt.open_ts >= SPLIT)):
            k = test & h.to_numpy()
            print(f"  {name:22s} {half} half: log loss {logloss(p[k], y[k]).mean():.4f}, "
                  f"Brier {((p[k] - y[k]) ** 2).mean():.4f}")
    for name in ("logistic (all)", "boosted trees (all)"):
        p = preds[name]
        e_yes = p - mt.yes_ask - fee(mt.yes_ask)
        e_no = (1 - p) - (1 - mt.yes_bid) - fee(1 - mt.yes_bid)
        for margin in MARGINS:
            side = np.where(test & (e_yes >= e_no) & (e_yes > margin), 1,
                            np.where(test & (e_no > e_yes) & (e_no > margin), -1, 0))
            star = " <- pre-registered" if margin == PRIMARY_MARGIN else ""
            print(describe(trade(mt, side), f"{name}, margin {margin * 100:.0f}c{star}"))
            if margin == PRIMARY_MARGIN:
                print(describe(trade(mt, side, delay=True), f"{name}, margin {margin * 100:.0f}c, next-min price"))
    return mt, Xm, preds, test


def importance(mt, Xm, test):
    """Which inputs the boosted-trees model leans on, measured on the newest out-of-sample months."""
    cols = list(Xm.columns)
    months = sorted(mt.month[test].unique())
    tr = (mt.month < months[-2]).to_numpy()
    te = (mt.month >= months[-2]).to_numpy()
    model = hgb().fit(Xm.loc[tr, cols], mt.yes_won[tr])
    r = permutation_importance(model, Xm.loc[te, cols], mt.yes_won[te], scoring="neg_log_loss",
                               n_repeats=5, random_state=0)
    imp = pd.Series(r.importances_mean * 1000, index=cols).sort_values(ascending=False)
    print("\n  boosted trees, log-loss damage when each input is scrambled (milli-nats, top 12):")
    for k, v in imp.head(12).items():
        print(f"    {k:18s} {v:+.2f}")


def main():
    global INDICATORS
    meta, X = dataset()
    INDICATORS = [c for c in X.columns if c not in ("logit_mid", "spread")]
    print(f"{meta.ticker.nunique():,} windows, {len(meta):,} window-decisions, {len(X.columns)} inputs")

    print("\n=== 1. Single indicators at minute 1: out-of-sample gain over Kalshi's price alone ===")
    single, bar = single_indicators(meta, X, 1)
    print(f"  gain in milli-nats of log loss per window (> 0 helps); Bonferroni bar z > {bar:.2f}")
    print(f"  raw_auc = how well the indicator alone sorts up vs down windows (0.5 = coin flip)")
    print(single.round(3).to_string(index=False))

    print("\n=== 2. Rule combos at minute 1, taker at the ask, after the fee ===")
    m1 = (meta.minute == 1).to_numpy()
    mt, Xm = meta[m1].reset_index(drop=True), X[m1].reset_index(drop=True)
    for name, side in rules(Xm).items():
        print(describe(trade(mt, side), name))
        print(describe(trade(mt, side, delay=True), "  same, at the next minute's price"))

    print("\n=== 3. All indicators combined, walk-forward by month ===")
    for minute in MINUTES:
        mt, Xm, preds, test = combined(meta, X, minute)
        if minute == 1:
            importance(mt, Xm, test)


if __name__ == "__main__":
    main()
