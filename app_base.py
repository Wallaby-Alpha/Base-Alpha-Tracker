"""
Base Chain Wallet Intelligence
================================
Three-tab Streamlit app — deploy free on Streamlit Community Cloud.

Tab 1 — Cohort Analyzer:     classify holders by total wallet net worth
Tab 2 — Whale Overlap:       find what tokens the big wallets currently share
Tab 3 — Recent Acquisitions: what have whales/sharks/dolphins actually bought in last N days

Requirements:
    pip install streamlit requests pandas python-dateutil

Moralis API:
    - Free tier: 40,000 requests/day
    - Sign up at https://moralis.io → Web3 APIs → get your API key
    - Endpoints used:
        GET /wallets/{address}/tokens          → balances + USD prices
        GET /{address}/erc20/transfers         → inbound token transfers
        GET /erc20/metadata                    → token name/symbol/decimals

To add paid access gating later:
  1. In Streamlit Cloud dashboard → Secrets, add:
        ACCESS_CODES = ["code1", "code2", "code3"]
  2. Uncomment the GATING BLOCK below.
"""

import io
import time
import requests
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
from collections import defaultdict
from datetime import datetime, timezone, timedelta

# ── page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Base Wallet Intel",
    page_icon="🔵",
    layout="centered",
)

st.markdown("""
<style>
    .stProgress > div > div { background-color: #0052ff; }
    code { font-size: 0.78rem; }
</style>
""", unsafe_allow_html=True)

# ── constants ─────────────────────────────────────────────────────────────────
COHORT_BRACKETS = [
    {"name": "Whale 🐋",   "min_usd": 100_000, "max_usd": float("inf")},
    {"name": "Shark 🦈",   "min_usd": 25_000,  "max_usd": 100_000},
    {"name": "Dolphin 🐬", "min_usd": 5_000,   "max_usd": 25_000},
    {"name": "Fish 🐟",    "min_usd": 500,     "max_usd": 5_000},
    {"name": "Minnow 🦐",  "min_usd": 0,       "max_usd": 500},
]

# Stablecoins + WETH on Base — filtered out of overlap/recent buys
SKIP_TOKENS = {
    "0x4200000000000000000000000000000000000006",  # WETH (Base)
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # USDC (Base)
    "0x50c5725949a6f0c72e6c4a641f24049a917db0cb",  # DAI (Base)
    "0xfde4c96c8593536e31f229ea8f37b2ada2699bb2",  # USDT (Base)
    "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca",  # USDbC (Base bridged USDC)
}

MORALIS_BASE_URL = "https://deep-index.moralis.io/api/v2.2"
CHAIN = "base"
MAX_WALLETS = 150


# ══════════════════════════════════════════════════════════════════════════════
# GATING BLOCK — uncomment when you want to sell access
# ══════════════════════════════════════════════════════════════════════════════
# def check_access():
#     valid_codes = st.secrets.get("ACCESS_CODES", [])
#     code = st.text_input("Enter access code", type="password", key="access_code")
#     if not code:
#         st.info("Enter your access code to continue. Purchase at [your-site.com](https://your-site.com).")
#         st.stop()
#     if code not in valid_codes:
#         st.error("Invalid access code.")
#         st.stop()
# check_access()
# ══════════════════════════════════════════════════════════════════════════════


# ── shared API helpers ────────────────────────────────────────────────────────
def moralis_get(path: str, api_key: str, params: dict = None) -> dict:
    """GET request to Moralis API. Returns parsed JSON or {}."""
    headers = {"X-API-Key": api_key, "accept": "application/json"}
    try:
        r = requests.get(
            f"{MORALIS_BASE_URL}{path}",
            headers=headers,
            params=params or {},
            timeout=30,
        )
        r.raise_for_status()
        return r.json()
    except Exception:
        return {}


def get_wallet_tokens(wallet: str, api_key: str) -> list:
    """
    Returns list of ERC-20 token balances for a wallet on Base.
    Each item includes token_address, symbol, name, balance, decimals,
    and usd_price / usd_value when available.
    """
    data = moralis_get(
        f"/wallets/{wallet}/tokens",
        api_key,
        params={"chain": CHAIN, "exclude_spam": "true", "exclude_unverified_contracts": "false"},
    )
    return data.get("result", [])


def wallet_usd_value(tokens: list) -> float:
    """Sum USD value across all tokens in a wallet."""
    total = 0.0
    for t in tokens:
        # Moralis returns usd_value as a string or None
        val = t.get("usd_value")
        if val is not None:
            try:
                total += float(val)
            except (ValueError, TypeError):
                pass
    return total


def assign_cohort(usd: float) -> str:
    for b in COHORT_BRACKETS:
        if b["min_usd"] <= usd < b["max_usd"]:
            return b["name"]
    return "Minnow 🦐"


def normalise_address(addr: str) -> str:
    """Lowercase EVM addresses for consistent comparison."""
    return addr.lower().strip() if addr else ""


def detect_address_col(df: pd.DataFrame):
    """Find the column that looks like EVM wallet addresses."""
    for col in df.columns:
        if df[col].astype(str).str.match(r"^0x[0-9a-fA-F]{40}$").any():
            return col
    return None


def parse_wallets_from_csv(uploaded) -> list:
    df = pd.read_csv(io.BytesIO(uploaded.read()))
    col = detect_address_col(df)
    if not col:
        return []
    return (
        df[col].dropna().astype(str)
        .str.strip().str.lower()
        .unique().tolist()
    )


# ── acquisition helpers (Tab 3) ───────────────────────────────────────────────
def get_erc20_transfers_in(wallet: str, api_key: str, from_date: str) -> list:
    """
    Fetch all inbound ERC-20 transfers to `wallet` on Base since `from_date` (ISO string).
    Moralis paginates with a cursor; we follow until exhausted or 500 transfers collected.
    """
    all_transfers = []
    cursor = None
    while True:
        params = {
            "chain":     CHAIN,
            "from_date": from_date,
            "limit":     100,
            "order":     "DESC",
        }
        if cursor:
            params["cursor"] = cursor

        data = moralis_get(f"/{wallet}/erc20/transfers", api_key, params=params)

        # DEBUG: uncomment to inspect raw API response
        #st.write(data)

        results = data.get("result", [])
        if not results:
            break

        for tx in results:
            # Only inbound transfers (to_address == wallet)
            if normalise_address(tx.get("to_address", "")) == wallet:
                all_transfers.append(tx)

        cursor = data.get("cursor")
        if not cursor or len(all_transfers) >= 500:
            break
        time.sleep(0.1)

    return all_transfers


def parse_inflow_events(transfers: list, wallet: str) -> list:
    """Convert raw Moralis transfer records into normalised inflow dicts."""
    inflows = []
    for tx in transfers:
        # Moralis returns the token contract under "token_address"
        token_addr = normalise_address(tx.get("token_address", ""))
        if not token_addr or token_addr in SKIP_TOKENS:
            continue

        decimals = int(tx.get("token_decimals", 18) or 18)
        try:
            raw_val = int(tx.get("value", 0) or 0)
            amount  = raw_val / (10 ** decimals)
        except (ValueError, TypeError):
            amount = 0.0

        if amount <= 0:
            continue

        block_ts = tx.get("block_timestamp", "")
        try:
            dt = datetime.fromisoformat(block_ts.replace("Z", "+00:00"))
            date_str = dt.strftime("%Y-%m-%d %H:%M UTC")
            ts = int(dt.timestamp())
        except Exception:
            date_str = block_ts
            ts = 0

        inflows.append({
            "mint":            token_addr,   # using "mint" key to match Solana app convention
            "symbol":          tx.get("token_symbol", token_addr[:8]),
            "name":            tx.get("token_name", "Unknown"),
            "amount_received": round(amount, 6),
            "timestamp":       ts,
            "date":            date_str,
            "tx_sig":          tx.get("transaction_hash", ""),
            "wallet":          wallet,
        })
    return inflows


# ── global sidebar: API key + remember me ────────────────────────────────────
with st.sidebar:
    st.title("🔵 Base Wallet Intel")
    st.markdown("---")

    # localStorage bridge — saves key client-side only
    components.html("""
<script>
(function() {
    const saved = localStorage.getItem('moralis_api_key');
    if (saved) {
        window.parent.postMessage({type: 'moralis_key', key: saved}, '*');
    }
})();
window.addEventListener('message', function(e) {
    if (e.data && e.data.type === 'save_moralis_key')
        localStorage.setItem('moralis_api_key', e.data.key);
    if (e.data && e.data.type === 'clear_moralis_key')
        localStorage.removeItem('moralis_api_key');
});
</script>
""", height=0)

    if "moralis_key_value" not in st.session_state:
        st.session_state["moralis_key_value"] = ""

    moralis_key = st.text_input(
        "Moralis API Key",
        type="password",
        placeholder="Paste key — stays in your browser",
        value=st.session_state["moralis_key_value"],
        key="moralis_key_input",
    )
    remember = st.checkbox("Remember in this browser", value=True)

    if moralis_key:
        st.session_state["moralis_key_value"] = moralis_key
        if remember:
            components.html(f"""
<script>
window.parent.postMessage({{type: 'save_moralis_key', key: '{moralis_key}'}}, '*');
</script>
""", height=0)
        else:
            components.html("""
<script>
window.parent.postMessage({type: 'clear_moralis_key'}, '*');
</script>
""", height=0)

    if not moralis_key:
        st.info("💡 Saved key auto-fills after page load.")

    st.markdown("---")
    st.caption("Get a free key at [moralis.io](https://moralis.io)")
    st.markdown("---")
    st.markdown("**How to get a holder CSV:**")
    st.caption(
        "Base doesn't offer free holder exports. Options:\n"
        "- [Dune Analytics](https://dune.com) — query `tokens_base.transfers` for a token contract\n"
        "- [Bubblemaps](https://bubblemaps.io) — export holder list from token page\n"
        "- [Basescan](https://basescan.org) token holders page (manual copy)\n\n"
        "CSV just needs one column of `0x...` addresses."
    )


# ── tabs ──────────────────────────────────────────────────────────────────────
tab1, tab2, tab3 = st.tabs(["🐋 Cohort Analyzer", "🔍 Whale Overlap", "📅 Recent Buys"])


# ══════════════════════════════════════════════════════════════════════════════
#  TAB 1 — COHORT ANALYZER
# ══════════════════════════════════════════════════════════════════════════════
with tab1:
    st.header("Cohort Analyzer")
    st.caption("Classify Base token holders by total wallet net worth.")

    with st.expander("ℹ️ How to use", expanded=False):
        st.markdown("""
1. **Prepare a CSV of Base wallet addresses.** Any CSV with a column of `0x...` addresses works — the app detects the address column automatically. Ways to get one:
   - **Dune Analytics:** query `tokens_base.transfers` or `erc20_base.evt_Transfer` for a token contract and export holder addresses
   - **Bubblemaps:** go to a token page → export holder list
   - **Basescan:** token holders page (manual copy/paste into spreadsheet, save as CSV)
   - **Your own list:** one address per row is fine
2. Upload the CSV below and hit **Run Cohort Analysis**
3. Results bucket each wallet into Whale / Shark / Dolphin / Fish / Minnow tiers by total portfolio value
4. Whales, Sharks & Dolphins are automatically passed to the **Whale Overlap** and **Recent Buys** tabs
""")

    c1_file = st.file_uploader("Upload holder CSV", type=["csv"], key="c1_file")
    c1_max  = st.slider("Max wallets", 10, MAX_WALLETS, 50, 10, key="c1_max")
    c1_btn  = st.button(
        "🚀 Run Cohort Analysis", type="primary",
        disabled=not (moralis_key and c1_file), key="c1_btn",
    )

    if c1_btn:
        wallets = parse_wallets_from_csv(c1_file)
        if not wallets:
            st.error("No valid Base (0x...) addresses found in CSV.")
            st.stop()
        if len(wallets) > c1_max:
            st.info(f"CSV has {len(wallets)} addresses — analyzing top {c1_max}.")
            wallets = wallets[:c1_max]

        st.markdown("---")
        prog   = st.progress(0)
        status = st.empty()

        cohort_buckets = defaultdict(list)
        rows = []

        for i, wallet in enumerate(wallets):
            status.text(f"[{i+1}/{len(wallets)}] {wallet[:12]}...")
            tokens    = get_wallet_tokens(wallet, moralis_key)
            net_worth = wallet_usd_value(tokens)
            label     = assign_cohort(net_worth)
            cohort_buckets[label].append({"wallet": wallet, "net_worth": net_worth})
            rows.append({"wallet": wallet, "net_worth_usd": round(net_worth, 2), "cohort": label})
            prog.progress((i + 1) / len(wallets))
            time.sleep(0.15)

        status.empty()
        prog.empty()

        # Save whales+sharks+dolphins for other tabs
        big_wallets = [
            r["wallet"] for r in rows
            if r["cohort"] in ("Whale 🐋", "Shark 🦈", "Dolphin 🐬")
        ]
        st.session_state["whale_wallets"] = big_wallets
        if big_wallets:
            st.success(f"✅ {len(big_wallets)} Whale/Shark/Dolphin wallets saved — available in Whale Overlap and Recent Buys tabs.")

        # Distribution metrics
        st.markdown("---")
        st.subheader("📊 Distribution")
        total = len(wallets)
        cols  = st.columns(len(COHORT_BRACKETS))
        for col, bracket in zip(cols, COHORT_BRACKETS):
            count = len(cohort_buckets[bracket["name"]])
            col.metric(bracket["name"], count, f"{count/total*100:.1f}%")

        # Per-cohort tables
        st.markdown("---")
        st.subheader("🏷️ Holders by Cohort")
        for bracket in COHORT_BRACKETS:
            members = cohort_buckets[bracket["name"]]
            if not members:
                continue
            with st.expander(f"{bracket['name']}  ·  {len(members)} holders"):
                df_out = pd.DataFrame([
                    {"Wallet": m["wallet"], "Net Worth (USD)": f"${m['net_worth']:,.2f}"}
                    for m in sorted(members, key=lambda x: -x["net_worth"])
                ])
                st.dataframe(df_out, use_container_width=True, hide_index=True)

        # Download
        st.markdown("---")
        csv_bytes = (
            pd.DataFrame(rows)
            .sort_values("net_worth_usd", ascending=False)
            .to_csv(index=False).encode()
        )
        st.download_button("⬇️ Download results CSV", csv_bytes, "holder_cohorts_base.csv", "text/csv")


# ══════════════════════════════════════════════════════════════════════════════
#  TAB 2 — WHALE OVERLAP
# ══════════════════════════════════════════════════════════════════════════════
with tab2:
    st.header("Whale Overlap")
    st.caption("See what tokens a group of wallets share — find what the big players are all holding.")

    with st.expander("ℹ️ How to use", expanded=False):
        st.markdown("""
**Two ways to load wallets:**
- Run the Cohort Analyzer first → Whales, Sharks & Dolphins auto-populate here
- Or paste wallet addresses directly (one per line)

Results show every token held by 2+ of the wallets, ranked by how many wallets share it.
Stablecoins and WETH are filtered out automatically.
""")

    source = st.radio(
        "Wallet source",
        ["Use Whales/Sharks/Dolphins from Cohort tab", "Paste wallets manually", "Upload new CSV"],
        key="t2_source",
        horizontal=True,
    )

    t2_wallets = []

    if source == "Use Whales/Sharks/Dolphins from Cohort tab":
        saved = st.session_state.get("whale_wallets", [])
        if saved:
            st.success(f"{len(saved)} wallets loaded from Cohort Analysis (Whales, Sharks & Dolphins).")
            t2_wallets = saved
            with st.expander("View wallets"):
                for w in saved:
                    st.code(w)
        else:
            st.info("Run the Cohort Analyzer first to populate this automatically.")

    elif source == "Paste wallets manually":
        raw = st.text_area(
            "Paste wallet addresses (one per line)",
            height=150,
            placeholder="0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045\n...",
            key="t2_paste",
        )
        if raw.strip():
            t2_wallets = [
                w.strip().lower() for w in raw.strip().splitlines()
                if w.strip().startswith("0x") and len(w.strip()) == 42
            ]
            st.caption(f"{len(t2_wallets)} addresses detected.")

    else:
        t2_file = st.file_uploader("Upload wallet CSV", type=["csv"], key="t2_file")
        if t2_file:
            t2_wallets = parse_wallets_from_csv(t2_file)
            if t2_wallets:
                st.caption(f"{len(t2_wallets)} addresses found.")
            else:
                st.error("No valid Base (0x...) addresses detected in CSV.")

    t2_max     = st.slider("Max wallets to scan", 5, MAX_WALLETS, 30, 5, key="t2_max")
    min_shared = st.slider("Min wallets sharing a token (filter noise)", 2, 10, 2, 1, key="t2_min")

    t2_btn = st.button(
        "🔍 Run Overlap Analysis",
        type="primary",
        disabled=not (moralis_key and t2_wallets),
        key="t2_btn",
    )

    if t2_btn:
        wallets = t2_wallets[:t2_max]
        if len(t2_wallets) > t2_max:
            st.info(f"Capped to {t2_max} wallets.")

        st.markdown("---")
        prog2   = st.progress(0)
        status2 = st.empty()

        token_counts   = defaultdict(int)
        token_metadata = {}
        token_holders  = defaultdict(list)

        for i, wallet in enumerate(wallets):
            status2.text(f"[{i+1}/{len(wallets)}] {wallet[:12]}...")
            tokens = get_wallet_tokens(wallet, moralis_key)
            seen_this_wallet = set()

            for t in tokens:
                addr = normalise_address(t.get("token_address", ""))
                if not addr or addr in SKIP_TOKENS:
                    continue
                # Skip tokens with no balance
                try:
                    bal = float(t.get("balance_formatted", 0) or 0)
                except (ValueError, TypeError):
                    bal = 0
                if bal <= 0 or addr in seen_this_wallet:
                    continue

                seen_this_wallet.add(addr)
                token_counts[addr] += 1
                token_holders[addr].append(wallet)

                if addr not in token_metadata:
                    try:
                        price = float(t.get("usd_price", 0) or 0)
                    except (ValueError, TypeError):
                        price = 0.0
                    token_metadata[addr] = {
                        "symbol":    t.get("symbol", "???"),
                        "name":      t.get("name", "Unknown"),
                        "price_usd": price,
                    }

            prog2.progress((i + 1) / len(wallets))
            time.sleep(0.15)

        status2.empty()
        prog2.empty()

        shared        = {m: c for m, c in token_counts.items() if c >= min_shared}
        sorted_tokens = sorted(shared.items(), key=lambda x: -x[1])

        if not sorted_tokens:
            st.warning(f"No tokens found shared by {min_shared}+ wallets.")
        else:
            st.subheader(f"🏆 {len(sorted_tokens)} shared tokens found")

            summary_rows = []
            for addr, count in sorted_tokens[:50]:
                meta = token_metadata[addr]
                summary_rows.append({
                    "Symbol":          meta["symbol"],
                    "Name":            meta["name"],
                    "Wallets Holding": count,
                    "% of Group":      f"{count/len(wallets)*100:.1f}%",
                    "Price (USD)":     f"${meta['price_usd']:,.6f}" if meta["price_usd"] > 0 else "—",
                    "Contract":        addr,
                })
            st.dataframe(pd.DataFrame(summary_rows), use_container_width=True, hide_index=True)

            st.markdown("---")
            st.subheader("🔎 Token Detail")
            for addr, count in sorted_tokens[:30]:
                meta    = token_metadata[addr]
                holders = token_holders[addr]
                pct     = count / len(wallets) * 100
                with st.expander(f"**{meta['symbol']}** — {count} wallets ({pct:.1f}%)  ·  {meta['name']}"):
                    st.caption(f"Contract: `{addr}`")
                    basescan_url = f"https://basescan.org/token/{addr}"
                    st.markdown(f"[View on Basescan]({basescan_url})")
                    if meta["price_usd"] > 0:
                        st.caption(f"Price: ${meta['price_usd']:,.6f}")
                    for h in holders:
                        st.code(h)

            st.markdown("---")
            dl_rows = []
            for addr, count in sorted_tokens:
                meta = token_metadata[addr]
                for h in token_holders[addr]:
                    dl_rows.append({
                        "contract":        addr,
                        "symbol":          meta["symbol"],
                        "name":            meta["name"],
                        "wallets_holding": count,
                        "wallet":          h,
                    })
            csv2 = pd.DataFrame(dl_rows).to_csv(index=False).encode()
            st.download_button("⬇️ Download overlap CSV", csv2, "whale_overlap_base.csv", "text/csv")


# ══════════════════════════════════════════════════════════════════════════════
#  TAB 3 — RECENT ACQUISITIONS
# ══════════════════════════════════════════════════════════════════════════════
with tab3:
    st.header("Recent Buys")
    st.caption("What tokens have whales/sharks/dolphins actually received in the last N days?")

    with st.expander("ℹ️ How to use", expanded=False):
        st.markdown("""
- Run **Cohort Analyzer** first to auto-populate wallets, or paste/upload your own list
- Set your lookback window (1–30 days)
- Results show every ERC-20 token received, flagged when 2+ wallets received the same one — that's your coordination signal
- Stablecoins and WETH are filtered automatically
- Note: this catches all *inbound transfers*, including buys, airdrops, and LP withdrawals
""")

    t3_source = st.radio(
        "Wallet source",
        ["Use Whales/Sharks/Dolphins from Cohort tab", "Paste wallets manually", "Upload new CSV"],
        key="t3_source",
        horizontal=True,
    )

    t3_wallets = []

    if t3_source == "Use Whales/Sharks/Dolphins from Cohort tab":
        saved3 = st.session_state.get("whale_wallets", [])
        if saved3:
            st.success(f"{len(saved3)} wallets loaded from Cohort Analysis (Whales, Sharks & Dolphins).")
            t3_wallets = saved3
            with st.expander("View wallets"):
                for w in saved3:
                    st.code(w)
        else:
            st.info("Run the Cohort Analyzer first to populate this automatically.")

    elif t3_source == "Paste wallets manually":
        raw3 = st.text_area(
            "Paste wallet addresses (one per line)",
            height=150,
            placeholder="0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045\n...",
            key="t3_paste",
        )
        if raw3.strip():
            t3_wallets = [
                w.strip().lower() for w in raw3.strip().splitlines()
                if w.strip().startswith("0x") and len(w.strip()) == 42
            ]
            st.caption(f"{len(t3_wallets)} addresses detected.")

    else:
        t3_file = st.file_uploader("Upload wallet CSV", type=["csv"], key="t3_file")
        if t3_file:
            t3_wallets = parse_wallets_from_csv(t3_file)
            if t3_wallets:
                st.caption(f"{len(t3_wallets)} addresses found.")
            else:
                st.error("No valid Base (0x...) addresses detected in CSV.")

    col_a, col_b = st.columns(2)
    with col_a:
        t3_days = st.slider("Lookback (days)", 1, 30, 7, 1, key="t3_days")
    with col_b:
        t3_max  = st.slider("Max wallets to scan", 5, 50, 20, 5, key="t3_max",
                             help="Each wallet fetches up to 500 transfers — keep low for speed")

    t3_min_shared = st.slider(
        "Highlight when received by N+ wallets",
        2, 10, 2, 1, key="t3_min_shared",
        help="Tokens received by this many wallets are flagged as coordination signals",
    )

    t3_btn = st.button(
        "📅 Run Acquisition Scan",
        type="primary",
        disabled=not (moralis_key and t3_wallets),
        key="t3_btn",
    )

    if t3_btn:
        wallets3   = t3_wallets[:t3_max]
        from_dt    = datetime.now(timezone.utc) - timedelta(days=t3_days)
        from_date  = from_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        cutoff_str = from_dt.strftime("%Y-%m-%d")

        if len(t3_wallets) > t3_max:
            st.info(f"Capped to {t3_max} wallets.")

        st.markdown(f"**Scanning {len(wallets3)} wallets for inbound transfers since {cutoff_str}...**")
        st.caption("Fetches up to 500 transfers per wallet — ~1–3s per wallet.")

        prog3   = st.progress(0)
        status3 = st.empty()

        all_acq        = []
        token_wallets3 = defaultdict(set)
        token_meta3    = {}

        for i, wallet in enumerate(wallets3):
            status3.text(f"[{i+1}/{len(wallets3)}] {wallet[:12]}... fetching transfers")
            transfers = get_erc20_transfers_in(wallet, moralis_key, from_date)
            inflows   = parse_inflow_events(transfers, wallet)

            for acq in inflows:
                mint = acq["mint"]
                token_wallets3[mint].add(wallet)
                all_acq.append(acq)
                if mint not in token_meta3:
                    token_meta3[mint] = {
                        "symbol": acq["symbol"],
                        "name":   acq["name"],
                    }

            prog3.progress((i + 1) / len(wallets3))
            time.sleep(0.15)

        status3.empty()
        prog3.empty()

        if not all_acq:
            st.warning(f"No ERC-20 inflows found in the last {t3_days} days for these wallets.")
        else:
            # Build summary
            summary = []
            for mint, buying_wallets in token_wallets3.items():
                meta      = token_meta3.get(mint, {"symbol": mint[:8], "name": ""})
                n_buys    = len(buying_wallets)
                events    = [a for a in all_acq if a["mint"] == mint]
                total_amt = sum(e["amount_received"] for e in events)
                latest    = max(e["date"] for e in events)
                summary.append({
                    "mint":           mint,
                    "symbol":         meta["symbol"],
                    "name":           meta["name"],
                    "wallets_bought": n_buys,
                    "total_received": round(total_amt, 4),
                    "last_seen":      latest,
                    "coordinated":    n_buys >= t3_min_shared,
                })

            summary.sort(key=lambda x: (-x["wallets_bought"], x["last_seen"]))

            # ── coordination signals ──────────────────────────────────────────
            coordinated = [s for s in summary if s["coordinated"]]
            if coordinated:
                st.markdown("---")
                st.subheader(f"🚨 Coordination Signals — received by {t3_min_shared}+ wallets")
                st.caption("These tokens were independently received by multiple whales/sharks/dolphins in your window.")
                coord_rows = []
                for s in coordinated:
                    coord_rows.append({
                        "Symbol":         s["symbol"],
                        "Name":           s["name"],
                        "Wallets":        s["wallets_bought"],
                        "Total Received": s["total_received"],
                        "Last Transfer":  s["last_seen"],
                        "Contract":       s["mint"],
                    })
                st.dataframe(pd.DataFrame(coord_rows), use_container_width=True, hide_index=True)

                for s in coordinated:
                    with st.expander(f"**{s['symbol']}** — {s['wallets_bought']} wallets · {s['name']}"):
                        st.caption(f"Contract: `{s['mint']}`")
                        st.markdown(f"[View on Basescan](https://basescan.org/token/{s['mint']})")
                        events = sorted(
                            [a for a in all_acq if a["mint"] == s["mint"]],
                            key=lambda x: x["timestamp"], reverse=True,
                        )
                        for ev in events:
                            st.markdown(
                                f"- `{ev['wallet'][:12]}...`  "
                                f"+{ev['amount_received']:,.4f} tokens  ·  {ev['date']}  "
                                f"· [tx](https://basescan.org/tx/{ev['tx_sig']})"
                            )
            else:
                st.info(f"No tokens received by {t3_min_shared}+ wallets in this window. Try lowering the threshold or extending the lookback.")

            # ── full table ────────────────────────────────────────────────────
            st.markdown("---")
            st.subheader(f"📋 All inflows ({len(summary)} unique tokens)")
            all_rows = []
            for s in summary:
                all_rows.append({
                    "Symbol":         s["symbol"],
                    "Name":           s["name"],
                    "Wallets":        s["wallets_bought"],
                    "Total Received": s["total_received"],
                    "Last Transfer":  s["last_seen"],
                    "🚨 Signal":      "✅" if s["coordinated"] else "",
                    "Contract":       s["mint"],
                })
            st.dataframe(pd.DataFrame(all_rows), use_container_width=True, hide_index=True)

            # ── download ──────────────────────────────────────────────────────
            st.markdown("---")
            dl3_rows = []
            for acq in all_acq:
                meta = token_meta3.get(acq["mint"], {"symbol": "", "name": ""})
                dl3_rows.append({
                    "wallet":          acq["wallet"],
                    "contract":        acq["mint"],
                    "symbol":          meta["symbol"],
                    "name":            meta["name"],
                    "amount_received": acq["amount_received"],
                    "date":            acq["date"],
                    "tx_hash":         acq["tx_sig"],
                    "wallets_received": len(token_wallets3[acq["mint"]]),
                    "coordinated":     len(token_wallets3[acq["mint"]]) >= t3_min_shared,
                })
            csv3 = pd.DataFrame(dl3_rows).sort_values(
                ["coordinated", "wallets_received"], ascending=[False, False]
            ).to_csv(index=False).encode()
            st.download_button(
                "⬇️ Download acquisition CSV", csv3,
                f"whale_acquisitions_base_{t3_days}d.csv", "text/csv",
            )
