"""BTC 15-minute lab: is the side Kalshi prices near 60 cents really a 60% shot?

Start it with 2_open_dashboard.bat (or `streamlit run app.py`). The data comes
from fetch_data.py; the math lives in backtest.py.
"""
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

import backtest as bt
import fairvalue as fv
from config import DB_PATH, TAKER_FEE_RATE

ET = ZoneInfo("America/New_York")  # Kalshi lists these markets in New York time
FONT = 'system-ui, -apple-system, "Segoe UI", sans-serif'

# Colors from the validated reference palette: slot 1 (blue) carries the data,
# slot 2 (orange) marks the one trade being highlighted, blue/red is the
# gain/loss pair, and grays are for text, grid and axes.
LIGHT = {"surface": "#fcfcfb", "ink": "#0b0b0b", "ink2": "#52514e", "muted": "#898781", "grid": "#e1e0d9",
         "axis": "#c3c2b7", "data": "#2a78d6", "accent": "#eb6834", "gain": "#2a78d6", "loss": "#e34948"}
DARK = {"surface": "#1a1a19", "ink": "#f0efec", "ink2": "#c3c2b7", "muted": "#898781", "grid": "#2c2c2a",
        "axis": "#383835", "data": "#3987e5", "accent": "#d95926", "gain": "#3987e5", "loss": "#e66767"}

st.set_page_config(page_title="BTC 15-min lab", page_icon="📈", layout="wide")
try:
    C = DARK if st.context.theme.type == "dark" else LIGHT
except AttributeError:
    C = LIGHT


# ---------- small helpers ----------

def et(unix):
    """Unix seconds -> New York wall-clock time, without tz info (Plotly wants naive times)."""
    if np.isscalar(unix):
        return pd.Timestamp(int(unix), unit="s", tz="UTC").tz_convert(ET).tz_localize(None)
    return pd.to_datetime(np.asarray(unix), unit="s", utc=True).tz_convert(ET).tz_localize(None)


def clock(t):
    return t.strftime("%I:%M %p").lstrip("0")


def day_label(d):
    return f"{d:%b} {d.day}, {d.year}"


def pct(x, signed=False):
    return f"{x * 100:+.1f}%" if signed else f"{x * 100:.1f}%"


def cents(x):
    return f"{x * 100:+.2f}¢"


def wash(hex_color, alpha):
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return f"rgba({r},{g},{b},{alpha})"


def style(fig, height, x_title=None, y_title=None):
    fig.update_layout(
        height=height, margin=dict(l=8, r=16, t=32, b=8), showlegend=False, barcornerradius=4,
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family=FONT, size=13, color=C["ink2"]),
        hoverlabel=dict(bgcolor=C["surface"], bordercolor=C["grid"], font=dict(family=FONT, color=C["ink"])),
    )
    # automargin grows the margins to fit tick labels and titles (without it, minus signs get clipped)
    fig.update_xaxes(title=x_title, showgrid=False, zeroline=False, showline=True, automargin=True,
                     linecolor=C["axis"], ticks="outside", tickcolor=C["axis"])
    fig.update_yaxes(title=y_title, gridcolor=C["grid"], gridwidth=1, zeroline=False, showline=False,
                     automargin=True)
    return fig


def bar_width(categories):
    """Bar width in category units that stays near 24 px on a ~1000 px wide chart."""
    return min(0.35, 0.024 * categories)


def show(fig):
    st.plotly_chart(fig, theme=None, config={"displayModeBar": False})


# ---------- data (cached so the page stays quick) ----------

@st.cache_resource(ttl=900, show_spinner="Loading data...")
def load_data():
    markets, candles = bt.load()
    return markets, bt.snapshots(markets, candles)


@st.cache_resource(max_entries=6, show_spinner=False)
def quotes_for(side, spread_c, fee_rate, t0, t1):
    _, snap = load_data()
    return bt.side_quotes(snap[(snap.close_ts > t0) & (snap.close_ts <= t1)], side, spread_c / 100, fee_rate)


@st.cache_data(max_entries=12, show_spinner=False)
def price_buckets(side, spread_c, fee_rate, t0, t1, entry):
    return bt.edge_by_price(quotes_for(side, spread_c, fee_rate, t0, t1), entry)


@st.cache_data(max_entries=24, show_spinner="Simulating 2,000 futures...")
def simulate(trades, rule, days, start, bet_frac, flat, fee_rate):
    return bt.simulate(trades, rule, days, start, bet_frac=bet_frac, flat=flat, fee_rate=fee_rate)


@st.cache_resource(ttl=900, show_spinner="Pricing every minute of every window...")
def model_data():
    markets, s, btc = fv.model_table()
    return s, fv.coinbase_agreement(markets, btc)


@st.cache_data(max_entries=12, show_spinner=False)
def model_candidates(model, delay, entry, spread_c, fee_rate, t0, t1):
    s, _ = model_data()
    return fv.candidates(s[(s.close_ts > t0) & (s.close_ts <= t1)], model, delay, entry, spread_c / 100, fee_rate)


# ---------- charts ----------

def edge_chart(df, x, x_title, bar_width, hover, zone=None):
    fig = go.Figure()
    if zone:
        fig.add_vrect(x0=zone[0], x1=zone[1], fillcolor=wash(C["data"], 0.08), line_width=0, layer="below",
                      annotation_text="your zone", annotation_position="top left",
                      annotation_font=dict(color=C["ink2"], size=12))
    fig.add_bar(
        x=x, y=df.edge * 100, width=bar_width,
        marker=dict(color=np.where(df.edge >= 0, C["gain"], C["loss"]).tolist(), line_width=0),
        error_y=dict(type="data", array=(df.half * 100).tolist(), color=C["ink2"], thickness=1, width=0),
        customdata=np.column_stack([df.win * 100, df.price * 100, df.windows, df.half * 100]),
        hovertemplate=hover,
    )
    fig.add_hline(y=0, line=dict(color=C["axis"], width=1))
    return style(fig, 320, x_title, "Edge (¢ per contract)")


def fan_chart(paths, start):
    days = np.arange(paths.shape[0])
    p10, p25, p50, p75, p90 = np.maximum(np.percentile(paths, [10, 25, 50, 75, 90], axis=1), 1)
    fig = go.Figure()
    for top, bottom in ((p90, p10), (p75, p25)):  # two 10% washes: the inner band reads darker
        fig.add_scatter(x=days, y=top, mode="lines", line=dict(width=0), hoverinfo="skip")
        fig.add_scatter(x=days, y=bottom, mode="lines", line=dict(width=0), fill="tonexty",
                        fillcolor=wash(C["data"], 0.10), hoverinfo="skip")
    fig.add_scatter(
        x=days, y=p50, mode="lines", line=dict(color=C["data"], width=2),
        customdata=np.column_stack([p25, p75, p10, p90]),
        hovertemplate=("Day %{x}<br>Middle run: $%{y:,.2f}<br>Middle half: $%{customdata[0]:,.0f} to "
                       "$%{customdata[1]:,.0f}<br>8 in 10 runs: $%{customdata[2]:,.0f} to $%{customdata[3]:,.0f}"
                       "<extra></extra>"),
    )
    # On a log axis, annotations take log10 values but lines don't, so the labels are placed by hand.
    fig.add_hline(y=start, line=dict(color=C["muted"], width=1))
    fig.add_annotation(x=0, y=np.log10(start), text=f"start ${start:,.0f}", showarrow=False,
                       xanchor="left", yanchor="bottom", font=dict(color=C["ink2"], size=12))
    fig.add_annotation(x=days[-1], y=np.log10(p50[-1]), text=f"middle run ${p50[-1]:,.0f}", showarrow=False,
                       xanchor="right", yanchor="bottom", font=dict(color=C["ink"], size=12))
    style(fig, 380, "Day", "Bankroll (log scale)")
    ticks = [m * 10 ** e for e in range(7) for m in (1, 2, 5)]
    fig.update_yaxes(type="log", tickvals=ticks, ticktext=[f"${t:,}" for t in ticks])
    fig.update_layout(hovermode="x")
    return fig


def replay_chart(m, quotes, btc, trade, zone, side):
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.12,
                        subplot_titles=("BTC price (Coinbase, 1-minute)", "Price of YES (¢)"))
    fig.for_each_annotation(lambda a: a.update(x=0, xanchor="left", font=dict(size=13, color=C["ink"])))

    if not btc.empty:
        fig.add_scatter(x=et(btc.ts), y=btc.close, mode="lines", line=dict(color=C["data"], width=2),
                        hovertemplate="%{x|%-I:%M %p}<br>BTC $%{y:,.2f}<extra></extra>", row=1, col=1)
    if pd.notna(m.target):  # a trace rather than a shape, so the y-axis always makes room for it
        x0 = et(m.open_ts - 120)
        fig.add_scatter(x=[x0, et(m.close_ts)], y=[m.target, m.target], mode="lines",
                        line=dict(color=C["muted"], width=1), hovertemplate="Target $%{y:,.2f}<extra></extra>",
                        row=1, col=1)
        fig.add_annotation(x=x0, y=m.target, text=f"target ${m.target:,.2f}", showarrow=False, xanchor="left",
                           yanchor="bottom", font=dict(color=C["ink2"], size=12), row=1, col=1)
    if pd.notna(m.final):
        fig.add_scatter(x=[et(m.close_ts)], y=[m.final], mode="markers",
                        marker=dict(size=10, color=C["data"], line=dict(color=C["surface"], width=2)),
                        hovertemplate="Kalshi final $%{y:,.2f}<extra></extra>", row=1, col=1)

    lo, hi = zone
    bands = ([(lo, hi)] if side != "no" else []) + ([(100 - hi, 100 - lo)] if side != "yes" else [])
    for a, b in bands:  # exclude_empty_subplots=False: the panel has no traces yet at this point
        fig.add_hrect(y0=a, y1=b, fillcolor=wash(C["data"], 0.08), line_width=0, layer="below", row=2, col=1,
                      exclude_empty_subplots=False)
    t = et(m.open_ts + quotes.minute.to_numpy() * 60)
    fig.add_scatter(x=t, y=quotes.yes_ask * 100, mode="lines", line=dict(width=0), hoverinfo="skip", row=2, col=1)
    fig.add_scatter(x=t, y=quotes.yes_bid * 100, mode="lines", line=dict(width=0), fill="tonexty",
                    fillcolor=wash(C["data"], 0.15), hoverinfo="skip", row=2, col=1)
    fig.add_scatter(x=t, y=(quotes.yes_bid + quotes.yes_ask) * 50, mode="lines", line=dict(color=C["data"], width=2),
                    customdata=np.column_stack([quotes.yes_bid * 100, quotes.yes_ask * 100]),
                    hovertemplate="%{x|%-I:%M %p}<br>YES bid %{customdata[0]:.1f}¢, ask %{customdata[1]:.1f}¢"
                                  "<extra></extra>", row=2, col=1)
    if trade is not None:
        x = et(m.open_ts + trade.minute * 60)
        y = trade.price * 100 if trade.side == "YES" else (1 - trade.price) * 100
        fig.add_scatter(x=[x], y=[y], mode="markers", hoverinfo="skip",
                        marker=dict(size=12, color=C["accent"], line=dict(color=C["surface"], width=2)), row=2, col=1)
        fig.add_annotation(x=x, y=y, text=f"bought {trade.side} at {trade.price * 100:.0f}¢", showarrow=True,
                           arrowcolor=C["muted"], arrowwidth=1, ax=48, ay=-32,
                           font=dict(color=C["ink"], size=12), row=2, col=1)

    style(fig, 560)
    fig.update_xaxes(tickformat="%-I:%M %p")
    fig.update_yaxes(tickprefix="$", tickformat=",.2~f", row=1, col=1)
    fig.update_yaxes(range=[0, 100], ticksuffix="¢", row=2, col=1)
    return fig


def stat_tiles(stats):
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Win rate", pct(stats["win_rate"]), help="How often the side you bought won.")
    c2.metric("Needed to break even", pct(stats["breakeven"]),
              help="Average price paid plus the fee. You have to win more often than this to make money.")
    c3.metric("Edge per contract", cents(stats["edge"]),
              help=f"Average profit per $1 contract after fees. 95% range: {cents(stats['edge_lo'])} "
                   f"to {cents(stats['edge_hi'])}.")
    c4.metric("Return per $1 risked", pct(stats["roi"], signed=True))


def verdict(stats, trades):
    span = f"95% range {stats['edge_lo'] * 100:+.1f}¢ to {stats['edge_hi'] * 100:+.1f}¢ per contract"
    if stats["n"] < 300:
        st.warning(f"Only {stats['n']} trades, which is too few to judge ({span}).", icon="⚠️")
    elif stats["edge_hi"] < 0:
        st.error(f"**Loses money.** Winning {pct(stats['win_rate'])} of the time wasn't enough to cover the "
                 f"{pct(stats['breakeven'])} needed, so you'd lose about {-stats['edge'] * 100:.1f}¢ per "
                 f"contract ({span}).", icon="❌")
    elif stats["edge_lo"] > 0:
        st.success(f"**Made money in the backtest**, about {stats['edge'] * 100:+.1f}¢ per contract ({span}). "
                   "Before trusting it, check it holds in the newest 30% below, and remember that if you try "
                   "many settings, one will look good by luck.", icon="✅")
    else:
        st.warning(f"**Too close to call.** The edge is somewhere between {stats['edge_lo'] * 100:+.1f}¢ and "
                   f"{stats['edge_hi'] * 100:+.1f}¢ per contract, so this data can't tell it apart from zero.",
                   icon="➖")
    older, newer = bt.split_check(trades)
    if older and newer:
        st.caption(f"Older 70% of trades: {cents(older['edge'])} per contract. Newest 30%: "
                   f"{cents(newer['edge'])}. A real edge should show up in both.")


# ---------- page ----------

st.title("BTC 15-minute lab")
st.caption("One idea, tested on Kalshi's real history: buy the side priced around 60¢ in the "
           "\"BTC up in the next 15 minutes?\" markets. Does it win often enough to beat the price plus fees, "
           "and what does betting 20% a trade do to $100?")

if not DB_PATH.exists():
    st.info("No data yet. Double-click **1_download_data.bat**, then refresh this page.")
    st.stop()
markets, snap = load_data()
if markets.empty:
    st.info("The download has started but no windows are finished yet. Refresh in a minute.")
    st.stop()

open_et = et(markets.open_ts)
first_day, last_day = open_et.min().date(), open_et.max().date()

with st.sidebar:
    st.header("The bet")
    zone = st.slider("Price zone (¢)", 50, 95, (55, 65),
                     help="Buy a side when its ask price is inside this range.")
    side = {"Either side": "either", "YES (BTC up) only": "yes", "NO (BTC down) only": "no"}[
        st.radio("Side", ["Either side", "YES (BTC up) only", "NO (BTC down) only"],
                 help="Either side = buy whichever side is priced in your zone, i.e. the market's favorite.")]
    entry = st.slider("Minutes into the window", 1, bt.LAST_ENTRY_MINUTE, (1, bt.LAST_ENTRY_MINUTE),
                      help="Only buy during these minutes. Each window trades at most once, "
                           "at the first minute that qualifies.")
    spread_c = st.slider("Skip when the bid-ask spread is over (¢)", 1, 10, 3)
    fee_rate = st.number_input("Taker fee rate", 0.0, 0.2, TAKER_FEE_RATE, 0.005, format="%.3f",
                               help="Kalshi's fee per contract = rate x price x (1 - price), rounded up "
                                    "per order. Check kalshi.com/fee-schedule.")
    picked = st.date_input("Dates", (first_day, last_day), first_day, last_day)
    d0, d1 = picked if len(picked) == 2 else (first_day, last_day)
    if st.button("Reload data"):
        st.cache_resource.clear()
        st.cache_data.clear()
        st.rerun()

st.caption(f"{len(markets):,} settled windows with minute-by-minute odds, {day_label(first_day)} to "
           f"{day_label(last_day)}. To add the newest, run **1_download_data.bat** and press Reload data.")

t0 = datetime.combine(d0, time.min, ET).timestamp()
t1 = datetime.combine(d1 + timedelta(days=1), time.min, ET).timestamp()
quotes = quotes_for(side, spread_c, fee_rate, t0, t1)
lo, hi = zone[0] / 100, zone[1] / 100
zone_quotes = bt.in_zone(quotes, lo, hi)
trades = bt.first_trades(zone_quotes, entry)
stats = bt.summarize(trades)

tab_test, tab_model, tab_sim, tab_replay = st.tabs(
    ["Does 60¢ win 60%?", "Model vs market", "$100 bankroll simulator", "Replay a window"])

with tab_test:
    if not stats:
        st.warning("No trades match these settings. Widen the price zone or the minutes.")
    else:
        st.markdown(f"Buying the side priced **{zone[0]} to {zone[1]}¢**, at most once per window (the first "
                    f"minute it qualifies): **{stats['n']:,} trades**, about {stats['per_day']:.0f} a day.")
        stat_tiles(stats)
        verdict(stats, trades)

        st.subheader("Edge by price paid")
        st.caption("Each bar is your same bet with a 5¢ zone at that price: the first minute a side cost that "
                   "much, one trade per window. The height is the win rate minus the cost (price + fee), in cents "
                   "per $1 contract, so above 0 made money. Whiskers are the 95% range: if one crosses 0, that "
                   "price can't be told apart from break-even.")
        buckets = price_buckets(side, spread_c, fee_rate, t0, t1, entry)
        fig = edge_chart(buckets, buckets.bucket + 2.5, "Price paid (¢)", 2,
                         "Paid %{customdata[1]:.1f}¢ on average<br>Won %{customdata[0]:.1f}% of the time<br>"
                         "Edge %{y:+.2f}¢ ± %{customdata[3]:.2f}¢<br>%{customdata[2]:,} windows<extra></extra>",
                         zone=zone)
        fig.update_xaxes(range=[0, 100], ticksuffix="¢", dtick=10)
        show(fig)
        with st.expander("Show as a table"):
            st.dataframe(pd.DataFrame({
                "Price paid": [f"{b:.0f} to {b + 5:.0f}¢" for b in buckets.bucket],
                "Average paid": [f"{p * 100:.1f}¢" for p in buckets.price],
                "Won": [pct(w) for w in buckets.win],
                "Edge per contract": [cents(e) for e in buckets.edge],
                "± (95%)": [f"{h * 100:.2f}¢" for h in buckets.half],
                "Windows": buckets.windows,
            }), hide_index=True)

        st.subheader("Edge by minute, inside your zone")
        st.caption("The edge if you only bought in that one minute, whenever a side was in your zone then. "
                   "Minute 1 has 14 minutes left; minute 14 has 1.")
        minutes = bt.edge_by_minute(zone_quotes, entry)
        fig = edge_chart(minutes, minutes.minute, "Minute of the window you bought in", 0.3,
                         "Minute %{x}<br>Paid %{customdata[1]:.1f}¢, won %{customdata[0]:.1f}%<br>"
                         "Edge %{y:+.2f}¢ ± %{customdata[3]:.2f}¢<br>%{customdata[2]:,} windows<extra></extra>")
        fig.update_xaxes(dtick=1)
        show(fig)
        with st.expander("Show as a table"):
            st.dataframe(pd.DataFrame({
                "Minute": minutes.minute,
                "Average paid": [f"{p * 100:.1f}¢" for p in minutes.price],
                "Won": [pct(w) for w in minutes.win],
                "Edge per contract": [cents(e) for e in minutes.edge],
                "± (95%)": [f"{h * 100:.2f}¢" for h in minutes.half],
                "Windows": minutes.windows,
            }), hide_index=True)

with tab_model:
    st.caption("At every minute of every window, the model takes BTC's price on Coinbase, how far it is from the "
               "window's start, the time left and how jumpy BTC has been lately, and works out the chance BTC "
               "finishes up. Then it buys whichever side Kalshi was charging too little for. The replay model is "
               "your btc_mc.py method and prices each month using only the history before it.")
    s_all, agree = model_data()
    a, b, c = st.columns([2, 2, 1])
    model = {"Replay (btc_mc.py)": "replay", "Bell curve": "normal"}[
        a.radio("Model", ["Replay (btc_mc.py)", "Bell curve"], horizontal=True)]
    min_edge = b.slider("Buy when the model expects at least (¢ per contract, after the fee)", 1, 15, 3)
    delay = c.checkbox("Buy a minute later", help="Decide now but pay the next minute's price. An edge that "
                                                  "disappears with a one-minute delay only exists for the fastest "
                                                  "traders.")
    acc = fv.accuracy(s_all[(s_all.close_ts > t0) & (s_all.close_ts <= t1)], model, entry)
    miss = ("Brier score: the average of (probability - result)², where the result is 1 if BTC finished up and "
            "0 if not. Lower means better predictions.")
    m1, m2, m3 = st.columns(3)
    m1.metric("Kalshi's price: average miss", f"{acc['market']:.4f}", help=miss)
    m2.metric("Model: average miss", f"{acc['model']:.4f}", delta=f"{acc['model'] - acc['market']:+.4f} vs Kalshi",
              delta_color="inverse", help=miss)
    m3.metric("Coinbase matches Kalshi's result", pct(agree),
              help="How often Coinbase's own minute averages give the same up/down result as Kalshi's official "
                   "number. The misses are near-ties.")

    cands = model_candidates(model, int(delay), entry, spread_c, fee_rate, t0, t1)
    model_trades = fv.model_trades(cands, min_edge / 100)
    model_stats = bt.summarize(model_trades)
    if not model_stats:
        st.warning("The model found no trades with these settings.")
    else:
        st.markdown(f"Buying the first minute in each window that the model expected at least **{min_edge}¢** "
                    f"per contract: **{model_stats['n']:,} trades**. On average the model expected "
                    f"**{model_trades.pred_edge.mean() * 100:+.1f}¢** per contract.")
        stat_tiles(model_stats)
        verdict(model_stats, model_trades)

        st.subheader("Did bigger expected edges pay more?")
        st.caption("Each bar takes the first minute per window that the model expected an edge in that range and "
                   "shows what those trades actually made. If the model knew something Kalshi didn't, the bars "
                   "would climb to the right.")
        bands = fv.by_predicted_edge(cands)
        names = {lo: (f"{lo}–{hi}¢" if hi < 100 else f"{lo}¢+") for lo, hi in zip(fv.BANDS, fv.BANDS[1:])}
        fig = edge_chart(bands, [names[int(b)] for b in bands.band], "What the model expected (¢ per contract)",
                         bar_width(len(bands)), "Model expected %{x}<br>Paid %{customdata[1]:.1f}¢, won %{customdata[0]:.1f}%<br>"
                               "Actually made %{y:+.2f}¢ ± %{customdata[3]:.2f}¢<br>%{customdata[2]:,} windows"
                               "<extra></extra>")
        fig.update_xaxes(type="category")
        show(fig)
        with st.expander("Show as a table"):
            st.dataframe(pd.DataFrame({
                "Model expected": [names[int(b)] for b in bands.band],
                "Expected on average": [cents(p) for p in bands.pred],
                "Actually made": [cents(e) for e in bands.edge],
                "± (95%)": [f"{h * 100:.2f}¢" for h in bands.half],
                "Windows": bands.windows,
            }), hide_index=True)

        st.subheader("Month by month")
        months = fv.by_month(model_trades)
        fig = edge_chart(months, months.month, None, bar_width(len(months)),
                         "%{x}<br>Paid %{customdata[1]:.1f}¢, won %{customdata[0]:.1f}%<br>"
                         "Edge %{y:+.2f}¢ ± %{customdata[3]:.2f}¢<br>%{customdata[2]:,} trades<extra></extra>")
        fig.update_xaxes(type="category")
        show(fig)
        with st.expander("Show as a table"):
            st.dataframe(pd.DataFrame({
                "Month": months.month,
                "Won": [pct(w) for w in months.win],
                "Edge per contract": [cents(e) for e in months.edge],
                "± (95%)": [f"{h * 100:.2f}¢" for h in months.half],
                "Trades": months.windows,
            }), hide_index=True)

with tab_sim:
    use_model = st.radio("Which bet", ["Price zone (first tab)", "Model (second tab)"], horizontal=True) != \
        "Price zone (first tab)"
    sim_trades, sim_stats = (model_trades, model_stats) if use_model else (trades, stats)
    if not sim_stats:
        st.info("That bet has no trades with the current settings, so there's nothing to simulate.")
    else:
        st.markdown(f"Replays the **{sim_stats['n']:,} real trades** from that tab in random order, about "
                    f"{sim_stats['per_day']:.0f} a day, across 2,000 simulated futures. Whole contracts only, fees "
                    "rounded up per order like on Kalshi.")
        a, b, c, d = st.columns(4)
        start = a.number_input("Starting bankroll ($)", 10, 10000, 100, 10)
        days = b.slider("Days", 7, 90, 30)
        bet_pct = c.slider("Your rule: % of bankroll per trade", 1, 50, 20)
        flat = d.number_input("Flat bet ($)", 1, 1000, 5)

        k = bt.kelly_fraction(sim_stats)
        if use_model:
            st.markdown("¼ Kelly sizes each trade from the **model's own** chance of winning. If the model is "
                        "overconfident, so are the bets.")
        elif k > 0:
            st.markdown(f"At this edge, full Kelly says spend **{k:.1%}** of the bankroll per trade; "
                        f"a quarter of that is **{k / 4:.1%}**.")
        else:
            st.markdown("Kelly says **don't bet**: after fees there's no edge, so the ¼ Kelly row never trades "
                        "and stays at your starting bankroll.")

        rules = {f"{bet_pct}% of bankroll (your rule)": "pct", "¼ Kelly": "kelly", f"Flat ${flat}": "flat"}
        results = {name: simulate(sim_trades, rule, days, float(start), bet_pct / 100, float(flat), fee_rate)
                   for name, rule in rules.items()}
        rows = []
        for name, paths in results.items():
            s = bt.sim_stats(paths, start)
            rows.append({"Rule": name, "Middle result": f"${s['median']:,.0f}",
                         "Bad case (1 in 10 worse)": f"${s['p10']:,.0f}",
                         "Good case (1 in 10 better)": f"${s['p90']:,.0f}",
                         "Chance you're up": pct(s["above_start"]),
                         "Chance you lose half": pct(s["below_half"]),
                         "Chance you lose 95%+": pct(s["wiped"])})
        st.dataframe(pd.DataFrame(rows), hide_index=True)

        pick = st.radio("Chart", list(rules), horizontal=True)
        st.caption("Line: the middle run. Dark band: the middle half of runs. Light band: 8 in 10 runs.")
        show(fan_chart(results[pick], start))

with tab_replay:
    traded = trades.set_index("ticker") if stats else pd.DataFrame()
    left, right = st.columns([1, 2])
    day = left.date_input("Day", last_day, first_day, last_day, key="replay_day")
    only_traded = left.checkbox("Only windows the bet traded")
    todays = markets[(open_et.date == day)]
    if only_traded:
        todays = todays[todays.ticker.isin(traded.index)]
    if todays.empty:
        st.info("No windows to show for that day.")
    else:
        labels = {}
        for row in todays.itertuples():
            tag = " · traded" if row.ticker in traded.index else ""
            labels[row.ticker] = (f"{clock(et(row.open_ts))} to {clock(et(row.close_ts))} · "
                                  f"{'UP' if row.yes_won else 'DOWN'}{tag}")
        ticker = right.selectbox("Window", todays.ticker.iloc[::-1], format_func=labels.get)
        m = markets.set_index("ticker").loc[ticker]
        trade = traded.loc[ticker] if ticker in traded.index else None

        moved = "UP" if m.yes_won else "DOWN"
        line = f"**BTC went {moved}.**"
        if pd.notna(m.final) and pd.notna(m.target):
            line += f" Final ${m.final:,.2f} vs target ${m.target:,.2f}."
        if trade is not None:
            pnl = trade.pnl * 100
            line += (f" The bet bought **{trade.side} at {trade.price * 100:.1f}¢** in minute {trade.minute} and "
                     f"{'won' if trade.won else 'lost'} ({pnl:+.1f}¢ per contract after the fee).")
        else:
            line += " The bet didn't trade this window."
        st.markdown(line)
        st.caption("Bottom chart: the line is the middle of the YES bid and ask, the darker wash is the bid-ask "
                   "spread, and the light bands are where a side was priced in your zone (YES on top, NO below).")
        show(replay_chart(m, bt.window_candles(ticker), bt.btc_between(m.open_ts - 120, m.close_ts),
                          trade, zone, side))
