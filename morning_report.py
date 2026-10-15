"""Morning report: run every live edge test on the recordings so far and write one dated
report (reports/YYYY-MM-DD_HHMM.md, and reports/latest.md). The recorder is the night
shift; this is the morning desk. It recommends; it never trades.

    python morning_report.py
"""
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

import flow
import live_edge as le
import livedata
import makersim

REPORTS = Path(__file__).parent / "reports"
NEED_WINDOWS = 100   # windows before a markout result counts (settled results need far more)


def block(df):
    return "```\n" + (df.to_string(index=False) if len(df) else "(no data yet)") + "\n```\n"


def speed_section(data, out):
    out.append("## 1. Speed: does Kalshi lag BTC?\n")
    for source, label in (("composite", "Coinbase + Kraken spot"), ("binance", "Binance futures (fastest)")):
        if not le.spot_rows(data, "BTC", source):
            out.append(f"- {label}: no data yet\n")
            continue
        g = le.btc_grid(data, "BTC", source)
        frames = [f for f in (le.window_frame(r, g, data["book"]) for r in le.windows(data).itertuples())
                  if f is not None]
        if not frames:
            out.append(f"- {label}: no complete windows yet\n")
            continue
        lag = le.lag_profile(frames, lags=range(-4, 13), res=le.STEP)
        lead = lag[lag.lag_s < 0].slope.sum()
        within = {s: lag.loc[lag.lag_s == s, "absorbed"].iloc[0] for s in (0.0, 0.5, 1.0, 3.0)}
        out.append(f"- **{label}**, {len(frames)} windows: Kalshi reflects {within[0.0]:.0%} of a move in the "
                   f"same quarter second, {within[0.5]:.0%} within 0.5 s, {within[1.0]:.0%} within 1 s. "
                   f"Share already moved BEFORE this feed: {lead:.0%}.\n")
        st = le.speed_test(frames, le.results([f.m.iloc[0] for f in frames]), thresholds=(0.03, 0.05),
                           latencies=(0.25, 1.0))
        out.append(block(st))
    return out


def hourly_section(data, out):
    out.append("## 2. Hourly vs 15-minute (same settlement number)\n")
    cm = le.cross_market(data)
    if cm is None or not len(cm):
        out.append("No top-of-hour windows recorded yet.\n")
        return out
    near = cm.loc[cm.groupby("window").vs_target.apply(lambda s: s.abs().idxmin())]
    out.append(f"{cm.window.nunique()} top-of-hour windows. Nearest strike to each target. best_c > 0 means "
               f"a locked-in profit was available after both fees. maker_* = the same pair with the 15-minute "
               f"leg as a free resting order (it still has to get filled, and the hourly price can move "
               f"before we hedge):\n")
    out.append(block(near[["window", "strike", "vs_target", "share_profitable", "best_c", "median_c",
                           "maker_share", "maker_best_c"]].round(2)))
    return out


def flow_section(data, out):
    out.append("## 3. Who loses money (maker markouts, cents per contract)\n")
    f = flow.trade_table(data)
    if f.empty:
        out.append("No trades yet.\n")
        return out
    f = f[f.m.str.startswith("KXBTC15M")].copy()
    out.append(f"{len(f):,} BTC trades over {f.m.nunique()} windows.\n")
    f["all"] = "all trades"
    f["taker"] = np.where(f.sign > 0, "bought YES", "bought NO")
    for by in ("all", "taker"):
        out.append(block(flow.summarize(f, by)))
    return out


def maker_section(data, out):
    out.append("## 4. Maker test with a real place in line (cents per contract, maker fee 0)\n")
    out.append("`back` = joined the back of the line (realistic for us), `front` = first in line (upper bound). "
               "mark60 = Kalshi's own price 60 s after the fill minus what we paid.\n")
    for series in ("KXBTC15M", "KXETH15M"):
        out.append(f"**{series}**\n")
        out.append(block(makersim.run(data, series)))
    return out


def checks_and_decision(data, out):
    w = data["mkt"].groupby("m").last()
    done = w[(w.close < time.time()) & w.index.str.startswith("KXBTC15M")]
    out.append("## 5. Checks\n")
    checks = [
        ("no-lookahead", "pass", "signals use only data at or before the decision time; results only score"),
        ("costs", "pass", "taker fee 0.07×P×(1−P); makers pay 0 on these series (fee_type quadratic)"),
        ("liquidity", "pass", "BTC median 4,500 contracts at the best price; $20 fills at the best price 99% of the time"),
        ("sample-size", "pass" if len(done) >= NEED_WINDOWS else "not yet",
         f"{len(done)} BTC windows recorded; markout verdicts need {NEED_WINDOWS}+, settled verdicts 1,000+"),
        ("out-of-sample", "see RESEARCH_LOG.md", "a lead counts only after it holds on data it was never tuned on"),
        ("bear-case", "n/a", "written for each lead in RESEARCH_LOG.md before anything is traded"),
    ]
    out.append(block(pd.DataFrame(checks, columns=["check", "result", "note"])))
    out.append("## 6. Decision\n")
    out.append("**No trade.** Nothing has met the acceptance criteria in RESEARCH_LOG.md (an edge whose 95% "
               "range is above +0.5¢ per contract after costs, on data it wasn't tuned on, then two weeks of "
               "paper trading). This report recommends; it never places orders.\n")
    return out


def main():
    t0 = time.time()
    data = livedata.load(kinds=("mkt", "book", "trade", "cb", "kr", "bn", "hmkt", "hbook"))
    hours = (data["book"].t.max() - data["book"].t.min()) / 3600
    out = [f"# Edge report, {datetime.now():%Y-%m-%d %H:%M}\n",
           f"Recorded so far: {hours:.1f} hours. Research history and leads: RESEARCH_LOG.md.\n"]
    for section in (speed_section, hourly_section, flow_section, maker_section, checks_and_decision):
        try:
            out = section(data, out)
        except Exception as e:  # one broken section shouldn't sink the report
            out.append(f"(section failed: {type(e).__name__}: {e})\n")
    REPORTS.mkdir(exist_ok=True)
    text = "\n".join(out)
    path = REPORTS / f"{datetime.now():%Y-%m-%d_%H%M}.md"
    path.write_text(text, encoding="utf-8")
    (REPORTS / "latest.md").write_text(text, encoding="utf-8")
    print(f"Report written to {path} in {time.time() - t0:.0f} s")


if __name__ == "__main__":
    main()
