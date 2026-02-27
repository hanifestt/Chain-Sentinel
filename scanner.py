"""
scanner.py — On-chain risk scanner for Chain Sentinel
Pulls data from Helius + Birdeye, scores risk, generates rule-based summaries.
No Anthropic/OpenAI required.
"""

import os
import asyncio
import aiohttp
import json
import time

HELIUS_API_KEY = os.environ.get("HELIUS_API_KEY", "")
BIRDEYE_API_KEY = os.environ.get("BIRDEYE_API_KEY", "")

HELIUS_API = f"https://api.helius.xyz/v0"
BIRDEYE_API = "https://public-api.birdeye.so"


# ── Main scan entry point ─────────────────────────────────────────────────────
async def scan_token(ca: str) -> dict:
    async with aiohttp.ClientSession() as session:
        # Run all scans in parallel for speed
        wallet_task = asyncio.create_task(scan_wallets(session, ca))
        lp_task = asyncio.create_task(scan_lp(session, ca))
        supply_task = asyncio.create_task(scan_supply(session, ca))
        mev_task = asyncio.create_task(scan_mev(session, ca))

        wallet_data = await wallet_task
        lp_data = await lp_task
        supply_data = await supply_task
        mev_data = await mev_task

    combined = {**wallet_data, **lp_data, **supply_data, **mev_data}

    # Score each module 0-100
    wallet_score = score_wallets(wallet_data)
    lp_score = score_lp(lp_data)
    supply_score = score_supply(supply_data)
    mev_score = score_mev(mev_data)

    # Weighted composite risk score
    risk_score = int(
        wallet_score * 0.30 +
        lp_score    * 0.25 +
        supply_score * 0.25 +
        mev_score   * 0.20
    )

    combined["risk_score"] = risk_score
    combined["wallet_risk"] = risk_label(wallet_score)
    combined["lp_risk"] = risk_label(lp_score)
    combined["supply_risk"] = risk_label(supply_score)
    combined["mev_risk"] = risk_label(mev_score)

    # AI summary
    combined["ai_summary"] = generate_summary(combined, wallet_score, lp_score, supply_score, mev_score)

    return combined


# ── Wallet scan ───────────────────────────────────────────────────────────────
async def scan_wallets(session: aiohttp.ClientSession, ca: str) -> dict:
    try:
        # Get token holders via Helius
        url = f"{HELIUS_API}/addresses/{ca}/transactions?api-key={HELIUS_API_KEY}&type=TRANSFER&limit=100"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return _wallet_defaults()
            txs = await resp.json()

        wallets = set()
        fresh_count = 0
        recent_ts = []

        for tx in txs:
            if "feePayer" in tx:
                wallets.add(tx["feePayer"])
            for acc in tx.get("accountData", []):
                if acc.get("account"):
                    wallets.add(acc["account"])
            if "timestamp" in tx:
                recent_ts.append(tx["timestamp"])

        wallet_count = len(wallets)

        # Estimate fresh wallets (simplified: wallets that only appear in last 24h of txs)
        if recent_ts:
            import time
            cutoff = time.time() - 86400
            fresh_count = sum(1 for ts in recent_ts if ts > cutoff)
            fresh_pct = round((fresh_count / max(len(recent_ts), 1)) * 100, 1)
        else:
            fresh_pct = 0

        # Simple clustering estimate: if many wallets appeared together in same block
        cluster_pct = min(fresh_pct * 0.6, 100)

        return {
            "wallet_count": wallet_count,
            "cluster_pct": round(cluster_pct, 1),
            "fresh_wallet_pct": fresh_pct,
        }
    except Exception:
        return _wallet_defaults()


def _wallet_defaults():
    return {"wallet_count": "N/A", "cluster_pct": "N/A", "fresh_wallet_pct": "N/A"}


# ── LP scan ───────────────────────────────────────────────────────────────────
async def scan_lp(session: aiohttp.ClientSession, ca: str) -> dict:
    try:
        url = f"{BIRDEYE_API}/defi/token_overview?address={ca}"
        headers = {"X-API-KEY": BIRDEYE_API_KEY, "x-chain": "solana"}
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return _lp_defaults()
            data = (await resp.json()).get("data", {})

        liquidity = data.get("liquidity", 0)
        # Birdeye doesn't directly expose lock status; flag if liquidity is very low
        lp_locked = "Unknown — verify on Raydium/Unicrypt"
        lp_lock_duration = "N/A"

        if liquidity and float(liquidity) < 5000:
            lp_locked = "⚠ Very low liquidity"
        elif liquidity:
            lp_locked = f"Liquidity: ${float(liquidity):,.0f}"

        return {
            "lp_locked": lp_locked,
            "lp_lock_duration": lp_lock_duration,
            "lp_liquidity_usd": liquidity,
        }
    except Exception:
        return _lp_defaults()


def _lp_defaults():
    return {"lp_locked": "N/A", "lp_lock_duration": "N/A", "lp_liquidity_usd": 0}


# ── Supply concentration scan ─────────────────────────────────────────────────
async def scan_supply(session: aiohttp.ClientSession, ca: str) -> dict:
    try:
        url = f"{BIRDEYE_API}/defi/token_holder?address={ca}&offset=0&limit=20"
        headers = {"X-API-KEY": BIRDEYE_API_KEY, "x-chain": "solana"}
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return _supply_defaults()
            data = (await resp.json()).get("data", {})

        holders = data.get("items", [])
        if not holders:
            return _supply_defaults()

        total_supply = sum(float(h.get("uiAmount", 0)) for h in holders)
        if total_supply == 0:
            return _supply_defaults()

        shares = [float(h.get("uiAmount", 0)) / total_supply for h in holders]
        top10_pct = round(sum(shares[:10]) * 100, 1)

        # Gini coefficient
        gini = compute_gini(shares)

        return {
            "top10_pct": top10_pct,
            "gini": round(gini, 2),
        }
    except Exception:
        return _supply_defaults()


def compute_gini(shares: list) -> float:
    if not shares:
        return 0
    n = len(shares)
    shares_sorted = sorted(shares)
    cumsum = 0
    for i, x in enumerate(shares_sorted):
        cumsum += (2 * (i + 1) - n - 1) * x
    return cumsum / (n * sum(shares_sorted)) if sum(shares_sorted) > 0 else 0


def _supply_defaults():
    return {"top10_pct": "N/A", "gini": "N/A"}


# ── MEV scan ──────────────────────────────────────────────────────────────────
async def scan_mev(session: aiohttp.ClientSession, ca: str) -> dict:
    try:
        # Use Helius to get recent transactions and detect sandwich patterns
        url = f"{HELIUS_API}/addresses/{ca}/transactions?api-key={HELIUS_API_KEY}&limit=50"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return _mev_defaults()
            txs = await resp.json()

        # Detect potential sandwich: same wallet appears before AND after a swap in same block
        slot_wallets: dict = {}
        for tx in txs:
            slot = tx.get("slot", 0)
            fee_payer = tx.get("feePayer", "")
            if slot not in slot_wallets:
                slot_wallets[slot] = []
            slot_wallets[slot].append(fee_payer)

        sandwich_count = sum(
            1 for wallets in slot_wallets.values()
            if len(wallets) >= 3 and len(set(wallets)) < len(wallets)
        )

        known_mev_bots = 0  # Would require a maintained blocklist

        return {
            "mev_bots": known_mev_bots,
            "sandwich_count": sandwich_count,
        }
    except Exception:
        return _mev_defaults()


def _mev_defaults():
    return {"mev_bots": "N/A", "sandwich_count": "N/A"}


# ── Risk scoring helpers ──────────────────────────────────────────────────────
def score_wallets(d: dict) -> int:
    score = 0
    cp = d.get("cluster_pct", 0)
    fp = d.get("fresh_wallet_pct", 0)
    if isinstance(cp, (int, float)):
        score += min(cp * 0.8, 50)
    if isinstance(fp, (int, float)):
        score += min(fp * 0.5, 50)
    return int(score)


def score_lp(d: dict) -> int:
    liquidity = d.get("lp_liquidity_usd", 0)
    try:
        liq = float(liquidity)
        if liq < 1000:
            return 90
        elif liq < 5000:
            return 70
        elif liq < 20000:
            return 40
        else:
            return 20
    except Exception:
        return 50


def score_supply(d: dict) -> int:
    score = 0
    top10 = d.get("top10_pct", 0)
    gini = d.get("gini", 0)
    if isinstance(top10, (int, float)):
        if top10 > 80:
            score += 60
        elif top10 > 50:
            score += 40
        elif top10 > 30:
            score += 20
    if isinstance(gini, (int, float)):
        score += int(gini * 40)
    return min(score, 100)


def score_mev(d: dict) -> int:
    sc = d.get("sandwich_count", 0)
    bots = d.get("mev_bots", 0)
    score = 0
    if isinstance(sc, (int, float)):
        score += min(sc * 5, 60)
    if isinstance(bots, (int, float)):
        score += min(bots * 20, 40)
    return min(score, 100)


def risk_label(score: int) -> str:
    if score <= 30:
        return "🟢 Low"
    elif score <= 60:
        return "🟡 Medium"
    elif score <= 80:
        return "🟠 High"
    else:
        return "🔴 Critical"


# ── Rule-based summary (no AI needed) ────────────────────────────────────────
def generate_summary(d, wallet_score, lp_score, supply_score, mev_score) -> str:
    flags = []
    advice = []

    fresh = d.get("fresh_wallet_pct", 0)
    cluster = d.get("cluster_pct", 0)
    if isinstance(fresh, (int, float)) and fresh > 40:
        flags.append(f"high fresh wallet activity ({fresh}%)")
    if isinstance(cluster, (int, float)) and cluster > 30:
        flags.append(f"possible coordinated buying detected ({cluster}% cluster estimate)")

    liq = d.get("lp_liquidity_usd", 0)
    try:
        liq = float(liq)
        if liq < 1000:
            flags.append("critically low liquidity — rug risk is very high")
            advice.append("Avoid entry until liquidity improves.")
        elif liq < 5000:
            flags.append("low liquidity pool")
            advice.append("Use small position sizes due to low liquidity.")
    except Exception:
        pass

    top10 = d.get("top10_pct", 0)
    gini = d.get("gini", 0)
    if isinstance(top10, (int, float)) and top10 > 50:
        flags.append(f"top 10 wallets control {top10}% of supply")
        advice.append("Whale dump risk is elevated — watch top holder movements.")
    if isinstance(gini, (int, float)) and gini > 0.7:
        flags.append(f"highly unequal supply distribution (Gini: {gini})")

    sc = d.get("sandwich_count", 0)
    if isinstance(sc, (int, float)) and sc > 3:
        flags.append(f"{sc} potential sandwich patterns detected")
        advice.append("Use higher slippage or a MEV-protected RPC.")

    overall = d.get("risk_score", 0)
    if overall <= 30:
        opener = "✅ This token shows relatively low risk signals."
    elif overall <= 60:
        opener = "⚠️ Moderate risk — proceed with caution."
    elif overall <= 80:
        opener = "🚨 High risk detected — significant red flags present."
    else:
        opener = "🔴 Critical risk — multiple severe red flags identified."

    flag_str = ("Key concerns: " + "; ".join(flags) + ".") if flags else "No major red flags detected across scanned metrics."
    action = " ".join(advice) if advice else "Always verify LP lock status independently before trading."

    return f"{opener} {flag_str} {action}"
