"""
smartmoney.py — Smart Money Wallet Finder for Chain Sentinel
Finds wallets that appear across multiple successful tokens, filters by PnL & win rate.
"""

import os
import asyncio
import aiohttp
import json
import time
import logging
from collections import defaultdict

logger = logging.getLogger(__name__)

HELIUS_API_KEY = os.environ.get("HELIUS_API_KEY", "")
HELIUS_RPC     = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
HELIUS_API     = "https://api.helius.xyz/v0"

SOL_MINT = "So11111111111111111111111111111111111111112"


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN ENTRY
# ═══════════════════════════════════════════════════════════════════════════════

async def find_smart_money(mints: list, min_win_rate: float = 0.60, min_pnl_sol: float = 10.0) -> dict:
    """
    Given a list of 2-5 mint addresses, find smart money wallets.
    Returns a report dict.
    """
    if len(mints) < 2:
        return {"error": "Please provide at least 2 contract addresses."}
    if len(mints) > 5:
        mints = mints[:5]

    async with aiohttp.ClientSession() as session:

        # ── Step 1: Get top holders for each mint ─────────────────────────────
        logger.info(f"[SM] Fetching holders for {len(mints)} tokens...")
        holder_lists = await asyncio.gather(*[
            get_top_holders(session, mint) for mint in mints
        ])

        # ── Step 2: Find intersection (wallets in 2+ lists) ───────────────────
        common = find_intersection(holder_lists, mints, min_appearances=2)
        logger.info(f"[SM] Found {len(common)} common wallets")

        if not common:
            return {
                "error": "No common wallets found across these tokens. Try tokens with more overlap.",
                "holder_counts": [len(h) for h in holder_lists],
            }

        # Limit to top 30 wallets to avoid rate limits
        common_wallets = list(common.keys())[:30]

        # ── Step 3: Calculate PnL for each common wallet ───────────────────────
        logger.info(f"[SM] Calculating PnL for {len(common_wallets)} wallets...")
        pnl_results = await asyncio.gather(*[
            calculate_wallet_pnl(session, wallet, mints) for wallet in common_wallets
        ])

        # ── Step 4: Filter by win rate and PnL ────────────────────────────────
        qualified = []
        for wallet, pnl_data in zip(common_wallets, pnl_results):
            if pnl_data is None:
                continue
            win_rate = pnl_data.get("win_rate", 0)
            total_pnl = pnl_data.get("total_pnl_sol", 0)
            if win_rate >= min_win_rate and total_pnl >= min_pnl_sol:
                pnl_data["wallet"] = wallet
                pnl_data["tokens_held"] = common[wallet]
                qualified.append(pnl_data)

        # Sort by total PnL descending
        qualified.sort(key=lambda x: x.get("total_pnl_sol", 0), reverse=True)
        logger.info(f"[SM] {len(qualified)} wallets passed filters")

        return {
            "mints_analyzed": mints,
            "total_common_wallets": len(common),
            "qualified_wallets": qualified[:10],  # top 10
            "min_win_rate": min_win_rate,
            "min_pnl_sol": min_pnl_sol,
        }


# ═══════════════════════════════════════════════════════════════════════════════
# STEP 1 — GET TOP HOLDERS
# ═══════════════════════════════════════════════════════════════════════════════

async def get_top_holders(session: aiohttp.ClientSession, mint: str) -> list:
    """Returns list of top 100 holder wallet addresses for a mint."""
    try:
        # Use getTokenLargestAccounts to get top 20 (Solana RPC limit)
        payload = {
            "jsonrpc": "2.0", "id": 1,
            "method": "getTokenLargestAccounts",
            "params": [mint]
        }
        async with session.post(HELIUS_RPC, json=payload, timeout=aiohttp.ClientTimeout(total=12)) as resp:
            data = await resp.json()

        accounts = data.get("result", {}).get("value", [])
        token_account_addresses = [acc["address"] for acc in accounts if acc.get("address")]

        if not token_account_addresses:
            return []

        # Convert token accounts to owner wallets
        owners = await get_account_owners(session, token_account_addresses)
        logger.info(f"[SM] {mint[:8]}... has {len(owners)} holders")
        return owners

    except Exception as e:
        logger.error(f"[SM] get_top_holders error for {mint[:8]}: {e}")
        return []


async def get_account_owners(session: aiohttp.ClientSession, token_accounts: list) -> list:
    """Batch convert token accounts to owner wallet addresses."""
    owners = []
    try:
        # getMultipleAccounts in one RPC call
        payload = {
            "jsonrpc": "2.0", "id": 1,
            "method": "getMultipleAccounts",
            "params": [token_accounts, {"encoding": "jsonParsed"}]
        }
        async with session.post(HELIUS_RPC, json=payload, timeout=aiohttp.ClientTimeout(total=12)) as resp:
            data = await resp.json()

        for account in data.get("result", {}).get("value", []) or []:
            if not account:
                continue
            parsed = account.get("data", {}).get("parsed", {})
            owner = parsed.get("info", {}).get("owner", "")
            if owner:
                owners.append(owner)
    except Exception as e:
        logger.error(f"[SM] get_account_owners error: {e}")
    return owners


# ═══════════════════════════════════════════════════════════════════════════════
# STEP 2 — FIND INTERSECTION
# ═══════════════════════════════════════════════════════════════════════════════

def find_intersection(holder_lists: list, mints: list, min_appearances: int = 2) -> dict:
    """
    Find wallets appearing in at least min_appearances token holder lists.
    Returns dict: {wallet: [list of mints they hold]}
    """
    wallet_to_mints = defaultdict(list)

    for mint, holders in zip(mints, holder_lists):
        for wallet in holders:
            # Exclude known program addresses
            if len(wallet) < 32 or wallet in EXCLUDED_PROGRAMS:
                continue
            wallet_to_mints[wallet].append(mint)

    # Keep only wallets in 2+ lists
    common = {
        wallet: held_mints
        for wallet, held_mints in wallet_to_mints.items()
        if len(held_mints) >= min_appearances
    }

    # Sort by how many tokens they hold (most overlap first)
    return dict(sorted(common.items(), key=lambda x: len(x[1]), reverse=True))


# Known program addresses to exclude
EXCLUDED_PROGRAMS = {
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "11111111111111111111111111111111",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJe1bfE",
    "So11111111111111111111111111111111111111112",
}


# ═══════════════════════════════════════════════════════════════════════════════
# STEP 3 — CALCULATE WALLET PNL
# ═══════════════════════════════════════════════════════════════════════════════

async def calculate_wallet_pnl(session: aiohttp.ClientSession, wallet: str, mints: list) -> dict:
    """
    Fetch last 50 transactions for a wallet and calculate realized PnL.
    """
    try:
        # Get recent transactions via Helius enhanced API
        url = f"{HELIUS_API}/addresses/{wallet}/transactions?api-key={HELIUS_API_KEY}&limit=50"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=12)) as resp:
            if resp.status != 200:
                return None
            txs = await resp.json()

        if not isinstance(txs, list) or not txs:
            return None

        trades = []
        token_pnl = defaultdict(lambda: {"buy_sol": 0.0, "sell_sol": 0.0, "buy_amount": 0.0, "sell_amount": 0.0})
        current_holdings = {}

        for tx in txs:
            tx_type = tx.get("type", "")
            ts = tx.get("timestamp", 0)

            # Get SOL change for this wallet
            sol_change = 0.0
            for change in tx.get("accountData", []):
                if change.get("account") == wallet:
                    sol_change = change.get("nativeBalanceChange", 0) / 1e9  # lamports to SOL

            # Analyze token transfers
            for transfer in tx.get("tokenTransfers", []):
                mint = transfer.get("mint", "")
                from_addr = transfer.get("fromUserAccount", "")
                to_addr   = transfer.get("toUserAccount", "")
                amount    = float(transfer.get("tokenAmount", 0))

                if not mint or amount == 0:
                    continue

                if to_addr == wallet and sol_change < 0:
                    # BUY: wallet received tokens and spent SOL
                    cost_sol = abs(sol_change)
                    token_pnl[mint]["buy_sol"]    += cost_sol
                    token_pnl[mint]["buy_amount"] += amount
                    trades.append({"type": "BUY", "mint": mint, "sol": cost_sol, "amount": amount, "ts": ts})
                    current_holdings[mint] = current_holdings.get(mint, 0) + amount

                elif from_addr == wallet and sol_change > 0:
                    # SELL: wallet sent tokens and received SOL
                    recv_sol = sol_change
                    token_pnl[mint]["sell_sol"]    += recv_sol
                    token_pnl[mint]["sell_amount"] += amount
                    trades.append({"type": "SELL", "mint": mint, "sol": recv_sol, "amount": amount, "ts": ts})
                    current_holdings[mint] = max(0, current_holdings.get(mint, 0) - amount)

        if not trades:
            return None

        # Calculate per-token PnL
        token_reports = []
        wins = 0
        total_pnl = 0.0

        for mint, data in token_pnl.items():
            bought  = data["buy_sol"]
            sold    = data["sell_sol"]
            pnl     = sold - bought
            total_pnl += pnl

            if bought > 0:
                roi_pct = ((sold - bought) / bought * 100) if bought > 0 else 0
                is_win  = pnl > 0
                if is_win:
                    wins += 1
                token_reports.append({
                    "mint":        mint,
                    "bought_sol":  round(bought, 4),
                    "sold_sol":    round(sold, 4),
                    "pnl_sol":     round(pnl, 4),
                    "roi_pct":     round(roi_pct, 1),
                    "holding":     round(current_holdings.get(mint, 0), 2),
                    "win":         is_win,
                })

        token_reports.sort(key=lambda x: x["pnl_sol"], reverse=True)

        total_trades = len(token_pnl)
        win_rate = wins / total_trades if total_trades > 0 else 0

        return {
            "total_pnl_sol":   round(total_pnl, 4),
            "win_rate":        round(win_rate, 3),
            "wins":            wins,
            "total_positions": total_trades,
            "token_breakdown": token_reports,
            "current_holdings": {k: v for k, v in current_holdings.items() if v > 0},
            "trade_count":     len(trades),
        }

    except Exception as e:
        logger.error(f"[SM] PnL error for {wallet[:8]}: {e}")
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# REPORT FORMATTER
# ═══════════════════════════════════════════════════════════════════════════════

def format_smart_money_report(result: dict) -> str:
    if result.get("error"):
        return f"❌ {result['error']}"

    qualified = result.get("qualified_wallets", [])
    total_common = result.get("total_common_wallets", 0)
    mints = result.get("mints_analyzed", [])
    min_wr = int(result.get("min_win_rate", 0.6) * 100)
    min_pnl = result.get("min_pnl_sol", 10)

    lines = [
        f"🧠 *SMART MONEY REPORT*",
        f"",
        f"Tokens analysed: `{len(mints)}`",
        f"Common wallets found: `{total_common}`",
        f"100% Win Rate wallets: `{len(qualified)}`",
        f"",
    ]

    if not qualified:
        lines.append("No wallets passed the filters. Try lowering thresholds or using more popular tokens.")
        return "\n".join(lines)

    for i, w in enumerate(qualified[:5], 1):
        wallet = w.get("wallet", "")
        pnl    = w.get("total_pnl_sol", 0)
        wr     = w.get("win_rate", 0)
        wins   = w.get("wins", 0)
        total  = w.get("total_positions", 0)
        trades = w.get("trade_count", 0)
        tokens_held = w.get("tokens_held", [])

        pnl_emoji = "🟢" if pnl > 0 else "🔴"

        lines += [
            f"━━━ 💼 WALLET #{i} ━━━",
            f"• Address: `{wallet[:8]}...{wallet[-4:]}`",
            f"  [Solscan](https://solscan.io/account/{wallet})",
            f"• Total PnL: {pnl_emoji} `{pnl:+.2f} SOL`",
            f"• Win Rate: `{wr*100:.0f}%` ({wins}/{total} positions)",
            f"• Trades analysed: `{trades}`",
            f"• Holds {len(tokens_held)} of your tokens",
            f"",
        ]

        # Top 3 token PnLs
        breakdown = w.get("token_breakdown", [])[:3]
        if breakdown:
            lines.append("  *Top positions:*")
            for t in breakdown:
                emoji = "✅" if t["win"] else "❌"
                lines.append(
                    f"  {emoji} `{t['mint'][:8]}...` "
                    f"Buy: {t['bought_sol']} SOL → "
                    f"Sell: {t['sold_sol']} SOL "
                    f"({t['roi_pct']:+.0f}%)"
                )
            lines.append("")

        # Current holdings
        holdings = w.get("current_holdings", {})
        if holdings:
            held_mints = list(holdings.keys())[:3]
            lines.append(f"  *Still holding:* {', '.join([f'`{m[:8]}...`' for m in held_mints])}")
            lines.append("")

    lines.append("_Powered by Chain Sentinel • $CS_")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════════
# STANDALONE SCRIPT MODE
# ═══════════════════════════════════════════════════════════════════════════════

async def main_standalone(mints: list):
    print(f"\n🧠 Chain Sentinel — Smart Money Finder")
    print(f"Analysing {len(mints)} tokens...\n")

    result = await find_smart_money(mints, min_win_rate=0.60, min_pnl_sol=10.0)

    if result.get("error"):
        print(f"Error: {result['error']}")
        return

    print(format_smart_money_report(result))

    # Also save full JSON report
    with open("smart_money_report.json", "w") as f:
        json.dump(result, f, indent=2, default=str)
    print("\n✅ Full report saved to smart_money_report.json")


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)

    if len(sys.argv) < 3:
        print("Usage: python smartmoney.py <mint1> <mint2> [mint3] [mint4] [mint5]")
        print("Example: python smartmoney.py ABC123... DEF456... GHI789...")
        sys.exit(1)

    input_mints = sys.argv[1:]
    asyncio.run(main_standalone(input_mints))
