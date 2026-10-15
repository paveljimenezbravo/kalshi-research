"""My Kalshi trades: balance, open positions, fills and settled results.

Start it with 5_my_trades.bat. It only listens on this PC (localhost), reads the
API key from %LOCALAPPDATA%\\kalshi-btc15\\secrets (never from this folder), and
only calls read-only portfolio endpoints: it cannot place or cancel orders.
"""
import urllib.error
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from kalshi_auth import KEY_ID_FILE, PEM_FILE, get, get_all, load_credentials

ET = ZoneInfo("America/New_York")

st.set_page_config(page_title="My Kalshi trades", page_icon="💼", layout="wide")


# ---------- field helpers (Kalshi returns cents, or newer *_dollars / *_fp strings) ----------

def dollars(d, name):
    if d.get(name + "_dollars") is not None:
        return float(d[name + "_dollars"])
    v = d.get(name) or 0
    return float(v) if isinstance(v, str) else v / 100  # strings like fee_cost are already dollars


def count(d, name):
    v = d.get(name + "_fp", d.get(name))
    return float(v or 0)


def when(s):
    return pd.to_datetime(s, utc=True).tz_convert(ET).tz_localize(None) if s else pd.NaT


@st.cache_data(ttl=60, show_spinner="Loading from Kalshi...")
def load_portfolio():
    creds = load_credentials()
    balance = get(creds, "/portfolio/balance")
    positions = get_all(creds, "/portfolio/positions", "market_positions", {"count_filter": "position"})
    fills = get_all(creds, "/portfolio/fills", "fills")
    settlements = get_all(creds, "/portfolio/settlements", "settlements")
    deposits = get_all(creds, "/portfolio/deposits", "deposits")
    withdrawals = get_all(creds, "/portfolio/withdrawals", "withdrawals")
    return balance, positions, fills, settlements, deposits, withdrawals


# ---------- page ----------

st.title("My Kalshi trades")

if load_credentials() is None:
    st.warning("No API key found on this PC yet.")
    st.markdown(f"""
1. On kalshi.com go to **Account & security → API keys → Create key**. Kalshi shows the
   private key **once** and downloads it as a `.pem` file. (Your old key's private half
   was never saved, so make a new one and delete the old key there.)
2. Save the file as **`{PEM_FILE}`**.
3. Put the new key ID (and nothing else) in **`{KEY_ID_FILE}`**.
4. Refresh this page.

Both files stay outside the project folder, so they can never be committed to GitHub.
Never paste the private key into chat.
""")
    st.stop()

if st.button("Refresh"):
    load_portfolio.clear()

try:
    balance, positions, fills, settlements, deposits, withdrawals = load_portfolio()
except urllib.error.HTTPError as e:
    st.error(f"Kalshi refused the request ({e.code}). "
             + ("Check that the key ID and .pem file belong to the same key." if e.code == 401 else e.reason))
    st.stop()
except urllib.error.URLError as e:
    st.error(f"Couldn't reach Kalshi: {e.reason}")
    st.stop()

# Summary. Every fill adds contracts of its `side` at that side's price, even one labelled
# "sell": selling YES you hold is booked as buying NO, and each YES+NO pair pays $1.
fill_df = pd.DataFrame([{
    "Time (ET)": when(f.get("created_time")),
    "Market": f.get("ticker") or f.get("market_ticker"),
    "Order": f"{f.get('action', '')} {f.get('side', '')}".title().replace("Yes", "YES").replace("No", "NO"),
    "Contracts": count(f, "count"),
    "Price": dollars(f, "yes_price" if f.get("side") == "yes" else "no_price"),
    "Fee": dollars(f, "fee_cost"),
    "Taker": f.get("is_taker"),
} for f in fills])
if not fill_df.empty:
    fill_df["Cost"] = fill_df["Contracts"] * fill_df["Price"]
    fill_df = fill_df.sort_values("Time (ET)", ascending=False)

set_df = pd.DataFrame([{
    "Settled (ET)": when(s.get("settled_time")),
    "Market": s.get("ticker"),
    "Result": (s.get("market_result") or "").upper(),
    "YES bought": count(s, "yes_count"),
    "NO bought": count(s, "no_count"),
    "Cost": dollars(s, "yes_total_cost") + dollars(s, "no_total_cost"),
    "Paid back": dollars(s, "revenue") + min(count(s, "yes_count"), count(s, "no_count")),
    "Fees": dollars(s, "fee_cost"),
} for s in settlements])
if not set_df.empty:
    set_df["Profit"] = set_df["Paid back"] - set_df["Cost"] - set_df["Fees"]
    set_df = set_df.sort_values("Settled (ET)", ascending=False)

c1, c2, c3, c4 = st.columns(4)
c1.metric("Cash", f"${dollars(balance, 'balance'):,.2f}")
c2.metric("Open positions value", f"${dollars(balance, 'portfolio_value'):,.2f}")
c3.metric("Settled profit after fees", ('-' if set_df['Profit'].sum() < 0 else '+') + f"${abs(set_df['Profit'].sum()):,.2f}" if not set_df.empty else "—",
          help="Payout plus $1 per YES+NO pair, minus what you paid and fees, over all settled markets.")
c4.metric("Fees paid", f"${set_df['Fees'].sum():,.2f}" if not set_df.empty else "—")

tab_money, tab_pos, tab_fills, tab_set = st.tabs(["Money in", "Open positions", "Fills", "Settled"])

with tab_pos:
    pos_df = pd.DataFrame([{
        "Market": p.get("ticker"),
        "Position": count(p, "position"),
        "Exposure": dollars(p, "market_exposure"),
        "Realized P&L": dollars(p, "realized_pnl"),
        "Fees paid": dollars(p, "fees_paid"),
    } for p in positions])
    if pos_df.empty:
        st.info("No open positions.")
    else:
        st.caption("Position > 0 is YES contracts, < 0 is NO contracts.")
        st.dataframe(pos_df, hide_index=True, use_container_width=True,
                     column_config={c: st.column_config.NumberColumn(format="$%.2f")
                                    for c in ("Exposure", "Realized P&L", "Fees paid")})

with tab_fills:
    if fill_df.empty:
        st.info("No fills yet.")
    else:
        markets = st.multiselect("Markets", sorted(fill_df["Market"].unique()))
        view = fill_df[fill_df["Market"].isin(markets)] if markets else fill_df
        st.dataframe(view, hide_index=True, use_container_width=True,
                     column_config={"Price": st.column_config.NumberColumn(format="$%.2f"),
                                    "Fee": st.column_config.NumberColumn(format="$%.2f"),
                                    "Cost": st.column_config.NumberColumn(format="$%.2f")})

with tab_set:
    if set_df.empty:
        st.info("No settled markets yet.")
    else:
        curve = set_df.sort_values("Settled (ET)").set_index("Settled (ET)")["Profit"].cumsum()
        st.line_chart(curve, y_label="Cumulative profit ($)")
        st.dataframe(set_df, hide_index=True, use_container_width=True,
                     column_config={c: st.column_config.NumberColumn(format="$%.2f")
                                    for c in ("Cost", "Paid back", "Fees", "Profit")})

with tab_money:
    # One row per cash event: deposits/withdrawals change what you put in; settled
    # markets change what it's worth. Card deposit fees come off the top.
    applied = lambda rows: [r for r in rows if r.get("status", "applied") == "applied"]
    events = pd.DataFrame(
        [{"t": pd.Timestamp(d["created_ts"], unit="s", tz="UTC"), "put_in": d["amount_cents"] / 100,
          "value": (d["amount_cents"] - d.get("fee_cents", 0)) / 100, "dep_fee": d.get("fee_cents", 0) / 100}
         for d in applied(deposits)]
        + [{"t": pd.Timestamp(w["created_ts"], unit="s", tz="UTC"), "put_in": -w["amount_cents"] / 100,
            "value": -w["amount_cents"] / 100, "dep_fee": 0.0} for w in applied(withdrawals)]
        + [{"t": t.tz_localize(ET).tz_convert("UTC"), "put_in": 0.0, "value": p, "dep_fee": 0.0}
           for t, p in zip(set_df["Settled (ET)"], set_df["Profit"]) if not pd.isna(t)])
    if events.empty:
        st.info("No deposits yet.")
    else:
        events = events.sort_values("t")
        events["t"] = events["t"].dt.tz_convert(ET).dt.tz_localize(None)
        events[["Put in", "Account value"]] = events[["put_in", "value"]].cumsum().values
        now_value = dollars(balance, "balance") + dollars(balance, "portfolio_value")
        line = pd.concat([events, pd.DataFrame([{"t": pd.Timestamp.now(ET).tz_localize(None),
                                                 "Put in": events["Put in"].iloc[-1], "Account value": now_value}])])
        put_in = events["put_in"].sum()
        m1, m2, m3 = st.columns(3)
        m1.metric("Total put in", f"${put_in:,.2f}")
        m2.metric("Worth now", f"${now_value:,.2f}")
        m3.metric("Card deposit fees", f"${events['dep_fee'].sum():,.2f}",
                  help="Kalshi charges about 2% on debit-card deposits; bank transfers are usually free.")
        fig = go.Figure()
        fig.add_scatter(x=line["t"], y=line["Put in"], name="Put in", line=dict(shape="hv", color="#898781", dash="dot"))
        fig.add_scatter(x=line["t"], y=line["Account value"], name="Account value", line=dict(shape="hv", color="#2a78d6"))
        fig.update_layout(height=380, margin=dict(l=10, r=10, t=10, b=10), yaxis_tickprefix="$",
                          legend=dict(orientation="h", y=1.08), hovermode="x unified")
        st.plotly_chart(fig, use_container_width=True)
        gap = now_value - events["Account value"].iloc[-1]
        if abs(gap) > 1:
            st.caption(f"{'-' if gap < 0 else '+'}${abs(gap):,.2f} of today's value isn't explained by deposits and settled markets "
                       "(open positions, or settlements Kalshi has archived).")
        st.dataframe(events[["t", "put_in", "dep_fee"]][events["put_in"] != 0].sort_values("t", ascending=False)
                     .rename(columns={"t": "Time (ET)", "put_in": "Amount", "dep_fee": "Fee"}),
                     hide_index=True, use_container_width=True,
                     column_config={c: st.column_config.NumberColumn(format="$%.2f") for c in ("Amount", "Fee")})

key_id = load_credentials()[0]
st.caption(f"Read-only. Key ending …{key_id[-4:]} · only reachable from this PC · data cached 60 s.")
