"""Market data: token discovery, live pair stats, candles, safety reports, SOL price.

Free public APIs used (no key needed):
  * DexScreener   - new token profiles, boosts, pair stats (price/liquidity/volume/txns)
  * GeckoTerminal - newly created pools, trending pools, OHLCV candles
  * RugCheck      - token safety report (mint/freeze authority, LP, holder concentration)
  * Jupiter       - SOL/USD price
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger(__name__)

SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qFqTJ2fQWWWNMybqsT3m4zDgdx"
STABLE_OR_BASE = {SOL_MINT, USDC_MINT, "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"}

DEXSCREENER = "https://api.dexscreener.com"
GECKO = "https://api.geckoterminal.com/api/v2"
RUGCHECK = "https://api.rugcheck.xyz/v1"
JUP_PRICE = "https://lite-api.jup.ag/price/v3"
JUP_TOKENS = "https://lite-api.jup.ag/tokens/v2/search"
JUP_QUOTE = "https://lite-api.jup.ag/swap/v1/quote"


@dataclass
class Candle:
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class PairInfo:
    mint: str
    symbol: str
    name: str
    pair_address: str
    dex: str
    price_usd: float
    price_native: float
    liquidity_usd: float
    fdv: float
    market_cap: float
    created_at: float  # unix seconds
    volume: dict[str, float] = field(default_factory=dict)
    price_change: dict[str, float] = field(default_factory=dict)
    buys: dict[str, int] = field(default_factory=dict)
    sells: dict[str, int] = field(default_factory=dict)
    url: str = ""
    boosted: bool = False
    has_socials: bool = False

    @property
    def age_minutes(self) -> float:
        return (time.time() - self.created_at) / 60 if self.created_at else 1e9

    def buy_sell_ratio(self, window: str = "m5") -> float:
        b, s = self.buys.get(window, 0), self.sells.get(window, 0)
        return b / s if s else float(b or 0)

    @classmethod
    def from_dexscreener(cls, p: dict[str, Any]) -> "PairInfo":
        def fnum(x: Any) -> float:
            try:
                return float(x)
            except (TypeError, ValueError):
                return 0.0

        txns = p.get("txns") or {}
        info = p.get("info") or {}
        return cls(
            mint=p["baseToken"]["address"],
            symbol=p["baseToken"].get("symbol", "?"),
            name=p["baseToken"].get("name", ""),
            pair_address=p.get("pairAddress", ""),
            dex=p.get("dexId", ""),
            price_usd=fnum(p.get("priceUsd")),
            price_native=fnum(p.get("priceNative")),
            liquidity_usd=fnum((p.get("liquidity") or {}).get("usd")),
            fdv=fnum(p.get("fdv")),
            market_cap=fnum(p.get("marketCap")),
            created_at=fnum(p.get("pairCreatedAt")) / 1000,
            volume={k: fnum(v) for k, v in (p.get("volume") or {}).items()},
            price_change={k: fnum(v) for k, v in (p.get("priceChange") or {}).items()},
            buys={k: int(v.get("buys", 0)) for k, v in txns.items()},
            sells={k: int(v.get("sells", 0)) for k, v in txns.items()},
            url=p.get("url", ""),
            boosted=bool((p.get("boosts") or {}).get("active")),
            has_socials=bool(info.get("socials") or info.get("websites")),
        )


@dataclass
class SafetyReport:
    ok: bool
    score: int
    risks: list[str]
    reason: str = ""


class RateLimiter:
    def __init__(self, per_minute: int) -> None:
        self.interval = 60.0 / per_minute
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            if self._next > now:
                await asyncio.sleep(self._next - now)
            self._next = max(now, self._next) + self.interval


class MarketData:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self.client = client or httpx.AsyncClient(timeout=15, headers={"Accept": "application/json",
                                                                         "User-Agent": "sniperbot/1.0"})
        self.dex_limit = RateLimiter(250)   # DexScreener: 300/min on pair endpoints
        self.gecko_limit = RateLimiter(25)  # GeckoTerminal free: ~30/min
        self.rug_limit = RateLimiter(50)
        self._candle_cache: dict[str, tuple[float, list[Candle]]] = {}
        self._safety_cache: dict[str, tuple[float, SafetyReport]] = {}
        self._sol_price: tuple[float, float] = (0.0, 0.0)
        self._decimals: dict[str, int] = {}

    async def close(self) -> None:
        await self.client.aclose()

    async def _get(self, url: str, limiter: RateLimiter, params: dict | None = None) -> Any:
        await limiter.wait()
        for attempt in range(3):
            try:
                r = await self.client.get(url, params=params)
                if r.status_code == 429:
                    await asyncio.sleep(2 + attempt * 3)
                    continue
                r.raise_for_status()
                return r.json()
            except (httpx.HTTPError, ValueError) as exc:
                if attempt == 2:
                    log.warning("GET %s failed: %s", url, exc)
                    return None
                await asyncio.sleep(1 + attempt)
        return None

    # -- discovery ---------------------------------------------------------
    async def discover_mints(self) -> list[str]:
        """Collect fresh/trending Solana token mints from several feeds."""
        results = await asyncio.gather(
            self._get(f"{DEXSCREENER}/token-profiles/latest/v1", self.dex_limit),
            self._get(f"{DEXSCREENER}/token-boosts/latest/v1", self.dex_limit),
            self._get(f"{DEXSCREENER}/token-boosts/top/v1", self.dex_limit),
            self._get(f"{GECKO}/networks/solana/new_pools", self.gecko_limit, {"page": 1}),
            self._get(f"{GECKO}/networks/solana/trending_pools", self.gecko_limit, {"page": 1, "duration": "1h"}),
        )
        mints: list[str] = []
        for feed in results[:3]:
            for item in feed or []:
                if isinstance(item, dict) and item.get("chainId") == "solana" and item.get("tokenAddress"):
                    mints.append(item["tokenAddress"])
        for feed in results[3:]:
            for pool in (feed or {}).get("data", []) if isinstance(feed, dict) else []:
                try:
                    tok = pool["relationships"]["base_token"]["data"]["id"]
                    mints.append(tok.split("_", 1)[1])
                except (KeyError, IndexError, TypeError):
                    continue
        seen: set[str] = set()
        out = []
        for m in mints:
            if m not in seen and m not in STABLE_OR_BASE:
                seen.add(m)
                out.append(m)
        return out

    async def pairs_for(self, mints: list[str]) -> dict[str, PairInfo]:
        """Best (highest-liquidity) SOL/USDC-quoted pair per mint. Batches of 30."""
        best: dict[str, PairInfo] = {}
        for i in range(0, len(mints), 30):
            chunk = mints[i:i + 30]
            data = await self._get(f"{DEXSCREENER}/tokens/v1/solana/{','.join(chunk)}", self.dex_limit)
            if isinstance(data, dict):
                data = data.get("pairs") or []
            for raw in data or []:
                try:
                    if raw.get("chainId") != "solana":
                        continue
                    if (raw.get("quoteToken") or {}).get("address") not in (SOL_MINT, USDC_MINT):
                        continue
                    p = PairInfo.from_dexscreener(raw)
                except (KeyError, TypeError):
                    continue
                if p.mint in chunk and (p.mint not in best or p.liquidity_usd > best[p.mint].liquidity_usd):
                    best[p.mint] = p
        return best

    # -- candles -------------------------------------------------------------
    async def candles(self, pool: str, timeframe: str = "minute", aggregate: int = 1,
                      limit: int = 120, max_age: float = 20) -> list[Candle]:
        """OHLCV candles oldest->newest. Cached for ``max_age`` seconds."""
        key = f"{pool}:{timeframe}:{aggregate}"
        cached = self._candle_cache.get(key)
        if cached and time.time() - cached[0] < max_age:
            return cached[1]
        data = await self._get(f"{GECKO}/networks/solana/pools/{pool}/ohlcv/{timeframe}", self.gecko_limit,
                               {"aggregate": aggregate, "limit": limit, "currency": "usd"})
        try:
            raw = data["data"]["attributes"]["ohlcv_list"]
        except (KeyError, TypeError):
            return cached[1] if cached else []
        out = sorted((Candle(int(c[0]), float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5]))
                      for c in raw), key=lambda c: c.ts)
        self._candle_cache[key] = (time.time(), out)
        if len(self._candle_cache) > 500:
            oldest = sorted(self._candle_cache.items(), key=lambda kv: kv[1][0])[:100]
            for k, _ in oldest:
                self._candle_cache.pop(k, None)
        return out

    # -- safety ----------------------------------------------------------------
    async def safety(self, mint: str, max_score: int) -> SafetyReport:
        cached = self._safety_cache.get(mint)
        if cached and time.time() - cached[0] < 600:
            rep = cached[1]
            return SafetyReport(rep.ok and rep.score <= max_score, rep.score, rep.risks, rep.reason)
        data = await self._get(f"{RUGCHECK}/tokens/{mint}/report/summary", self.rug_limit)
        if not isinstance(data, dict):
            return SafetyReport(False, -1, [], "safety check unavailable")
        risks = data.get("risks") or []
        danger = [r.get("name", "?") for r in risks if r.get("level") == "danger"]
        names = [f"{r.get('name')} ({r.get('level')})" for r in risks]
        score = int(data.get("score") or 0)
        ok, reason = True, ""
        if danger:
            ok, reason = False, "danger: " + ", ".join(danger)
        elif score > max_score:
            ok, reason = False, f"risk score {score} > {max_score}"
        rep = SafetyReport(ok, score, names, reason)
        self._safety_cache[mint] = (time.time(), SafetyReport(not danger, score, names, reason))
        return rep

    async def jup_quote(self, input_mint: str, output_mint: str, amount: int, slippage_bps: int):
        """Live Jupiter quote (read-only). Returns ('ok', outAmount) | ('no_route', msg) | ('unavailable', msg)."""
        await self.dex_limit.wait()
        try:
            r = await self.client.get(JUP_QUOTE, params={"inputMint": input_mint, "outputMint": output_mint,
                                                         "amount": str(amount), "slippageBps": slippage_bps,
                                                         "restrictIntermediateTokens": "true"})
        except httpx.HTTPError as exc:
            return "unavailable", str(exc)
        if r.status_code == 200:
            out = (r.json() or {}).get("outAmount")
            return ("ok", int(out)) if out else ("no_route", "empty quote")
        if r.status_code in (400, 404):  # e.g. NO_ROUTES_FOUND / TOKEN_NOT_TRADABLE
            return "no_route", r.text[:160]
        return "unavailable", f"HTTP {r.status_code}"

    async def token_decimals(self, mint: str) -> int | None:
        if mint not in self._decimals:
            data = await self._get(JUP_TOKENS, self.dex_limit, {"query": mint})
            for d in data or []:
                if isinstance(d, dict) and d.get("id") == mint and d.get("decimals") is not None:
                    self._decimals[mint] = int(d["decimals"])
        return self._decimals.get(mint)

    async def jup_prices(self, mints: list[str]) -> dict[str, float]:
        """Fallback USD prices from Jupiter (any mint), used when DexScreener has no price for a holding."""
        out: dict[str, float] = {}
        for i in range(0, len(mints), 50):
            data = await self._get(JUP_PRICE, self.dex_limit, {"ids": ",".join(mints[i:i + 50])})
            for m in mints[i:i + 50]:
                try:
                    px = float(data[m]["usdPrice"])
                    if px > 0:
                        out[m] = px
                except (KeyError, TypeError, ValueError):
                    continue
        return out

    # -- SOL price -------------------------------------------------------------
    async def sol_price(self) -> float:
        ts, px = self._sol_price
        if px and time.time() - ts < 30:
            return px
        data = await self._get(JUP_PRICE, self.dex_limit, {"ids": SOL_MINT})
        try:
            new = float(data[SOL_MINT]["usdPrice"])
        except (KeyError, TypeError, ValueError):
            new = 0.0
        if not new:
            pairs = await self._get(f"{DEXSCREENER}/tokens/v1/solana/{SOL_MINT}", self.dex_limit) or []
            usdc = [p for p in pairs if isinstance(p, dict)
                    and (p.get("quoteToken") or {}).get("address") == USDC_MINT]
            usdc.sort(key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0), reverse=True)
            if usdc:
                new = float(usdc[0].get("priceUsd") or 0)
        if new:
            self._sol_price = (time.time(), new)
            return new
        return px  # stale is better than zero
