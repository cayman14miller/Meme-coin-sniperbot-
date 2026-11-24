"""Market recorder: builds an unbiased dataset of every coin the scanner can see.

Why: evaluating entry rules on the handful of coins the bot happened to buy is biased and far too small.
The recorder snapshots EVERY discovered candidate on a fixed schedule - including the many the bot would
skip - and keeps following each one for hours, so later studies can measure base rates (how often
candidates dump), and replay any entry rule using only data that existed at decision time.

It never trades and never touches the bot's database. Run it alongside or instead of the bot:

    python -m sniperbot.recorder --db market-data.db            # runs until Ctrl+C
    python -m sniperbot.recorder --db market-data.db --hours 2  # stop after 2 hours

Data recorded (all timestamps unix seconds, UTC):
  sightings   - which discovery feed listed which mint, its rank and boost amounts (each discovery pass)
  dex_snaps   - DexScreener best-pair snapshot: price, liquidity, FDV, volume/txns/price-change windows
  gecko_snaps - GeckoTerminal pool snapshot: buys/sells AND unique buyers/sellers/traders (wash-trading
                signal), volume windows, reserve, locked-liquidity %
  safety      - RugCheck summary (score, risks, LP locked %) at first sighting and hourly
  sol_price   - SOL/USD each cycle
  cycles      - health log: duration, counts, request errors
Raw JSON of a subset of snapshots is kept (zlib-compressed) so new features can be derived later.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sqlite3
import time
import zlib
from dataclasses import dataclass, field
from typing import Any

import httpx

from .data.market import DEXSCREENER, GECKO, JUP_PRICE, RUGCHECK, SOL_MINT, STABLE_OR_BASE, USDC_MINT, RateLimiter

log = logging.getLogger("recorder")
SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS tokens (
    mint TEXT PRIMARY KEY, symbol TEXT, name TEXT, first_seen REAL, first_source TEXT,
    last_sighting REAL, pair_address TEXT, dex TEXT, quote TEXT, pair_created_at REAL,
    dead_at REAL, untracked_at REAL);
CREATE TABLE IF NOT EXISTS sightings (
    ts REAL, mint TEXT, source TEXT, rank INTEGER, boost_amount REAL, boost_total REAL);
CREATE TABLE IF NOT EXISTS dex_snaps (
    ts REAL, mint TEXT, pair_address TEXT, dex TEXT, n_pairs INTEGER,
    price_usd REAL, price_native REAL, liq_usd REAL, liq_quote REAL, fdv REAL, mcap REAL,
    vol_m5 REAL, vol_h1 REAL, vol_h6 REAL, vol_h24 REAL,
    chg_m5 REAL, chg_h1 REAL, chg_h6 REAL, chg_h24 REAL,
    buys_m5 INTEGER, sells_m5 INTEGER, buys_h1 INTEGER, sells_h1 INTEGER,
    buys_h6 INTEGER, sells_h6 INTEGER, buys_h24 INTEGER, sells_h24 INTEGER,
    boost_active REAL, has_socials INTEGER, raw BLOB);
CREATE TABLE IF NOT EXISTS gecko_snaps (
    ts REAL, pool TEXT, mint TEXT, price_usd REAL, reserve_usd REAL, fdv REAL, locked_liq_pct REAL,
    buys_m5 INTEGER, sells_m5 INTEGER, buyers_m5 INTEGER, sellers_m5 INTEGER, traders_m5 INTEGER,
    buys_m15 INTEGER, sells_m15 INTEGER, buyers_m15 INTEGER, sellers_m15 INTEGER, traders_m15 INTEGER,
    buys_h1 INTEGER, sells_h1 INTEGER, buyers_h1 INTEGER, sellers_h1 INTEGER, traders_h1 INTEGER,
    vol_m5 REAL, vol_m15 REAL, vol_h1 REAL, source TEXT, raw BLOB);
CREATE TABLE IF NOT EXISTS safety (
    ts REAL, mint TEXT, ok INTEGER, score REAL, score_norm REAL, lp_locked_pct REAL,
    n_risks INTEGER, danger TEXT, risks TEXT);
CREATE TABLE IF NOT EXISTS sol_price (ts REAL PRIMARY KEY, usd REAL);
CREATE TABLE IF NOT EXISTS cycles (
    ts REAL PRIMARY KEY, duration REAL, discovered INTEGER, new_tokens INTEGER, tracked INTEGER,
    dex_snaps INTEGER, gecko_snaps INTEGER, safety_checks INTEGER, requests INTEGER, errors INTEGER);
CREATE INDEX IF NOT EXISTS idx_dex_mint_ts ON dex_snaps(mint, ts);
CREATE INDEX IF NOT EXISTS idx_gecko_mint_ts ON gecko_snaps(mint, ts);
CREATE INDEX IF NOT EXISTS idx_sight_mint ON sightings(mint, ts);
CREATE INDEX IF NOT EXISTS idx_safety_mint ON safety(mint, ts);
"""


def _f(x: Any) -> float | None:
    try:
        return None if x is None else float(x)
    except (TypeError, ValueError):
        return None


def _i(x: Any) -> int | None:
    try:
        return None if x is None else int(x)
    except (TypeError, ValueError):
        return None


def _z(obj: Any) -> bytes:
    return zlib.compress(json.dumps(obj, separators=(",", ":")).encode(), 6)


def unz(blob: bytes | None) -> Any:
    return json.loads(zlib.decompress(blob)) if blob else None


@dataclass
class Tracked:
    mint: str
    first_seen: float
    last_sighting: float
    pair: str = ""
    last_dex: float = 0.0
    last_gecko: float = 0.0
    last_safety: float = 0.0
    dex_count: int = 0
    gecko_count: int = 0
    low_liq_since: float = 0.0
    last_liq: float = 0.0


@dataclass
class Config:
    cycle_sec: float = 30.0
    discovery_sec: float = 60.0
    track_hours: float = 6.0
    young_minutes: float = 60.0         # snapshot every cycle while this young (since first seen)
    old_snapshot_sec: float = 120.0     # cadence after that
    gecko_young_sec: float = 120.0
    gecko_old_sec: float = 600.0
    safety_recheck_sec: float = 3600.0
    dead_liq_usd: float = 1000.0
    dead_after_sec: float = 1800.0      # stop following after 30 min below dead_liq
    raw_every: int = 10                 # keep raw JSON for every Nth snapshot per mint (and the first)
    dex_per_min: int = 150              # DexScreener allows 300/min on pair endpoints; leave room for the bot
    gecko_per_min: int = 8              # GeckoTerminal free tier ~30/min shared with the bot
    rug_per_min: int = 20
    min_liq_detail_usd: float = 5000.0  # GeckoTerminal/RugCheck only for coins at least this liquid


@dataclass
class CycleStats:
    discovered: int = 0
    new_tokens: int = 0
    dex_snaps: int = 0
    gecko_snaps: int = 0
    safety_checks: int = 0
    requests: int = 0
    errors: int = 0
    rows: dict[str, list] = field(default_factory=lambda: {
        "sightings": [], "dex_snaps": [], "gecko_snaps": [], "safety": [], "sol_price": []})


class Recorder:
    def __init__(self, db_path: str, cfg: Config | None = None, client: httpx.AsyncClient | None = None) -> None:
        self.cfg = cfg or Config()
        self.db = sqlite3.connect(db_path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.db.execute("INSERT OR IGNORE INTO meta VALUES('schema_version', ?)", (str(SCHEMA_VERSION),))
        self.db.execute("INSERT OR IGNORE INTO meta VALUES('created_at', ?)", (str(time.time()),))
        self.db.commit()
        self.client = client or httpx.AsyncClient(timeout=20, headers={"Accept": "application/json",
                                                                         "User-Agent": "sniperbot-recorder/1"})
        self.dex_limit = RateLimiter(self.cfg.dex_per_min)
        self.gecko_limit = RateLimiter(self.cfg.gecko_per_min)
        self.rug_limit = RateLimiter(self.cfg.rug_per_min)
        self.jup_limit = RateLimiter(30)
        self.tracked: dict[str, Tracked] = {}
        self.last_discovery = 0.0
        self.stop_event = asyncio.Event()
        self._load_tracked()

    # ------------------------------------------------------------------ state
    def _load_tracked(self) -> None:
        """Resume after a restart: keep following coins still inside their tracking window."""
        cutoff = time.time() - self.cfg.track_hours * 3600
        rows = self.db.execute("SELECT mint, first_seen, last_sighting, pair_address FROM tokens "
                               "WHERE last_sighting >= ? AND dead_at IS NULL AND untracked_at IS NULL",
                               (cutoff,)).fetchall()
        for mint, first, last, pair in rows:
            self.tracked[mint] = Tracked(mint, first, last, pair or "")
        if rows:
            log.info("resumed tracking %d tokens", len(rows))

    # ------------------------------------------------------------------ http
    async def _get(self, url: str, limiter: RateLimiter, st: CycleStats, params: dict | None = None) -> Any:
        for attempt in range(3):
            await limiter.wait()
            st.requests += 1
            try:
                r = await self.client.get(url, params=params)
                if r.status_code == 429:
                    st.errors += 1
                    await asyncio.sleep(5 * (attempt + 1))
                    continue
                if r.status_code >= 400:
                    st.errors += 1
                    return None
                return r.json()
            except (httpx.HTTPError, ValueError) as exc:
                st.errors += 1
                log.debug("GET %s failed: %s", url, exc)
                await asyncio.sleep(1 + attempt)
        return None

    async def _get_ts(self, url: str, limiter: RateLimiter, st: CycleStats, params: dict | None = None):
        """Like _get, but also returns the time the response arrived (rows are stamped with it)."""
        data = await self._get(url, limiter, st, params)
        return time.time(), data

    # ------------------------------------------------------------------ discovery
    async def discover(self, now: float, st: CycleStats) -> None:
        feeds = await asyncio.gather(
            self._get(f"{DEXSCREENER}/token-profiles/latest/v1", self.dex_limit, st),
            self._get(f"{DEXSCREENER}/token-boosts/latest/v1", self.dex_limit, st),
            self._get(f"{DEXSCREENER}/token-boosts/top/v1", self.dex_limit, st),
            self._get(f"{GECKO}/networks/solana/new_pools", self.gecko_limit, st, {"page": 1}),
            self._get(f"{GECKO}/networks/solana/trending_pools", self.gecko_limit, st,
                      {"page": 1, "duration": "1h"}),
        )
        found: dict[str, str] = {}
        for source, feed in zip(("dex_profile", "dex_boost_latest", "dex_boost_top"), feeds[:3]):
            for rank, item in enumerate(feed or []):
                if not isinstance(item, dict) or item.get("chainId") != "solana" or not item.get("tokenAddress"):
                    continue
                mint = item["tokenAddress"]
                if mint in STABLE_OR_BASE:
                    continue
                st.rows["sightings"].append((now, mint, source, rank, _f(item.get("amount")),
                                             _f(item.get("totalAmount"))))
                found.setdefault(mint, source)
        for source, feed in zip(("gecko_new", "gecko_trending"), feeds[3:]):
            for rank, pool in enumerate((feed or {}).get("data", []) if isinstance(feed, dict) else []):
                try:
                    mint = pool["relationships"]["base_token"]["data"]["id"].split("_", 1)[1]
                except (KeyError, IndexError, TypeError, AttributeError):
                    continue
                if mint in STABLE_OR_BASE:
                    continue
                st.rows["sightings"].append((now, mint, source, rank, None, None))
                found.setdefault(mint, source)
                self._gecko_row(pool.get("attributes") or {}, mint, time.time(), st, source, keep_raw=False)
        st.discovered = len(found)
        for mint, source in found.items():
            t = self.tracked.get(mint)
            if t:
                t.last_sighting = now
                continue
            row = self.db.execute("SELECT first_seen FROM tokens WHERE mint=?", (mint,)).fetchone()
            if row:  # seen before, tracking had lapsed: resume
                self.tracked[mint] = Tracked(mint, row[0], now)
                self.db.execute("UPDATE tokens SET untracked_at=NULL, dead_at=NULL WHERE mint=?", (mint,))
            else:
                self.tracked[mint] = Tracked(mint, now, now)
                self.db.execute("INSERT INTO tokens(mint, first_seen, first_source, last_sighting) VALUES(?,?,?,?)",
                                (mint, now, source, now))
                st.new_tokens += 1
        self.db.executemany("UPDATE tokens SET last_sighting=? WHERE mint=?", [(now, m) for m in found])
        self.last_discovery = now

    # ------------------------------------------------------------------ dexscreener
    def _due_dex(self, now: float) -> list[Tracked]:
        due = []
        for t in self.tracked.values():
            young = now - t.first_seen < self.cfg.young_minutes * 60
            interval = 0 if young else self.cfg.old_snapshot_sec
            if now - t.last_dex >= interval - 1:
                due.append(t)
        return due

    async def snapshot_dex(self, now: float, st: CycleStats) -> None:
        due = self._due_dex(now)
        chunks = [due[i:i + 30] for i in range(0, len(due), 30)]
        results = await asyncio.gather(*(self._get_ts(
            f"{DEXSCREENER}/tokens/v1/solana/{','.join(t.mint for t in c)}", self.dex_limit, st) for c in chunks))
        for chunk, (ts, data) in zip(chunks, results):
            if isinstance(data, dict):
                data = data.get("pairs") or []
            by_mint: dict[str, list[dict]] = {}
            for p in data or []:
                try:
                    if p.get("chainId") == "solana" and \
                            (p.get("quoteToken") or {}).get("address") in (SOL_MINT, USDC_MINT):
                        by_mint.setdefault(p["baseToken"]["address"], []).append(p)
                except (KeyError, AttributeError, TypeError):
                    continue
            for t in chunk:
                pairs = by_mint.get(t.mint)
                if not pairs:
                    if data is not None:  # answered, but no SOL/USDC pair: untradeable, retire like dead liquidity
                        t.low_liq_since = t.low_liq_since or ts
                        t.last_dex = ts
                    continue
                best = max(pairs, key=lambda p: _f((p.get("liquidity") or {}).get("usd")) or 0)
                self._dex_row(t, best, len(pairs), ts, st)

    def _dex_row(self, t: Tracked, p: dict, n_pairs: int, now: float, st: CycleStats) -> None:
        liq = p.get("liquidity") or {}
        vol, chg, tx = p.get("volume") or {}, p.get("priceChange") or {}, p.get("txns") or {}
        info = p.get("info") or {}
        keep_raw = t.dex_count % self.cfg.raw_every == 0
        pair = p.get("pairAddress", "")
        st.rows["dex_snaps"].append((
            now, t.mint, pair, p.get("dexId"), n_pairs, _f(p.get("priceUsd")), _f(p.get("priceNative")),
            _f(liq.get("usd")), _f(liq.get("quote")), _f(p.get("fdv")), _f(p.get("marketCap")),
            _f(vol.get("m5")), _f(vol.get("h1")), _f(vol.get("h6")), _f(vol.get("h24")),
            _f(chg.get("m5")), _f(chg.get("h1")), _f(chg.get("h6")), _f(chg.get("h24")),
            *(_i((tx.get(w) or {}).get(k)) for w in ("m5", "h1", "h6", "h24") for k in ("buys", "sells")),
            _f((p.get("boosts") or {}).get("active")), int(bool(info.get("socials") or info.get("websites"))),
            _z(p) if keep_raw else None))
        t.dex_count += 1
        t.last_dex = now
        st.dex_snaps += 1
        if pair and pair != t.pair:
            t.pair = pair
            base = p.get("baseToken") or {}
            self.db.execute("UPDATE tokens SET pair_address=?, dex=?, quote=?, pair_created_at=?, symbol=?, name=? "
                            "WHERE mint=?", (pair, p.get("dexId"), (p.get("quoteToken") or {}).get("symbol"),
                                             (_f(p.get("pairCreatedAt")) or 0) / 1000 or None, base.get("symbol"),
                                             base.get("name"), t.mint))
        liq_usd = _f(liq.get("usd")) or 0
        price = _f(p.get("priceUsd")) or 0
        t.last_liq = liq_usd
        if liq_usd < self.cfg.dead_liq_usd or price <= 0:
            t.low_liq_since = t.low_liq_since or now
        else:
            t.low_liq_since = 0.0

    # ------------------------------------------------------------------ geckoterminal
    def _gecko_row(self, a: dict, mint: str, now: float, st: CycleStats, source: str, keep_raw: bool) -> None:
        tx, vol = a.get("transactions") or {}, a.get("volume_usd") or {}

        def w(win: str, k: str) -> int | None:
            return _i((tx.get(win) or {}).get(k))

        st.rows["gecko_snaps"].append((
            now, a.get("address"), mint, _f(a.get("base_token_price_usd")), _f(a.get("reserve_in_usd")),
            _f(a.get("fdv_usd")), _f(a.get("locked_liquidity_percentage")),
            *(w(win, k) for win in ("m5", "m15", "h1") for k in ("buys", "sells", "buyers", "sellers", "unique_traders")),
            _f(vol.get("m5")), _f(vol.get("m15")), _f(vol.get("h1")), source, _z(a) if keep_raw else None))
        st.gecko_snaps += 1

    async def snapshot_gecko(self, now: float, st: CycleStats, max_calls: int) -> None:
        due = []
        for t in self.tracked.values():
            if not t.pair or t.last_liq < self.cfg.min_liq_detail_usd:
                continue
            young = now - t.first_seen < self.cfg.young_minutes * 60
            interval = self.cfg.gecko_young_sec if young else self.cfg.gecko_old_sec
            if now - t.last_gecko >= interval - 1:
                due.append(t)
        due.sort(key=lambda t: t.last_gecko)  # stalest first
        due = due[:30 * max_calls]
        chunks = [due[i:i + 30] for i in range(0, len(due), 30)]
        results = await asyncio.gather(*(self._get_ts(
            f"{GECKO}/networks/solana/pools/multi/{','.join(t.pair for t in c)}", self.gecko_limit, st)
            for c in chunks))
        for chunk, (ts, data) in zip(chunks, results):
            by_pool = {}
            for d in (data or {}).get("data", []) if isinstance(data, dict) else []:
                a = d.get("attributes") or {}
                if a.get("address"):
                    by_pool[a["address"]] = a
            for t in chunk:
                t.last_gecko = now  # even if absent, don't retry every cycle
                a = by_pool.get(t.pair)
                if a:
                    self._gecko_row(a, t.mint, ts, st, "multi", keep_raw=t.gecko_count % self.cfg.raw_every == 0)
                    t.gecko_count += 1

    # ------------------------------------------------------------------ rugcheck
    async def safety_checks(self, now: float, st: CycleStats, max_calls: int) -> None:
        due = [t for t in self.tracked.values() if now - t.last_safety >= self.cfg.safety_recheck_sec
               and t.last_liq >= self.cfg.min_liq_detail_usd]
        due.sort(key=lambda t: (t.last_safety > 0, -t.first_seen))  # never-checked first, newest first
        due = due[:max_calls]
        results = await asyncio.gather(*(self._get_ts(f"{RUGCHECK}/tokens/{t.mint}/report/summary",
                                                      self.rug_limit, st) for t in due))
        for t, (ts, data) in zip(due, results):
            t.last_safety = now
            if not isinstance(data, dict):
                continue
            risks = data.get("risks") or []
            danger = [r.get("name") for r in risks if r.get("level") == "danger"]
            st.rows["safety"].append((ts, t.mint, int(not danger), _f(data.get("score")),
                                      _f(data.get("score_normalised")), _f(data.get("lpLockedPct")), len(risks),
                                      json.dumps(danger), json.dumps(risks)))
            st.safety_checks += 1

    # ------------------------------------------------------------------ cycle
    def _retire(self, now: float) -> None:
        now = max(now, time.time())  # snapshot rows are stamped at response time, later than cycle start
        for mint, t in list(self.tracked.items()):
            if t.low_liq_since and now - t.low_liq_since >= self.cfg.dead_after_sec:
                self.db.execute("UPDATE tokens SET dead_at=? WHERE mint=?", (now, mint))
                del self.tracked[mint]
            elif now - t.last_sighting > self.cfg.track_hours * 3600:
                self.db.execute("UPDATE tokens SET untracked_at=? WHERE mint=?", (now, mint))
                del self.tracked[mint]

    def _flush(self, now: float, st: CycleStats, duration: float) -> None:
        r = st.rows
        with self.db:
            self.db.executemany("INSERT INTO sightings VALUES(?,?,?,?,?,?)", r["sightings"])
            self.db.executemany(f"INSERT INTO dex_snaps VALUES({','.join('?' * 30)})", r["dex_snaps"])
            self.db.executemany(f"INSERT INTO gecko_snaps VALUES({','.join('?' * 27)})", r["gecko_snaps"])
            self.db.executemany("INSERT INTO safety VALUES(?,?,?,?,?,?,?,?,?)", r["safety"])
            self.db.executemany("INSERT OR REPLACE INTO sol_price VALUES(?,?)", r["sol_price"])
            self.db.execute("INSERT OR REPLACE INTO cycles VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (now, duration, st.discovered, st.new_tokens, len(self.tracked), st.dex_snaps,
                             st.gecko_snaps, st.safety_checks, st.requests, st.errors))

    async def cycle(self) -> CycleStats:
        now = time.time()
        st = CycleStats()
        if now - self.last_discovery >= self.cfg.discovery_sec - 1:
            await self.discover(now, st)
        jup = self._get_ts(JUP_PRICE, self.jup_limit, st, {"ids": SOL_MINT})
        # per-cycle budgets keep each service under its rate limit
        cycles_per_min = max(60 / self.cfg.cycle_sec, 1)
        gecko_calls = max(int(self.cfg.gecko_per_min / cycles_per_min) - (1 if st.discovered else 0), 1)
        rug_calls = max(int(self.cfg.rug_per_min / cycles_per_min), 1)
        sol, *_ = await asyncio.gather(jup, self.snapshot_dex(now, st), self.snapshot_gecko(now, st, gecko_calls),
                                       self.safety_checks(now, st, rug_calls))
        try:
            st.rows["sol_price"].append((sol[0], float(sol[1][SOL_MINT]["usdPrice"])))
        except (KeyError, TypeError, ValueError):
            pass
        self._retire(now)
        self._flush(now, st, time.time() - now)
        return st

    async def run(self, hours: float | None = None) -> None:
        end = time.time() + hours * 3600 if hours else None
        log.info("recording to %s (cycle %ss, tracking %sh)", self.db_path_str(), self.cfg.cycle_sec,
                 self.cfg.track_hours)
        last_log = 0.0
        while not self.stop_event.is_set() and (end is None or time.time() < end):
            start = time.time()
            try:
                st = await self.cycle()
                if time.time() - last_log >= 60:
                    last_log = time.time()
                    log.info("tracked %d | +%d new | dex %d | gecko %d | rugcheck %d | req %d | err %d | %.1fs",
                             len(self.tracked), st.new_tokens, st.dex_snaps, st.gecko_snaps, st.safety_checks,
                             st.requests, st.errors, time.time() - start)
            except Exception:
                log.exception("cycle failed (continuing)")
            try:
                await asyncio.wait_for(self.stop_event.wait(),
                                       timeout=max(self.cfg.cycle_sec - (time.time() - start), 1))
            except asyncio.TimeoutError:
                pass
        await self.client.aclose()
        self.db.close()
        log.info("recorder stopped")

    def db_path_str(self) -> str:
        return self.db.execute("PRAGMA database_list").fetchone()[2]


def main() -> None:
    ap = argparse.ArgumentParser(description="Record the meme-coin candidate universe (read-only, never trades).")
    ap.add_argument("--db", default="market-data.db")
    ap.add_argument("--hours", type=float, default=None, help="stop after N hours (default: run until Ctrl+C)")
    ap.add_argument("--cycle-sec", type=float, default=30)
    ap.add_argument("--track-hours", type=float, default=6)
    ap.add_argument("--gecko-per-min", type=int, default=8,
                    help="GeckoTerminal budget; keep total with the bot under ~30/min")
    ap.add_argument("--dex-per-min", type=int, default=150)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cfg = Config(cycle_sec=args.cycle_sec, track_hours=args.track_hours, gecko_per_min=args.gecko_per_min,
                 dex_per_min=args.dex_per_min)

    async def go() -> None:
        rec = Recorder(args.db, cfg)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, rec.stop_event.set)
            except NotImplementedError:  # pragma: no cover (Windows)
                pass
        await rec.run(args.hours)

    asyncio.run(go())


if __name__ == "__main__":
    main()
