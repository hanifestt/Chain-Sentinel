"""
scanner.py — On-chain risk scanner for Chain Sentinel
Uses Helius RPC for wallet/MEV data and Birdeye for supply/LP data.
"""

import os
import asyncio
import aiohttp
import time

HELIUS_API_KEY = os.environ.get("HELIUS_API_KEY", "")
BIRDEYE_API_KEY = os.environ.get("BIRDEYE_API_KEY", "")

HELIUS_RPC  = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
HELIUS_API  = "https://api.helius.xyz/v0"
BIRDEYE_API = "https://public-api.birdeye.so"


# ── Main entry ────────────────────────────────────────────────────────────────
async def scan_token(ca: str) -> dict:
    async with aiohttp.ClientSession() as session:
        wallet_data, lp_data, supply_data, mev_data, dev_data = await asyncio.gather(
            scan_wallets(session, ca),
            scan_lp(session, ca),
            scan_supply(session, ca),
            scan_mev(session, ca),
            get_dev_alpha(ca),
        )

    combined = {**wallet_data, **lp_data, **supply_data, **mev_data}
    combined["dev"] = dev_data

    ws = score_wallets(wallet_data)
    ls = score_lp(lp_data)
    ss = score_supply(supply_data)
    ms = score_mev(mev_data)

    combined["risk_score"]   = int(ws*0.30 + ls*0.25 + ss*0.25 + ms*0.20)
    combined["wallet_risk"]  = risk_label(ws)
    combined["lp_risk"]      = risk_label(ls)
    combined["supply_risk"]  = risk_label(ss)
    combined["mev_risk"]     = risk_label(ms)
    combined["ai_summary"]   = generate_summary(combined, ws, ls, ss, ms)
    return combined


# ── Wallet scan — uses Helius getTokenAccounts RPC ───────────────────────────
async def scan_wallets(session: aiohttp.ClientSession, ca: str) -> dict:
    try:
        payload = {
            "jsonrpc": "2.0", "id": 1,
            "method": "getTokenAccounts",
            "params": {
                "mint": ca,
                "limit": 100,
                "displayOptions": {"showZeroBalance": False}
            }
        }
        async with session.post(HELIUS_RPC, json=payload, timeout=aiohttp.ClientTimeout(total=12)) as resp:
            if resp.status != 200:
                return _wallet_defaults()
            data = await resp.json()

        accounts = data.get("result", {})
        if isinstance(accounts, dict):
            accounts = accounts.get("token_accounts", [])
        if not accounts:
            return _wallet_defaults()

        wallet_count = len(accounts)

        # Check how many wallets were recently active via transaction history
        tx_url = f"{HELIUS_API}/addresses/{ca}/transactions?api-key={HELIUS_API_KEY}&limit=100&type=TRANSFER"
        async with session.get(tx_url, timeout=aiohttp.ClientTimeout(total=12)) as resp2:
            txs = await resp2.json() if resp2.status == 200 else []

        cutoff = time.time() - 86400
        recent_wallets = set()
        all_wallets = set()
        slot_map: dict = {}

        for tx in txs if isinstance(txs, list) else []:
            fp = tx.get("feePayer", "")
            ts = tx.get("timestamp", 0)
            slot = tx.get("slot", 0)
            if fp:
                all_wallets.add(fp)
                slot_map.setdefault(slot, []).append(fp)
                if ts > cutoff:
                    recent_wallets.add(fp)

        fresh_pct = round(len(recent_wallets) / max(len(all_wallets), 1) * 100, 1)

        # Cluster detection: multiple wallets active in same slot
        clustered = sum(1 for ws in slot_map.values() if len(set(ws)) > 2)
        cluster_pct = round(min(clustered / max(len(slot_map), 1) * 100, 100), 1)

        return {
            "wallet_count": wallet_count,
            "cluster_pct": cluster_pct,
            "fresh_wallet_pct": fresh_pct,
        }
    except Exception as e:
        return _wallet_defaults()

def _wallet_defaults():
    return {"wallet_count": "N/A", "cluster_pct": "N/A", "fresh_wallet_pct": "N/A"}


# ── LP scan — Birdeye token overview ─────────────────────────────────────────
async def scan_lp(session: aiohttp.ClientSession, ca: str) -> dict:
    try:
        headers = {"X-API-KEY": BIRDEYE_API_KEY, "x-chain": "solana"}
        url = f"{BIRDEYE_API}/defi/token_overview?address={ca}"
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=12)) as resp:
            if resp.status != 200:
                return _lp_defaults()
            data = (await resp.json()).get("data", {})

        liq = float(data.get("liquidity") or 0)
        volume_24h = float(data.get("v24hUSD") or 0)
        price = float(data.get("price") or 0)
        mc = float(data.get("mc") or 0)

        if liq == 0:
            lp_status = "⚠ No liquidity data found"
        elif liq < 1000:
            lp_status = f"🔴 Very low — ${liq:,.0f} (high rug risk)"
        elif liq < 10000:
            lp_status = f"🟡 Low — ${liq:,.0f}"
        elif liq < 50000:
            lp_status = f"🟢 Moderate — ${liq:,.0f}"
        else:
            lp_status = f"🟢 Strong — ${liq:,.0f}"

        return {
            "lp_locked": lp_status,
            "lp_lock_duration": "Verify lock on Raydium/Unicrypt",
            "lp_liquidity_usd": liq,
            "volume_24h": volume_24h,
            "price": price,
            "market_cap": mc,
        }
    except Exception:
        return _lp_defaults()

def _lp_defaults():
    return {"lp_locked": "N/A", "lp_lock_duration": "N/A", "lp_liquidity_usd": 0, "volume_24h": 0, "price": 0, "market_cap": 0}


# ── Supply concentration — Helius RPC getLargestAccounts ─────────────────────
async def scan_supply(session: aiohttp.ClientSession, ca: str) -> dict:
    import logging
    logger = logging.getLogger(__name__)
    try:
        # Step 1: get total supply
        total_supply = await get_total_supply(session, ca)
        logger.info(f"[SUPPLY] total_supply={total_supply}")
        if not total_supply or total_supply == 0:
            logger.warning("[SUPPLY] total_supply is 0 or None")
            return _supply_defaults()

        # Step 2: use getTokenLargestAccounts — simpler, always works on free Helius
        payload = {
            "jsonrpc": "2.0", "id": 1,
            "method": "getTokenLargestAccounts",
            "params": [ca]
        }
        async with session.post(HELIUS_RPC, json=payload, timeout=aiohttp.ClientTimeout(total=12)) as resp:
            status = resp.status
            raw = await resp.json()
            logger.info(f"[SUPPLY] getTokenLargestAccounts status={status} raw={str(raw)[:300]}")

        if status != 200:
            logger.warning(f"[SUPPLY] bad status {status}")
            return _supply_defaults()

        error = raw.get("error")
        if error:
            logger.warning(f"[SUPPLY] RPC error: {error}")
            return _supply_defaults()

        value = raw.get("result", {}).get("value", [])
        logger.info(f"[SUPPLY] got {len(value)} largest accounts")

        if not value:
            return _supply_defaults()

        # value is list of {address, amount, decimals, uiAmount, uiAmountString}
        amounts = []
        for item in value:
            ui = item.get("uiAmount")
            if ui is None:
                # fallback: raw amount / 10^decimals
                raw_amt = int(item.get("amount", 0))
                dec = int(item.get("decimals", 0))
                ui = raw_amt / (10 ** dec) if dec else raw_amt
            amounts.append(float(ui))

        amounts = sorted([a for a in amounts if a > 0], reverse=True)
        logger.info(f"[SUPPLY] amounts (top5): {amounts[:5]}")

        if not amounts:
            return _supply_defaults()

        shares = [a / total_supply for a in amounts]
        top10_pct = round(sum(shares[:10]) * 100, 1)
        top1_pct  = round(shares[0] * 100, 2)
        gini      = round(compute_gini(shares), 2)

        logger.info(f"[SUPPLY] top10={top10_pct}% top1={top1_pct}% gini={gini}")

        return {
            "top10_pct": top10_pct,
            "top1_pct":  top1_pct,
            "gini":      gini,
            "holder_count": len(amounts),
        }
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"[SUPPLY] exception: {e}", exc_info=True)
        return _supply_defaults()

async def get_total_supply(session: aiohttp.ClientSession, ca: str) -> float:
    import logging
    logger = logging.getLogger(__name__)
    try:
        payload = {"jsonrpc": "2.0", "id": 1, "method": "getTokenSupply", "params": [ca]}
        async with session.post(HELIUS_RPC, json=payload, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            data = await resp.json()
            logger.info(f"[SUPPLY] getTokenSupply raw: {str(data)[:200]}")
            val = data.get("result", {}).get("value", {})
            ui = val.get("uiAmount")
            if ui is None:
                raw_amt = int(val.get("amount", 0))
                dec = int(val.get("decimals", 0))
                ui = raw_amt / (10 ** dec) if dec else raw_amt
            return float(ui or 0)
    except Exception as e:
        logging.getLogger(__name__).error(f"[SUPPLY] getTokenSupply error: {e}")
        return 0.0

def compute_gini(shares):
    if not shares: return 0
    n = len(shares)
    s = sorted(shares)
    cumsum = sum((2*(i+1) - n - 1) * x for i, x in enumerate(s))
    total = sum(s)
    return cumsum / (n * total) if total > 0 else 0

def _supply_defaults():
    return {"top10_pct": "N/A", "top1_pct": "N/A", "gini": "N/A", "holder_count": "N/A"}


# ── MEV scan — Helius transaction history ─────────────────────────────────────
async def scan_mev(session: aiohttp.ClientSession, ca: str) -> dict:
    try:
        url = f"{HELIUS_API}/addresses/{ca}/transactions?api-key={HELIUS_API_KEY}&limit=100"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=12)) as resp:
            if resp.status != 200:
                return _mev_defaults()
            txs = await resp.json()

        if not isinstance(txs, list):
            return _mev_defaults()

        slot_map: dict = {}
        for tx in txs:
            slot = tx.get("slot", 0)
            fp   = tx.get("feePayer", "")
            slot_map.setdefault(slot, []).append(fp)

        # Sandwich = same wallet appears as first AND last in a slot with 3+ txs
        sandwich_count = 0
        for wallets in slot_map.values():
            if len(wallets) >= 3 and wallets[0] == wallets[-1] and wallets[0] != "":
                sandwich_count += 1

        # Count repeated fee payers (bot behavior)
        from collections import Counter
        payer_counts = Counter(tx.get("feePayer", "") for tx in txs)
        bot_wallets = sum(1 for cnt in payer_counts.values() if cnt >= 5)

        return {"mev_bots": bot_wallets, "sandwich_count": sandwich_count}
    except Exception:
        return _mev_defaults()

def _mev_defaults():
    return {"mev_bots": "N/A", "sandwich_count": "N/A"}


# ── Scoring ───────────────────────────────────────────────────────────────────
def score_wallets(d):
    score = 0
    cp = d.get("cluster_pct", 0)
    fp = d.get("fresh_wallet_pct", 0)
    if isinstance(cp, (int, float)): score += min(cp * 0.8, 50)
    if isinstance(fp, (int, float)): score += min(fp * 0.5, 50)
    return int(score)

def score_lp(d):
    liq = d.get("lp_liquidity_usd", 0)
    try:
        liq = float(liq)
        if liq == 0:    return 80
        if liq < 1000:  return 90
        if liq < 5000:  return 70
        if liq < 20000: return 40
        return 20
    except Exception:
        return 50

def score_supply(d):
    score = 0
    top10 = d.get("top10_pct", 0)
    gini  = d.get("gini", 0)
    if isinstance(top10, (int, float)):
        if top10 > 80:   score += 60
        elif top10 > 50: score += 40
        elif top10 > 30: score += 20
    if isinstance(gini, (int, float)):
        score += int(gini * 40)
    return min(score, 100)

def score_mev(d):
    sc   = d.get("sandwich_count", 0)
    bots = d.get("mev_bots", 0)
    score = 0
    if isinstance(sc,   (int, float)): score += min(sc * 5, 60)
    if isinstance(bots, (int, float)): score += min(bots * 20, 40)
    return min(score, 100)

def risk_label(score):
    if score <= 30: return "🟢 Low"
    if score <= 60: return "🟡 Medium"
    if score <= 80: return "🟠 High"
    return "🔴 Critical"


# ── Rule-based summary ────────────────────────────────────────────────────────
def generate_summary(d, ws, ls, ss, ms) -> str:
    flags, advice = [], []

    fresh = d.get("fresh_wallet_pct", 0)
    cluster = d.get("cluster_pct", 0)
    if isinstance(fresh, (int, float)) and fresh > 40:
        flags.append(f"high fresh wallet activity ({fresh}%)")
    if isinstance(cluster, (int, float)) and cluster > 30:
        flags.append(f"coordinated buying patterns detected ({cluster}%)")

    liq = d.get("lp_liquidity_usd", 0)
    try:
        liq = float(liq)
        if liq < 1000:
            flags.append("critically low liquidity — rug risk very high")
            advice.append("Avoid entry until liquidity improves.")
        elif liq < 5000:
            flags.append("low liquidity")
            advice.append("Use small position sizes.")
    except Exception:
        pass

    top10 = d.get("top10_pct", 0)
    top1  = d.get("top1_pct", 0)
    if isinstance(top1,  (int, float)) and top1 > 20:
        flags.append(f"single wallet holds {top1}% of supply")
        advice.append("Single-wallet dump risk is very high.")
    if isinstance(top10, (int, float)) and top10 > 50:
        flags.append(f"top 10 wallets hold {top10}% of supply")
        advice.append("Watch top holder movements closely.")

    sc = d.get("sandwich_count", 0)
    bots = d.get("mev_bots", 0)
    if isinstance(sc,   (int, float)) and sc > 2:
        flags.append(f"{sc} sandwich attack patterns detected")
        advice.append("Use MEV-protected RPC or higher slippage.")
    if isinstance(bots, (int, float)) and bots > 0:
        flags.append(f"{bots} suspected bot wallet(s) active")

    overall = d.get("risk_score", 0)
    if overall <= 30:   opener = "✅ Relatively low risk signals."
    elif overall <= 60: opener = "⚠️ Moderate risk — proceed with caution."
    elif overall <= 80: opener = "🚨 High risk — significant red flags."
    else:               opener = "🔴 Critical risk — multiple severe red flags."

    flag_str = ("Key concerns: " + "; ".join(flags) + ".") if flags else "No major red flags detected."
    action   = " ".join(advice) if advice else "Always verify LP lock status before trading."

    return f"{opener} {flag_str} {action}"


# ═══════════════════════════════════════════════════════════════════════════════
# DEV ALPHA — finds the deployer and their full launch history
# ═══════════════════════════════════════════════════════════════════════════════

async def get_dev_alpha(ca: str) -> dict:
    """
    Full dev history analysis:
    1. Find mint authority (or deployer from tx history if renounced)
    2. Find all tokens this wallet deployed in last 60 days
    3. Cross-reference with DexScreener for peak market caps
    4. Return a structured report
    """
    import logging
    logger = logging.getLogger(__name__)

    async with aiohttp.ClientSession() as session:
        # Step 1: get deployer wallet
        deployer = await get_deployer(session, ca)
        logger.info(f"[DEV] deployer={deployer}")

        if not deployer:
            return {"error": "Could not identify deployer wallet."}

        # Step 2: get all tokens they deployed
        tokens = await get_deployed_tokens(session, deployer)
        logger.info(f"[DEV] found {len(tokens)} deployed tokens")

        if not tokens:
            return {
                "deployer": deployer,
                "token_count": 0,
                "tokens": [],
                "summary": f"Deployer `{deployer[:6]}...{deployer[-4:]}` has no other token launches found in recent history."
            }

        # Step 3: enrich with DexScreener data
        enriched = await enrich_with_dexscreener(session, tokens)

        # Step 4: build report
        return build_dev_report(deployer, ca, enriched)


# ── Step 1: Find deployer ─────────────────────────────────────────────────────
async def get_deployer(session: aiohttp.ClientSession, ca: str) -> str:
    import logging
    logger = logging.getLogger(__name__)
    try:
        # Method 1: get mint authority from getAccountInfo
        payload = {
            "jsonrpc": "2.0", "id": 1,
            "method": "getAccountInfo",
            "params": [ca, {"encoding": "jsonParsed"}]
        }
        async with session.post(HELIUS_RPC, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            data = await resp.json()

        parsed = data.get("result", {}).get("value", {}).get("data", {}).get("parsed", {})
        info   = parsed.get("info", {}) if isinstance(parsed, dict) else {}
        mint_authority = info.get("mintAuthority")
        freeze_authority = info.get("freezeAuthority")
        logger.info(f"[DEV] mintAuthority={mint_authority} freezeAuthority={freeze_authority}")

        if mint_authority and mint_authority not in ("null", None, ""):
            return mint_authority
        if freeze_authority and freeze_authority not in ("null", None, ""):
            return freeze_authority

        # Method 2: trace back to creation tx via getSignaturesForAddress
        # Get ALL signatures to find the very first one (creation)
        sigs_payload = {
            "jsonrpc": "2.0", "id": 1,
            "method": "getSignaturesForAddress",
            "params": [ca, {"limit": 1000, "commitment": "finalized"}]
        }
        async with session.post(HELIUS_RPC, json=sigs_payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            sigs_data = await resp.json()

        signatures = sigs_data.get("result", [])
        if not signatures:
            return None

        # Last sig = oldest = creation tx
        oldest_sig = signatures[-1].get("signature")
        if not oldest_sig:
            return None

        logger.info(f"[DEV] creation sig={oldest_sig[:20]}...")

        # Get full transaction
        tx_payload = {
            "jsonrpc": "2.0", "id": 1,
            "method": "getTransaction",
            "params": [oldest_sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}]
        }
        async with session.post(HELIUS_RPC, json=tx_payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            tx_data = await resp.json()

        result = tx_data.get("result", {})
        if not result:
            return None

        # Fee payer is always index 0 in accountKeys and is the deployer
        account_keys = result.get("transaction", {}).get("message", {}).get("accountKeys", [])
        for key in account_keys:
            if isinstance(key, dict):
                if key.get("signer") and key.get("writable"):
                    deployer = key.get("pubkey", "")
                    if deployer and deployer != ca:
                        logger.info(f"[DEV] deployer from tx={deployer}")
                        return deployer
            elif isinstance(key, str) and key != ca:
                return key

        # Method 3: Helius enhanced tx endpoint - feePayer field
        helius_url = f"{HELIUS_API}/transactions/?api-key={HELIUS_API_KEY}"
        async with session.post(helius_url, json={"transactions": [oldest_sig]}, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                txs = await resp.json()
                if isinstance(txs, list) and txs:
                    fee_payer = txs[0].get("feePayer", "")
                    if fee_payer and fee_payer != ca:
                        logger.info(f"[DEV] deployer from Helius enhanced={fee_payer}")
                        return fee_payer

        return None
    except Exception as e:
        logging.getLogger(__name__).error(f"[DEV] get_deployer error: {e}", exc_info=True)
        return None


# ── Step 2: Find all tokens deployed by this wallet ──────────────────────────
async def get_deployed_tokens(session: aiohttp.ClientSession, deployer: str) -> list:
    import logging, time
    logger = logging.getLogger(__name__)
    TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
    TOKEN_2022    = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
    cutoff = time.time() - (60 * 86400)

    try:
        # ── Strategy 1: Helius enhanced API (parsed transactions) ──
        seen = set()
        token_mints = []

        # Try multiple pages to get enough history
        before = None
        for page in range(5):  # up to 500 txs
            url = f"{HELIUS_API}/addresses/{deployer}/transactions?api-key={HELIUS_API_KEY}&limit=100"
            if before:
                url += f"&before={before}"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=12)) as resp:
                if resp.status != 200:
                    break
                txs = await resp.json()

            if not isinstance(txs, list) or not txs:
                break

            oldest_ts = None
            for tx in txs:
                ts = tx.get("timestamp", 0)
                if oldest_ts is None or ts < oldest_ts:
                    oldest_ts = ts

                # Skip if older than 60 days
                if ts < cutoff:
                    continue

                tx_type = tx.get("type", "")

                # Method A: Helius marks these as TOKEN_MINT or CREATE
                if tx_type in ("TOKEN_MINT", "CREATE", "INIT_MINT", "EXECUTE"):
                    for transfer in tx.get("tokenTransfers", []):
                        mint = transfer.get("mint", "")
                        from_addr = transfer.get("fromUserAccount", "")
                        # Only count if deployer is the source (minting)
                        if mint and mint not in seen and (from_addr == deployer or from_addr == ""):
                            seen.add(mint)
                            token_mints.append({"mint": mint, "timestamp": ts})

                # Method B: scan ALL token transfers regardless of type
                for transfer in tx.get("tokenTransfers", []):
                    mint = transfer.get("mint", "")
                    from_addr = transfer.get("fromUserAccount", "")
                    to_addr   = transfer.get("toUserAccount", "")
                    # A mint event has empty fromUserAccount (tokens created from nothing)
                    if mint and mint not in seen and from_addr == "" and to_addr == deployer:
                        seen.add(mint)
                        token_mints.append({"mint": mint, "timestamp": ts})

                # Method C: scan instructions for InitializeMint
                for ix in tx.get("instructions", []):
                    prog = ix.get("programId", "")
                    if prog not in (TOKEN_PROGRAM, TOKEN_2022):
                        continue
                    parsed = ix.get("parsed", {})
                    ix_type = parsed.get("type", "") if isinstance(parsed, dict) else ""
                    if "initializeMint" in ix_type.lower():
                        info = parsed.get("info", {}) if isinstance(parsed, dict) else {}
                        mint = info.get("mint", "")
                        if mint and mint not in seen:
                            seen.add(mint)
                            token_mints.append({"mint": mint, "timestamp": ts})

                # Method D: check inner instructions too
                for inner in tx.get("innerInstructions", []):
                    for ix in inner.get("instructions", []):
                        prog = ix.get("programId", "")
                        if prog not in (TOKEN_PROGRAM, TOKEN_2022):
                            continue
                        parsed = ix.get("parsed", {})
                        ix_type = parsed.get("type", "") if isinstance(parsed, dict) else ""
                        if "initializeMint" in ix_type.lower():
                            info = parsed.get("info", {}) if isinstance(parsed, dict) else {}
                            mint = info.get("mint", "")
                            if mint and mint not in seen:
                                seen.add(mint)
                                token_mints.append({"mint": mint, "timestamp": ts})

            # Stop paginating if we've gone past 60 days
            if oldest_ts and oldest_ts < cutoff:
                break
            # Set cursor for next page
            before = txs[-1].get("signature") if txs else None
            if not before:
                break

        logger.info(f"[DEV] Strategy 1 found {len(token_mints)} mints")

        # ── Strategy 2: Solscan API as fallback if we found nothing ──
        if not token_mints:
            logger.info("[DEV] Trying Solscan fallback...")
            try:
                solscan_url = f"https://pro-api.solscan.io/v2.0/account/token-accounts?address={deployer}&type=token&page=1&page_size=40"
                headers = {"token": os.environ.get("SOLSCAN_API_KEY", "")}
                async with session.get(solscan_url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        for item in data.get("data", []):
                            mint = item.get("tokenAddress", "")
                            ts   = item.get("blockTime", 0)
                            if mint and mint not in seen and ts >= cutoff:
                                seen.add(mint)
                                token_mints.append({"mint": mint, "timestamp": ts})
                        logger.info(f"[DEV] Solscan fallback found {len(token_mints)} tokens")
            except Exception as e2:
                logger.warning(f"[DEV] Solscan fallback failed: {e2}")

        # ── Strategy 3: Use DexScreener directly on deployer ──
        if not token_mints:
            logger.info("[DEV] Trying DexScreener search fallback...")
            try:
                dex_url = f"https://api.dexscreener.com/latest/dex/search?q={deployer}"
                async with session.get(dex_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        for pair in data.get("pairs", []) or []:
                            if pair.get("chainId") != "solana":
                                continue
                            mint = pair.get("baseToken", {}).get("address", "")
                            if mint and mint not in seen:
                                seen.add(mint)
                                token_mints.append({"mint": mint, "timestamp": int(time.time())})
                        logger.info(f"[DEV] DexScreener fallback found {len(token_mints)} tokens")
            except Exception as e3:
                logger.warning(f"[DEV] DexScreener fallback failed: {e3}")

        logger.info(f"[DEV] Total unique mints found: {len(token_mints)}")
        return token_mints[:25]

    except Exception as e:
        logging.getLogger(__name__).error(f"[DEV] get_deployed_tokens error: {e}", exc_info=True)
        return []


# ── Step 3: Enrich with DexScreener ──────────────────────────────────────────
async def enrich_with_dexscreener(session: aiohttp.ClientSession, tokens: list) -> list:
    import logging
    logger = logging.getLogger(__name__)
    enriched = []

    # DexScreener allows batch of up to 30 addresses
    mints = [t["mint"] for t in tokens]
    chunks = [mints[i:i+29] for i in range(0, len(mints), 29)]

    dex_data = {}
    for chunk in chunks:
        try:
            url = f"https://api.dexscreener.com/latest/dex/tokens/{','.join(chunk)}"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=12)) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    for pair in result.get("pairs", []) or []:
                        mint = pair.get("baseToken", {}).get("address", "")
                        if mint and mint not in dex_data:
                            dex_data[mint] = pair
            await asyncio.sleep(0.5)  # rate limit
        except Exception as e:
            logger.error(f"[DEV] dexscreener error: {e}")

    for token in tokens:
        mint = token["mint"]
        pair = dex_data.get(mint, {})
        name = pair.get("baseToken", {}).get("name", "Unknown")
        symbol = pair.get("baseToken", {}).get("symbol", "???")
        mc = pair.get("fdv") or pair.get("marketCap") or 0
        price_usd = pair.get("priceUsd", "0")
        volume_24h = pair.get("volume", {}).get("h24", 0)
        price_change = pair.get("priceChange", {}).get("h24", 0)

        try:
            mc = float(mc)
        except Exception:
            mc = 0

        enriched.append({
            "mint": mint,
            "name": name,
            "symbol": symbol,
            "market_cap": mc,
            "price_usd": price_usd,
            "volume_24h": volume_24h,
            "price_change_24h": price_change,
            "timestamp": token.get("timestamp", 0),
            "on_dex": bool(pair),
        })

    # Sort by market cap descending
    enriched.sort(key=lambda x: x["market_cap"], reverse=True)
    return enriched


# ── Step 4: Build the report ──────────────────────────────────────────────────
def build_dev_report(deployer: str, current_ca: str, tokens: list) -> dict:
    import time

    total = len(tokens)
    on_dex = [t for t in tokens if t["on_dex"]]
    dead = [t for t in tokens if not t["on_dex"]]

    biggest = on_dex[0] if on_dex else None
    biggest_mc = biggest["market_cap"] if biggest else 0

    # Format market cap
    def fmt_mc(mc):
        if mc >= 1_000_000:
            return f"${mc/1_000_000:.2f}M"
        elif mc >= 1_000:
            return f"${mc/1_000:.1f}K"
        else:
            return f"${mc:.0f}"

    # Risk assessment based on history
    if total == 0:
        risk = "🟡 No history found"
        risk_note = "First launch or wallet is new."
    elif len(dead) > len(on_dex) and total > 2:
        risk = "🔴 Serial launcher"
        risk_note = f"{len(dead)}/{total} previous tokens are dead or untraded."
    elif biggest_mc > 1_000_000:
        risk = "🟢 Proven dev"
        risk_note = f"Has launched a token that hit {fmt_mc(biggest_mc)}."
    elif biggest_mc > 100_000:
        risk = "🟡 Some track record"
        risk_note = f"Best previous launch peaked at {fmt_mc(biggest_mc)}."
    else:
        risk = "🟠 Low track record"
        risk_note = "No significant previous launches found."

    # Build token list string (top 5)
    token_lines = []
    for i, t in enumerate(tokens[:5], 1):
        mc_str = fmt_mc(t["market_cap"]) if t["market_cap"] > 0 else "Dead/No data"
        token_lines.append(f"{i}. {t['name']} (${t['symbol']}) — {mc_str}")

    summary = (
        f"This dev has launched {total} token(s) in the last 60 days. "
    )
    if biggest:
        summary += f"Their biggest success was {biggest['name']} (${biggest['symbol']}) which hit {fmt_mc(biggest_mc)}."
    else:
        summary += "None of their previous tokens are currently trading on DEX."

    return {
        "deployer": deployer,
        "token_count": total,
        "tokens": tokens,
        "token_lines": token_lines,
        "biggest_launch": biggest,
        "biggest_mc": biggest_mc,
        "dead_count": len(dead),
        "risk": risk,
        "risk_note": risk_note,
        "summary": summary,
    }




