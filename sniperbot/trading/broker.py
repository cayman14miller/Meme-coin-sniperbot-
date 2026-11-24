"""Order execution.

PaperBroker  - simulated fills with fees + slippage, fake SOL balance (default).
JupiterBroker - real swaps SOL<->token through the Jupiter aggregator, signed locally.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from dataclasses import dataclass

import httpx

from ..data.market import SOL_MINT
from ..store import Store

log = logging.getLogger(__name__)
LAMPORTS = 1_000_000_000


@dataclass
class Fill:
    qty: float          # tokens bought / sold (UI units)
    sol: float          # SOL spent (buy) or received (sell)
    price_usd: float    # effective price per token in USD
    tx: str = ""


class BrokerError(RuntimeError):
    pass


class Broker:
    mode = "base"

    async def balance_sol(self) -> float:
        raise NotImplementedError

    async def buy(self, mint: str, sol_amount: float, price_usd: float, sol_usd: float, slippage_bps: int,
                  priority_fee: int) -> Fill:
        raise NotImplementedError

    async def sell(self, mint: str, qty: float, price_usd: float, sol_usd: float, slippage_bps: int,
                   priority_fee: int, sell_all: bool = False) -> Fill:
        raise NotImplementedError

    async def token_balance(self, mint: str) -> float | None:
        """Tokens actually held (UI units), or None when the broker cannot know (paper)."""
        return None

    async def close(self) -> None:
        pass


# Per-swap pool fee by DEX, from live Jupiter round-trip quotes (research/cost_probe.py, Nov 2026):
# Pump.fun AMM pools of small-cap coins cost ~0.9-1% per side; Meteora/Raydium pools far less.
POOL_FEE = {"pumpswap": 0.0095, "pumpfun": 0.0125, "meteora": 0.003, "raydium": 0.0025, "orca": 0.003}
DEFAULT_POOL_FEE = 0.0095   # unknown DEX: assume the expensive case
BASE_NETWORK_LAMPORTS = 5000


class PaperBroker(Broker):
    """Simulated fills that cost what real swaps cost.

    fill_model "quote": price each paper trade with a live Jupiter quote (actual route, pool fees, price
    impact; no route = failed trade, as live). If the quote service is unreachable, falls back to "model".
    fill_model "model": pool fee by DEX + constant-product price impact against the SOL side of the pool
    (calibrated on live quotes). Network fees are always charged. `fail_rate` randomly fails transactions
    (fee still paid) for stress tests; default 0 because no measured live rate exists yet.
    """

    mode = "paper"

    def __init__(self, store: Store, starting_sol: float, market=None, fill_model: str = "model",
                 fail_rate: float = 0.0, rng=None) -> None:
        import random

        self.store = store
        if self.store.get("paper_sol") is None:
            self.store.set("paper_sol", starting_sol)
        self.market = market
        self.fill_model = fill_model
        self.fail_rate = fail_rate
        self.rng = rng or random.Random()
        self.liquidity: dict[str, float] = {}
        self.dex: dict[str, str] = {}
        self.last_fill_source = ""

    def reset(self, starting_sol: float) -> None:
        self.store.set("paper_sol", starting_sol)

    async def balance_sol(self) -> float:
        return float(self.store.get("paper_sol", 0.0))

    def _network_fee(self, priority_fee: int) -> float:
        return (BASE_NETWORK_LAMPORTS + priority_fee) / LAMPORTS

    def _model_cost(self, mint: str, usd: float) -> float:
        """Fraction lost on one swap: pool fee + price impact vs the SOL side (~half the quoted liquidity)."""
        fee = POOL_FEE.get(self.dex.get(mint, ""), DEFAULT_POOL_FEE)
        liq = self.liquidity.get(mint, 0)
        impact = usd / (liq / 2) if liq > 0 else 0.02
        return min(fee + impact, 0.9)

    def _maybe_fail(self, priority_fee: int) -> None:
        if self.fail_rate and self.rng.random() < self.fail_rate:
            bal = float(self.store.get("paper_sol", 0.0))
            self.store.set("paper_sol", bal - self._network_fee(priority_fee))
            raise BrokerError("simulated transaction failure (fee paid)")

    async def _quote(self, inp: str, out: str, amount: int, slippage_bps: int):
        """('ok', outAmount) | ('no_route', msg) | ('unavailable', msg)."""
        if self.fill_model != "quote" or self.market is None:
            return "unavailable", "quote mode off"
        try:
            return await self.market.jup_quote(inp, out, amount, slippage_bps)
        except Exception as exc:
            return "unavailable", str(exc)

    async def _decimals(self, mint: str) -> int | None:
        try:
            return await self.market.token_decimals(mint) if self.market is not None else None
        except Exception:
            return None

    async def buy(self, mint, sol_amount, price_usd, sol_usd, slippage_bps, priority_fee) -> Fill:
        bal = await self.balance_sol()
        net_fee = self._network_fee(priority_fee)
        if sol_amount + net_fee > bal:
            raise BrokerError(f"insufficient paper balance: {bal:.4f} SOL")
        if price_usd <= 0 or sol_usd <= 0:
            raise BrokerError("no price")
        self._maybe_fail(priority_fee)
        qty = None
        status, val = await self._quote(SOL_MINT, mint, int(sol_amount * LAMPORTS), slippage_bps)
        if status == "no_route":
            raise BrokerError(f"no route to buy (paper quote): {val}")
        if status == "ok":
            dec = await self._decimals(mint)
            if dec is not None:
                qty = int(val) / 10 ** dec
                self.last_fill_source = "quote"
        if qty is None:
            cost = self._model_cost(mint, sol_amount * sol_usd)
            if cost * 10_000 > slippage_bps + POOL_FEE.get(self.dex.get(mint, ""), DEFAULT_POOL_FEE) * 10_000:
                raise BrokerError(f"price impact {cost:.1%} exceeds max slippage {slippage_bps / 100:.1f}%")
            qty = sol_amount * sol_usd * (1 - cost) / price_usd
            self.last_fill_source = "model"
        if qty <= 0:
            raise BrokerError("quote returned no tokens")
        self.store.set("paper_sol", bal - sol_amount - net_fee)
        return Fill(qty=qty, sol=sol_amount, price_usd=sol_amount * sol_usd / qty,
                    tx=f"paper-{self.last_fill_source}-{int(time.time() * 1000)}")

    async def sell(self, mint, qty, price_usd, sol_usd, slippage_bps, priority_fee, sell_all=False) -> Fill:
        if price_usd <= 0 or sol_usd <= 0:
            raise BrokerError("no price")
        self._maybe_fail(priority_fee)
        sol = None
        dec = await self._decimals(mint) if self.fill_model == "quote" else None
        if dec is not None:
            status, val = await self._quote(mint, SOL_MINT, int(qty * 10 ** dec), slippage_bps)
            if status == "no_route":
                raise BrokerError(f"no route to sell (paper quote): {val}")
            if status == "ok":
                sol = int(val) / LAMPORTS
                self.last_fill_source = "quote"
        if sol is None:
            value = qty * price_usd
            liq = self.liquidity.get(mint)
            if liq is not None:
                # constant-product payout from the SOL side (~half the USD liquidity): ~fee+impact in a normal
                # pool, near zero in a drained one whatever the stale displayed price says
                y = max(liq, 0.0) / 2
                fee = POOL_FEE.get(self.dex.get(mint, ""), DEFAULT_POOL_FEE)
                out_usd = value * (1 - fee) * y / (y + value) if y > 0 else 0.0
            else:
                out_usd = value * (1 - self._model_cost(mint, value))
            sol = out_usd / sol_usd
            self.last_fill_source = "model"
        sol = max(sol - self._network_fee(priority_fee), 0.0)
        bal = await self.balance_sol()
        self.store.set("paper_sol", bal + sol)
        return Fill(qty=qty, sol=sol, price_usd=(sol * sol_usd / qty) if qty else 0.0,
                    tx=f"paper-{self.last_fill_source}-{int(time.time() * 1000)}")


class JupiterBroker(Broker):
    """Live trading via Jupiter swap API. Requires `solders`."""

    mode = "live"

    def __init__(self, private_key: str, rpc_url: str, api_key: str = "") -> None:
        try:
            from solders.keypair import Keypair  # noqa: F401
        except ImportError as exc:  # pragma: no cover
            raise BrokerError("pip install solders base58 for live trading") from exc
        from solders.keypair import Keypair

        key = private_key.strip()
        if key.startswith("["):
            self.keypair = Keypair.from_bytes(bytes(json.loads(key)))
        else:
            self.keypair = Keypair.from_base58_string(key)
        self.pubkey = str(self.keypair.pubkey())
        self.rpc_url = rpc_url
        if api_key:
            self.jup = "https://api.jup.ag/swap/v1"
            headers = {"x-api-key": api_key}
        else:
            self.jup = "https://lite-api.jup.ag/swap/v1"
            headers = {}
        self.client = httpx.AsyncClient(timeout=30, headers=headers)
        self._decimals: dict[str, int] = {}

    async def close(self) -> None:
        await self.client.aclose()

    # -- RPC ------------------------------------------------------------------
    async def _rpc(self, method: str, params: list) -> dict:
        r = await self.client.post(self.rpc_url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            raise BrokerError(f"RPC {method}: {data['error']}")
        return data["result"]

    async def balance_sol(self) -> float:
        res = await self._rpc("getBalance", [self.pubkey, {"commitment": "confirmed"}])
        return res["value"] / LAMPORTS

    async def decimals(self, mint: str) -> int:
        if mint not in self._decimals:
            res = await self._rpc("getTokenSupply", [mint])
            self._decimals[mint] = int(res["value"]["decimals"])
        return self._decimals[mint]

    async def token_balance_raw(self, mint: str) -> int:
        res = await self._rpc("getTokenAccountsByOwner",
                              [self.pubkey, {"mint": mint}, {"encoding": "jsonParsed", "commitment": "confirmed"}])
        total = 0
        for acc in res.get("value", []):
            total += int(acc["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"])
        return total

    async def token_balance(self, mint: str) -> float | None:
        raw = await self.token_balance_raw(mint)
        return raw / 10 ** await self.decimals(mint)

    # -- swap -----------------------------------------------------------------
    async def _swap(self, input_mint: str, output_mint: str, amount_raw: int, slippage_bps: int,
                    priority_fee: int) -> tuple[dict, str]:
        from solders.transaction import VersionedTransaction

        q = await self.client.get(f"{self.jup}/quote", params={
            "inputMint": input_mint, "outputMint": output_mint, "amount": str(amount_raw),
            "slippageBps": slippage_bps, "restrictIntermediateTokens": "true"})
        if q.status_code != 200:
            raise BrokerError(f"quote failed: {q.text[:200]}")
        quote = q.json()
        s = await self.client.post(f"{self.jup}/swap", json={
            "quoteResponse": quote, "userPublicKey": self.pubkey, "wrapAndUnwrapSol": True,
            "dynamicComputeUnitLimit": True, "dynamicSlippage": False,
            "prioritizationFeeLamports": {"priorityLevelWithMaxLamports": {
                "maxLamports": priority_fee, "priorityLevel": "veryHigh"}}})
        if s.status_code != 200:
            raise BrokerError(f"swap build failed: {s.text[:200]}")
        raw_tx = VersionedTransaction.from_bytes(base64.b64decode(s.json()["swapTransaction"]))
        signed = VersionedTransaction(raw_tx.message, [self.keypair])
        sig = await self._rpc("sendTransaction", [base64.b64encode(bytes(signed)).decode(),
                                                  {"encoding": "base64", "skipPreflight": True, "maxRetries": 3}])
        await self._confirm(sig)
        return quote, sig

    async def _confirm(self, sig: str, timeout: float = 60) -> None:
        start = time.time()
        while time.time() - start < timeout:
            res = await self._rpc("getSignatureStatuses", [[sig], {"searchTransactionHistory": False}])
            st = res["value"][0]
            if st:
                if st.get("err"):
                    raise BrokerError(f"transaction failed: {st['err']} ({sig})")
                if st.get("confirmationStatus") in ("confirmed", "finalized"):
                    return
            await asyncio.sleep(2)
        raise BrokerError(f"transaction not confirmed in {timeout}s ({sig})")

    async def buy(self, mint, sol_amount, price_usd, sol_usd, slippage_bps, priority_fee) -> Fill:
        bal = await self.balance_sol()
        if sol_amount + 0.01 > bal:  # keep 0.01 SOL for fees/rent
            raise BrokerError(f"insufficient SOL: {bal:.4f}")
        dec = await self.decimals(mint)
        before = await self.token_balance_raw(mint)
        quote, sig = await self._swap(SOL_MINT, mint, int(sol_amount * LAMPORTS), slippage_bps, priority_fee)
        after = await self.token_balance_raw(mint)
        got_raw = (after - before) or int(quote["outAmount"])
        qty = got_raw / 10 ** dec
        eff_price = (sol_amount * sol_usd / qty) if qty else price_usd
        return Fill(qty=qty, sol=sol_amount, price_usd=eff_price, tx=sig)

    async def sell(self, mint, qty, price_usd, sol_usd, slippage_bps, priority_fee, sell_all=False) -> Fill:
        dec = await self.decimals(mint)
        held = await self.token_balance_raw(mint)
        amount = held if sell_all else min(int(qty * 10 ** dec), held)
        if amount <= 0:
            raise BrokerError("no token balance to sell")
        sol_before = await self.balance_sol()
        quote, sig = await self._swap(mint, SOL_MINT, amount, slippage_bps, priority_fee)
        sol_after = await self.balance_sol()
        received = sol_after - sol_before
        if received <= 0:
            received = int(quote["outAmount"]) / LAMPORTS
        sold = amount / 10 ** dec
        eff_price = (received * sol_usd / sold) if sold else price_usd
        return Fill(qty=sold, sol=received, price_usd=eff_price, tx=sig)
