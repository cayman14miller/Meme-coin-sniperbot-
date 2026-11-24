"""The trading engine: discover -> research -> buy -> monitor -> sell."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from .analysis.signals import Analysis, analyze, exit_signal
from .config import Secrets, Settings
from .data.market import MarketData, PairInfo
from .notify import Notifier
from .risk import RiskManager, evaluate_exit, start_of_day_utc
from .store import Store
from .trading.broker import Broker, JupiterBroker, PaperBroker

log = logging.getLogger(__name__)

MAX_DEEP_RESEARCH_PER_SCAN = 12   # candles + safety calls are rate-limited, so research the best few
REBUY_COOLDOWN_SEC = 2 * 3600     # don't re-enter the same token for 2h


class Engine:
    def __init__(self, secrets: Secrets, store: Store, market: MarketData | None = None,
                 broker: Broker | None = None, notifier: Notifier | None = None) -> None:
        self.secrets = secrets
        self.store = store
        self.settings = Settings()
        saved = store.get("settings")
        if saved:
            try:
                self.settings.update(saved)
            except ValueError as exc:
                log.warning("ignoring invalid saved settings: %s", exc)
        self.market = market or MarketData()
        self.notifier = notifier or Notifier(store, secrets)
        self.risk = RiskManager(store)
        if broker is not None:
            self.broker = broker
        elif secrets.mode == "live":
            problems = secrets.live_problems()
            if problems:
                raise SystemExit("Live trading blocked by safety interlocks:\n  - " + "\n  - ".join(problems))
            self.broker = JupiterBroker(secrets.solana_private_key, secrets.solana_rpc_url, secrets.jupiter_api_key)
        else:
            self.broker = PaperBroker(store, self.settings.paper_starting_sol)
        self.radar: dict[str, dict[str, Any]] = {}
        self.last_scan: dict[str, Any] = {"ts": 0, "discovered": 0, "passed_filters": 0, "researched": 0}
        self.sol_usd = 0.0
        self.equity_usd = 0.0
        self.balance_sol = 0.0
        self._last_equity_save = 0.0
        self._last_exit_check: dict[int, float] = {}
        self._last_sell_fail: dict[int, float] = {}
        self._last_priced: dict[int, float] = {}
        self._last_stale_alert: dict[int, float] = {}
        self._last_heartbeat = 0.0
        self.equity_sol = 0.0
        self._trade_lock = asyncio.Lock()
        self._tasks: list[asyncio.Task] = []

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        await self.reconcile_unresolved()
        open_pos = self.store.open_positions()
        self.notifier.send("info", f"Sniper bot online ({self.broker.mode} mode)",
                           f"Trading is {'ON' if self.settings.enabled else 'OFF'}. "
                           f"Notifications: {', '.join(self.notifier.channels)}"
                           + (f"\nResuming protection of {len(open_pos)} open position(s): "
                              + ", ".join(p["symbol"] for p in open_pos) if open_pos else ""),
                           push=bool(open_pos))
        self._tasks = [
            asyncio.create_task(self._loop(self.scan_once, lambda: self.settings.scan_interval_sec), name="scan"),
            asyncio.create_task(self._loop(self.monitor_once, lambda: self.settings.monitor_interval_sec),
                                name="monitor"),
            asyncio.create_task(self.notifier.telegram_commands(self.telegram_command), name="telegram"),
        ]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.market.close()
        await self.broker.close()
        await self.notifier.close()

    async def _loop(self, fn, interval) -> None:
        while True:
            try:
                await fn()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("%s failed", fn.__name__)
                self.notifier.send("error", f"{fn.__name__} error", str(exc)[:300], push=False)
            await asyncio.sleep(interval())

    # ------------------------------------------------------------------ control
    def save_settings(self) -> None:
        self.store.set("settings", self.settings.to_dict())

    def update_settings(self, data: dict[str, Any]) -> list[str]:
        changed = self.settings.update(data)
        if changed:
            self.save_settings()
            if "enabled" in changed:
                state = "ON" if self.settings.enabled else "OFF"
                self.notifier.send("info", f"Trading turned {state}")
            else:
                self.notifier.send("info", "Settings updated", ", ".join(changed), push=False)
        return changed

    def set_enabled(self, on: bool) -> None:
        self.update_settings({"enabled": on})

    def resume_risk(self) -> None:
        self.risk.reset(self.equity_sol)
        self.notifier.send("info", "Risk lock reset", "Kill switch cleared - bot may trade again "
                           f"(trading is {'ON' if self.settings.enabled else 'OFF'}).")

    async def telegram_command(self, cmd: str) -> str:
        if cmd in ("/on", "/start_trading"):
            if self.risk.tripped:
                return f"Risk lock is active: {self.risk.trip_reason}. Send /resume first."
            self.set_enabled(True)
            return "Trading ON"
        if cmd in ("/off", "/stop"):
            self.set_enabled(False)
            return "Trading OFF (open positions still protected by stop loss)"
        if cmd == "/resume":
            self.resume_risk()
            return "Risk lock cleared"
        if cmd == "/panic":
            self.set_enabled(False)
            n = await self.sell_all("panic (telegram)")
            return f"Trading OFF, sold {n} positions"
        st = self.store.stats()
        return (f"{'ON' if self.settings.enabled else 'OFF'} | {self.broker.mode}"
                f"{' | RISK LOCKED: ' + self.risk.trip_reason if self.risk.tripped else ''}\n"
                f"Equity ${self.equity_usd:,.2f}  SOL {self.balance_sol:.3f}\n"
                f"Open {len(self.store.open_positions())} | W/L {st['wins']}/{st['losses']} "
                f"({st['win_rate']:.0f}%) | PnL ${st['realized_pnl_usd']:+.2f}\n"
                "Commands: /status /on /off /resume /panic")

    # ------------------------------------------------------------------ scanning
    def passes_filters(self, p: PairInfo) -> tuple[bool, str]:
        s = self.settings
        if p.price_usd <= 0:
            return False, "no price"
        if not s.min_liquidity_usd <= p.liquidity_usd <= s.max_liquidity_usd:
            return False, f"liquidity ${p.liquidity_usd:,.0f}"
        if p.volume.get("m5", 0) < s.min_volume_5m_usd:
            return False, f"5m volume ${p.volume.get('m5', 0):,.0f}"
        if p.age_minutes < s.min_pair_age_minutes:
            return False, f"too new ({p.age_minutes:.1f}m)"
        if p.age_minutes > s.max_pair_age_hours * 60:
            return False, f"too old ({p.age_minutes / 60:.0f}h)"
        if p.buy_sell_ratio("m5") < s.min_buy_sell_ratio:
            return False, f"buy/sell {p.buy_sell_ratio('m5'):.2f}"
        if s.max_fdv_usd and p.fdv > s.max_fdv_usd:
            return False, f"FDV ${p.fdv:,.0f}"
        return True, ""

    def enabled_strategies(self) -> list[str]:
        s = self.settings
        return [n for n, on in (("early", s.enable_early), ("momentum", s.enable_momentum),
                                ("dip", s.enable_dip), ("breakout", s.enable_breakout)) if on]

    async def research(self, p: PairInfo) -> tuple[Analysis, dict[str, Any]]:
        """Deep research on one coin: safety report + 1m chart analysis."""
        safety: dict[str, Any] = {"checked": False}
        if self.settings.require_safety_check:
            rep = await self.market.safety(p.mint, self.settings.max_rugcheck_score)
            safety = {"checked": True, "ok": rep.ok, "score": rep.score, "risks": rep.risks, "reason": rep.reason}
            if not rep.ok:
                return Analysis(0, "rejected", [f"safety: {rep.reason}"], {}, {}), safety
        candles = await self.market.candles(p.pair_address, "minute", 1, 120)
        a = analyze(p, candles, self.enabled_strategies())
        return a, safety

    async def scan_once(self) -> None:
        s = self.settings
        await self.refresh_account()
        if not s.enabled or self.risk.tripped:
            return
        open_pos = self.store.open_positions()
        if len(open_pos) >= s.max_open_positions or self.risk.in_cooldown(s):
            return
        mints = await self.market.discover_mints()
        pairs = await self.market.pairs_for(mints)
        held = {p["mint"] for p in open_pos}
        candidates = []
        for p in pairs.values():
            if p.mint in held or self.store.recently_traded(p.mint, time.time() - REBUY_COOLDOWN_SEC):
                continue
            ok, why = self.passes_filters(p)
            if ok:
                candidates.append(p)
        # pre-rank cheaply by activity before spending rate-limited calls on deep research
        candidates.sort(key=lambda p: (p.volume.get("m5", 0) / max(p.liquidity_usd, 1)) * p.buy_sell_ratio("m5"),
                        reverse=True)
        self.last_scan = {"ts": time.time(), "discovered": len(mints), "passed_filters": len(candidates),
                          "researched": 0}
        for p in candidates[:MAX_DEEP_RESEARCH_PER_SCAN]:
            if not self.settings.enabled or self.risk.tripped:
                break
            a, safety = await self.research(p)
            self.last_scan["researched"] += 1
            self.radar[p.mint] = self._radar_entry(p, a, safety)
            action, note = "skip", f"score {a.score:.0f} < {s.min_entry_score:g}"
            if a.strategy == "rejected":
                note = "; ".join(a.reasons)
            elif a.score >= s.min_entry_score:
                if len(self.store.open_positions()) >= s.max_open_positions:
                    self._log_decision(p, a, safety, "skip", "max open positions reached")
                    break
                pid = await self.buy(p, a)
                action, note = ("buy", f"position {pid}") if pid else ("buy_failed", "see events")
            self._log_decision(p, a, safety, action, note)
        # keep radar small & fresh
        cutoff = time.time() - 1800
        self.radar = {k: v for k, v in sorted(self.radar.items(), key=lambda kv: -kv[1]["ts"])[:60]
                      if v["ts"] >= cutoff}

    def _log_decision(self, p: PairInfo, a: Analysis, safety: dict, action: str, note: str) -> None:
        """Every researched candidate is logged with the exact inputs behind the decision."""
        self.store.add_decision(
            mint=p.mint, symbol=p.symbol, pair_address=p.pair_address, action=action, note=note,
            strategy=a.strategy, score=a.score, scores=json.dumps(a.scores), reasons=json.dumps(a.reasons),
            metrics=json.dumps({k: round(v, 8) for k, v in a.metrics.items()}),
            pair=json.dumps({"price_usd": p.price_usd, "liq": p.liquidity_usd, "fdv": p.fdv, "age_min": p.age_minutes,
                             "volume": p.volume, "price_change": p.price_change, "buys": p.buys, "sells": p.sells,
                             "boosted": p.boosted, "socials": p.has_socials}),
            safety=json.dumps(safety))

    def _radar_entry(self, p: PairInfo, a: Analysis, safety: dict) -> dict[str, Any]:
        return {"ts": time.time(), "mint": p.mint, "symbol": p.symbol, "name": p.name, "url": p.url,
                "price_usd": p.price_usd, "liquidity_usd": p.liquidity_usd, "fdv": p.fdv,
                "age_min": p.age_minutes, "vol_m5": p.volume.get("m5", 0), "chg_m5": p.price_change.get("m5", 0),
                "chg_h1": p.price_change.get("h1", 0), "bs_ratio": p.buy_sell_ratio("m5"),
                "score": a.score, "strategy": a.strategy, "scores": a.scores, "reasons": a.reasons,
                "metrics": {k: round(v, 6) for k, v in a.metrics.items()}, "safety": safety}

    # ------------------------------------------------------------------ trading
    def trade_size_sol(self) -> float:
        s = self.settings
        return s.trade_size if s.trade_unit == "SOL" else (s.trade_size / self.sol_usd if self.sol_usd else 0)

    def deployed_usd(self) -> float:
        return sum(p["cost_usd"] for p in self.store.open_positions())

    async def buy(self, p: PairInfo, a: Analysis, manual: bool = False) -> int | None:
        async with self._trade_lock:
            s = self.settings
            if not self.sol_usd:
                await self.refresh_account()
            size_sol = self.trade_size_sol()
            size_usd = size_sol * self.sol_usd
            if size_sol <= 0:
                return None
            if self.broker.mode == "live":
                cap = self.secrets.live_max_trade_sol
                deployed_sol = sum(x["cost_sol"] for x in self.store.open_positions())
                if size_sol > cap or deployed_sol + size_sol > self.secrets.live_max_deployed_sol:
                    self.notifier.send("warning", f"Skipped {p.symbol}: live hard cap",
                                       f"size {size_sol:.4f} SOL (cap {cap}), deployed {deployed_sol:.4f} SOL "
                                       f"(cap {self.secrets.live_max_deployed_sol})")
                    return None
            if self.deployed_usd() + size_usd > s.max_capital_usd:
                self.notifier.send("warning", f"Skipped {p.symbol}: capital limit",
                                   f"deployed ${self.deployed_usd():.2f} + ${size_usd:.2f} > "
                                   f"max ${s.max_capital_usd:.2f}", push=False)
                return None
            if isinstance(self.broker, PaperBroker):
                self.broker.liquidity[p.mint] = p.liquidity_usd
            # write-ahead record: if the process dies or the broker errors mid-buy, the attempt is not lost
            pid = self.store.open_position(
                mint=p.mint, symbol=p.symbol, name=p.name, pair_address=p.pair_address,
                strategy=("manual:" if manual else "") + a.strategy, score=a.score,
                reasons=json.dumps(a.reasons), entry_price=p.price_usd, qty=0.0, initial_qty=0.0,
                cost_sol=size_sol, cost_usd=size_usd, mode=self.broker.mode, status="pending")
            try:
                fill = await self.broker.buy(p.mint, size_sol, p.price_usd, self.sol_usd, s.slippage_bps,
                                             s.priority_fee_lamports)
            except Exception as exc:
                self.store.update_position(pid, status="unknown", exit_reason=f"buy error: {str(exc)[:200]}")
                self.notifier.send("error", f"Buy {p.symbol} failed", str(exc)[:300])
                await self.reconcile_unresolved()
                return None
            self.store.update_position(pid, status="open", entry_price=fill.price_usd, qty=fill.qty,
                                       initial_qty=fill.qty, peak_price=fill.price_usd, last_price=fill.price_usd,
                                       cost_sol=fill.sol, cost_usd=fill.sol * self.sol_usd)
            self._last_priced[pid] = time.time()
            self.store.add_trade(position_id=pid, side="buy", mint=p.mint, symbol=p.symbol, price=fill.price_usd,
                                 qty=fill.qty, sol=fill.sol, usd=fill.sol * self.sol_usd, reason=a.summary,
                                 tx=fill.tx, mode=self.broker.mode)
            self.notifier.send("trade", f"BUY {p.symbol} ({a.strategy}, score {a.score:.0f})",
                               f"{fill.sol:.4f} SOL (${fill.sol * self.sol_usd:.2f}) @ ${fill.price_usd:.8g}\n"
                               f"SL ${fill.price_usd * (1 - s.stop_loss_pct / 100):.8g} (-{s.stop_loss_pct:g}%)\n"
                               + "; ".join(a.reasons[:5]) + (f"\n{p.url}" if p.url else ""))
            await self.refresh_account()
            return pid

    async def sell(self, pos: dict, fraction: float, reason: str, price_usd: float | None = None) -> bool:
        async with self._trade_lock:
            pos = self.store.position(pos["id"])
            if not pos or pos["status"] != "open":
                return False
            s = self.settings
            price = price_usd or pos["last_price"] or pos["entry_price"]
            if not self.sol_usd:
                await self.refresh_account()
            full = fraction >= 0.999
            qty = pos["qty"] if full else pos["qty"] * fraction
            try:
                fill = await self.broker.sell(pos["mint"], qty, price, self.sol_usd, max(s.slippage_bps, 500),
                                              s.priority_fee_lamports, sell_all=full)
            except Exception as exc:
                # retried every monitor tick; only push a notification every 5 minutes per position
                last = self._last_sell_fail.get(pos["id"], 0)
                self.notifier.send("error", f"Sell {pos['symbol']} failed (will retry)",
                                   f"{reason}: {str(exc)[:300]}", push=time.time() - last > 300)
                if time.time() - last > 300:
                    self._last_sell_fail[pos["id"]] = time.time()
                return False
            got_usd = fill.sol * self.sol_usd
            realized_usd = pos["realized_usd"] + got_usd
            realized_sol = pos["realized_sol"] + fill.sol
            remaining = 0.0 if full else max(pos["qty"] - fill.qty, 0.0)
            update: dict[str, Any] = {"qty": remaining, "realized_usd": realized_usd, "realized_sol": realized_sol,
                                      "last_price": price}
            if not full:
                update["tp1_done"] = 1
            pnl_usd = None
            if full or remaining <= 0:
                update.update(status="closed", closed_at=time.time(), exit_reason=reason)
                pnl_usd = realized_usd - pos["cost_usd"]
            self.store.update_position(pos["id"], **update)
            self.store.add_trade(position_id=pos["id"], side="sell", mint=pos["mint"], symbol=pos["symbol"],
                                 price=fill.price_usd, qty=fill.qty, sol=fill.sol, usd=got_usd, pnl_usd=pnl_usd,
                                 reason=reason, tx=fill.tx, mode=self.broker.mode)
            if pnl_usd is not None:
                pct = pnl_usd / pos["cost_usd"] * 100 if pos["cost_usd"] else 0
                level = "win" if pnl_usd > 0 else "loss"
                self.notifier.send(level, f"SOLD {pos['symbol']} {'WIN' if pnl_usd > 0 else 'LOSS'} "
                                          f"{pnl_usd:+.2f} USD ({pct:+.1f}%)", reason)
            else:
                self.notifier.send("trade", f"Partial sell {pos['symbol']} ({fraction:.0%})",
                                   f"{reason}: +{fill.sol:.4f} SOL")
            await self.refresh_account()
            return True

    async def sell_all(self, reason: str) -> int:
        n = 0
        for pos in self.store.open_positions():
            if await self.sell(pos, 1.0, reason):
                n += 1
        return n

    # ------------------------------------------------------------------ recovery
    async def reconcile_unresolved(self) -> None:
        """Resolve buys with unknown outcome by checking what the wallet actually holds.

        A live swap can land on-chain even though our code saw an error or timeout. Without this, those
        tokens would sit in the wallet with no stop loss and no record.
        """
        for pos in self.store.unresolved_positions():
            try:
                held = await self.broker.token_balance(pos["mint"])
            except Exception as exc:
                self.notifier.send("error", f"Cannot verify buy of {pos['symbol']} (will retry)", str(exc)[:200],
                                   push=time.time() - self._last_stale_alert.get(-pos["id"], 0) > 300)
                self._last_stale_alert[-pos["id"]] = time.time()
                continue
            if held is None:  # paper: an errored buy never changes holdings
                self.store.update_position(pos["id"], status="failed", closed_at=time.time())
            elif held > 0:
                price = pos["entry_price"]
                pairs = await self.market.pairs_for([pos["mint"]])
                if pos["mint"] in pairs:
                    price = pairs[pos["mint"]].price_usd or price
                self.store.update_position(pos["id"], status="open", qty=held, initial_qty=held, entry_price=price,
                                           peak_price=price, last_price=price,
                                           exit_reason="adopted after uncertain buy (entry price estimated)")
                self._last_priced[pos["id"]] = time.time()
                self.notifier.send("warning", f"Recovered {pos['symbol']} position after uncertain buy",
                                   f"Wallet holds {held:g} tokens; now protected by stop loss. "
                                   f"Entry price estimated at ${price:.8g}.")
            else:
                self.store.update_position(pos["id"], status="failed", closed_at=time.time())

    # ------------------------------------------------------------------ monitoring
    async def refresh_account(self) -> None:
        px = await self.market.sol_price()
        if px:
            self.sol_usd = px
        try:
            self.balance_sol = await self.broker.balance_sol()
        except Exception as exc:
            log.warning("balance error: %s", exc)
        holdings = sum(p["qty"] * (p["last_price"] or p["entry_price"]) for p in self.store.open_positions())
        if self.sol_usd:
            self.equity_usd = self.balance_sol * self.sol_usd + holdings
            self.equity_sol = self.balance_sol + holdings / self.sol_usd

    async def monitor_once(self) -> None:
        if self.store.unresolved_positions():
            await self.reconcile_unresolved()
        positions = self.store.open_positions()
        if positions:
            mints = list({p["mint"] for p in positions})
            pairs = await self.market.pairs_for(mints)
            missing = [m for m in mints if m not in pairs or pairs[m].price_usd <= 0]
            fallback = await self.market.jup_prices(missing) if missing else {}
            now = time.time()
            ticks = []
            for pos in positions:
                pair = pairs.get(pos["mint"])
                if pair and pair.price_usd > 0:
                    price, source = pair.price_usd, "dexscreener"
                elif pos["mint"] in fallback:
                    price, source = fallback[pos["mint"]], "jupiter"
                else:
                    self._alert_unpriced(pos, now)
                    continue
                self._last_priced[pos["id"]] = now
                ticks.append((now, pos["id"], price, pair.liquidity_usd if pair else None, source))
                if not pair:  # stop/TP rules still apply on the fallback price; chart exits need pair data
                    peak = max(pos["peak_price"], price)
                    self.store.update_position(pos["id"], last_price=price, peak_price=peak)
                    pos.update(last_price=price, peak_price=peak)
                    decision = evaluate_exit(pos, price, self.settings)
                    if decision.fraction > 0:
                        await self.sell(pos, decision.fraction, decision.reason + " [fallback price]", price)
                    continue
                if isinstance(self.broker, PaperBroker):
                    self.broker.liquidity[pos["mint"]] = pair.liquidity_usd
                peak = max(pos["peak_price"], price)
                self.store.update_position(pos["id"], last_price=price, peak_price=peak)
                pos.update(last_price=price, peak_price=peak)
                decision = evaluate_exit(pos, price, self.settings)
                if decision.fraction > 0:
                    await self.sell(pos, decision.fraction, decision.reason, price)
                    continue
                if self.settings.use_signal_exits and time.time() - self._last_exit_check.get(pos["id"], 0) > 30:
                    self._last_exit_check[pos["id"]] = time.time()
                    candles = await self.market.candles(pos["pair_address"], "minute", 1, 120)
                    sig = exit_signal(pair, candles, pos["entry_price"])
                    if sig.exit:
                        await self.sell(pos, 1.0, f"chart exit: {sig.reason}", price)
            if ticks:
                self.store.add_ticks(ticks)
        await self.refresh_account()
        await self._heartbeat()
        if not self.equity_usd:
            return
        if time.time() - self._last_equity_save > 60:
            self.store.add_equity(self.equity_usd)
            self._last_equity_save = time.time()
        reason = self.risk.check(self.equity_sol, self.settings)
        if reason:
            self.risk.trip(reason)
            self.notifier.send("risk", "RISK LIMIT TRIPPED - trading halted",
                               f"{reason}\nNo new trades until you press 'Resume' in the dashboard "
                               "(or send /resume). Open positions remain protected by stop losses.")

    def _alert_unpriced(self, pos: dict, now: float) -> None:
        """A holding with no price means its stop loss is NOT being evaluated - say so loudly."""
        last = self._last_priced.setdefault(pos["id"], now)
        if now - last >= 60 and now - self._last_stale_alert.get(pos["id"], 0) >= 300:
            self._last_stale_alert[pos["id"]] = now
            self.notifier.send("risk", f"NO PRICE for {pos['symbol']} for {now - last:.0f}s",
                               "Its stop loss cannot be evaluated until a price source responds. "
                               "Consider selling manually from the dashboard.")

    async def _heartbeat(self) -> None:
        """Ping a dead-man's-switch URL so an outside service can alert if the bot stops."""
        url = self.secrets.heartbeat_url
        if not url or time.time() - self._last_heartbeat < 60:
            return
        self._last_heartbeat = time.time()
        try:
            await self.market.client.get(url, timeout=10)
        except Exception as exc:  # never let monitoring break trading
            log.warning("heartbeat failed: %s", exc)

    # ------------------------------------------------------------------ dashboard
    async def investigate(self, mint: str) -> dict[str, Any]:
        pairs = await self.market.pairs_for([mint])
        p = pairs.get(mint)
        if not p:
            return {"error": "no Solana SOL/USDC pair found for that mint"}
        a, safety = await self.research(p)
        ok, why = self.passes_filters(p)
        entry = self._radar_entry(p, a, safety)
        entry.update(passes_filters=ok, filter_reason=why, would_buy=ok and a.score >= self.settings.min_entry_score)
        self.radar[mint] = entry
        return entry

    def reset_paper(self) -> None:
        if not isinstance(self.broker, PaperBroker):
            raise ValueError("only available in paper mode")
        if self.store.open_positions():
            raise ValueError("close open positions first")
        self.broker.reset(self.settings.paper_starting_sol)
        self.store.set("peak_equity_sol", 0)
        self.notifier.send("info", "Paper wallet reset", f"{self.settings.paper_starting_sol} SOL", push=False)

    async def manual_buy(self, mint: str) -> dict[str, Any]:
        if self.risk.tripped:
            return {"error": f"risk lock active: {self.risk.trip_reason}"}
        pairs = await self.market.pairs_for([mint])
        p = pairs.get(mint)
        if not p:
            return {"error": "no pair found"}
        a, _ = await self.research(p)
        pid = await self.buy(p, a, manual=True)
        return {"position_id": pid} if pid else {"error": "buy failed - see events"}

    def snapshot(self) -> dict[str, Any]:
        now = time.time()
        open_pos = self.store.open_positions()
        for p in open_pos:
            px = p["last_price"] or p["entry_price"]
            value = p["qty"] * px
            p["value_usd"] = value
            p["unrealized_usd"] = value + p["realized_usd"] - p["cost_usd"]
            p["change_pct"] = (px - p["entry_price"]) / p["entry_price"] * 100 if p["entry_price"] else 0
            p["stop_price"] = p["entry_price"] * (1 - self.settings.stop_loss_pct / 100)
            p["reasons"] = json.loads(p["reasons"] or "[]")
        return {
            "mode": self.broker.mode,
            "enabled": self.settings.enabled,
            "risk": {"tripped": self.risk.tripped, "reason": self.risk.trip_reason,
                     "tripped_at": self.store.get("risk_tripped_at"),
                     "peak_equity": float(self.store.get("peak_equity_sol", 0) or 0) * self.sol_usd},
            "unresolved": self.store.unresolved_positions(),
            "account": {"sol_usd": self.sol_usd, "balance_sol": self.balance_sol, "equity_usd": self.equity_usd,
                        "deployed_usd": self.deployed_usd(), "trade_size_sol": self.trade_size_sol()},
            "stats": {"all": self.store.stats(), "today": self.store.stats(start_of_day_utc(now))},
            "positions": open_pos,
            "closed": self.store.closed_positions(100),
            "trades": self.store.trades(100),
            "events": self.store.events(80),
            "radar": sorted(self.radar.values(), key=lambda r: -r["score"])[:40],
            "last_scan": self.last_scan,
            "settings": self.settings.to_dict(),
            "channels": self.notifier.channels,
            "equity": self.store.equity_history(now - 7 * 86400),
            "server_time": now,
        }
