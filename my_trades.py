"""My Kalshi trades: balance, open positions, fills and settled results.

Start it with 5_my_trades.bat. It only listens on this PC (localhost), reads the
API key from %LOCALAPPDATA%\\kalshi-btc15\\secrets (never from this folder), and
only calls read-only portfolio endpoints: it cannot place or cancel orders.
"""
import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from config import HOME, KALSHI_API

ET = ZoneInfo("America/New_York")
SECRETS = HOME / "secrets"
KEY_ID_FILE = SECRETS / "key_id.txt"
PEM_FILE = SECRETS / "private_key.pem"
API_PATH = urllib.parse.urlparse(KALSHI_API).path  # /trade-api/v2, part of what gets signed

st.set_page_config(page_title="My Kalshi trades", page_icon="💼", layout="wide")


# ---------- signed requests ----------

def load_credentials():
    if not KEY_ID_FILE.exists() or not PEM_FILE.exists():
        return None
    key_id = KEY_ID_FILE.read_text().strip()
    key = serialization.load_pem_private_key(PEM_FILE.read_bytes(), password=None)
    return key_id, key


def get(creds, path, params=None):
    """GET a portfolio endpoint, signed the way Kalshi requires (RSA-PSS over time + method + path)."""
    key_id, key = creds
    ts = str(int(time.time() * 1000))
    msg = (ts + "GET" + API_PATH + path).encode()
    if isinstance(key, rsa.RSAPrivateKey):
        sig = key.sign(msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
                       hashes.SHA256())
    else:  # Ed25519 keys sign the message directly
        sig = key.sign(msg)
    url = KALSHI_API + path + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, headers={
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def get_all(creds, path, field, params=None, max_pages=50):
    """Follow Kalshi's cursor until the list ends."""
    rows, params = [], dict(params or {}, limit=200)
    for _ in range(max_pages):
        page = get(creds, path, params)
        rows += page.get(field) or []
        if not page.get("cursor"):
            break
        params["cursor"] = page["cursor"]
    return rows


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
    return balance, positions, fills, settlements


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
    balance, positions, fills, settlements = load_portfolio()
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

tab_pos, tab_fills, tab_set = st.tabs(["Open positions", "Fills", "Settled"])

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

key_id = load_credentials()[0]
st.caption(f"Read-only. Key ending …{key_id[-4:]} · only reachable from this PC · data cached 60 s.")
