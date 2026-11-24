"""Risk management.

* Per-position exit rules: hard stop loss (default 10%), two take-profit levels,
  trailing stop from the peak, max hold time.
* Account-level kill switch ("risk command"): trips when the bot is in too deep a
  hole - daily loss limit, max drawdown from peak equity, or a losing streak.
  Once tripped, NO new trades open until a human resets it from the dashboard
  or Telegram. Open positions keep being protected by their stop losses.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone

from .config import Settings
from .store import Store


@dataclass
class ExitDecision:
    fraction: float  # 0 = hold, 0.5 = sell half, 1 = sell all
    reason: str = ""


def evaluate_exit(pos: dict, price: float, s: Settings, now: float | None = None) -> ExitDecision:
    now = now or time.time()
    entry = pos["entry_price"]
    if entry <= 0 or price <= 0:
        return ExitDecision(0)
    change = (price - entry) / entry * 100
    peak = max(pos["peak_price"], price)
    from_peak = (price - peak) / peak * 100

    if change <= -s.stop_loss_pct:
        return ExitDecision(1, f"stop loss {change:.1f}% (limit -{s.stop_loss_pct:g}%)")
    if change >= s.take_profit_2_pct:
        return ExitDecision(1, f"take profit 2 +{change:.1f}%")
    if not pos["tp1_done"] and change >= s.take_profit_pct:
        return ExitDecision(0.5, f"take profit 1 +{change:.1f}% (selling half)")
    peak_gain = (peak - entry) / entry * 100
    if peak_gain >= s.trailing_activate_pct and from_peak <= -s.trailing_stop_pct:
        return ExitDecision(1, f"trailing stop {from_peak:.1f}% from peak (+{peak_gain:.0f}% max)")
    if pos["tp1_done"] and price <= entry:
        return ExitDecision(1, "break-even stop after TP1")
    held_min = (now - pos["opened_at"]) / 60
    if held_min >= s.max_hold_minutes:
        return ExitDecision(1, f"max hold time {held_min:.0f}m")
    return ExitDecision(0)


def start_of_day_utc(now: float | None = None) -> float:
    d = datetime.fromtimestamp(now or time.time(), tz=timezone.utc)
    return d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


class RiskManager:
    def __init__(self, store: Store) -> None:
        self.store = store

    @property
    def tripped(self) -> bool:
        return bool(self.store.get("risk_tripped", False))

    @property
    def trip_reason(self) -> str:
        return self.store.get("risk_reason", "")

    def trip(self, reason: str) -> None:
        self.store.set("risk_tripped", True)
        self.store.set("risk_reason", reason)
        self.store.set("risk_tripped_at", time.time())

    def reset(self, equity_sol: float) -> None:
        self.store.set("risk_tripped", False)
        self.store.set("risk_reason", "")
        # fresh baselines so the same hole doesn't immediately re-trip it
        self.store.set("peak_equity_sol", equity_sol)
        self.store.set("risk_reset_at", time.time())

    def update_peak(self, equity_sol: float) -> float:
        peak = float(self.store.get("peak_equity_sol", 0) or 0)
        if equity_sol > peak:
            peak = equity_sol
            self.store.set("peak_equity_sol", peak)
        return peak

    def check(self, equity_sol: float, s: Settings) -> str | None:
        """Drawdown is measured in SOL so SOL's own price moves neither trip nor mask it."""
        """Return a reason string if the kill switch should trip."""
        if self.tripped:
            return None
        since = max(start_of_day_utc(), float(self.store.get("risk_reset_at", 0) or 0))
        daily = self.store.stats(since)["realized_pnl_usd"]
        if s.max_daily_loss_usd > 0 and daily <= -s.max_daily_loss_usd:
            return f"daily loss ${-daily:.2f} hit limit ${s.max_daily_loss_usd:.2f}"
        peak = self.update_peak(equity_sol)
        if peak > 0 and s.max_drawdown_pct > 0:
            dd = (equity_sol - peak) / peak * 100
            if dd <= -s.max_drawdown_pct:
                return f"drawdown {dd:.1f}% from peak {peak:.3f} SOL (limit -{s.max_drawdown_pct:g}%)"
        if s.max_consecutive_losses > 0:
            reset_at = float(self.store.get("risk_reset_at", 0) or 0)
            streak = self._losses_since(reset_at)
            if streak >= s.max_consecutive_losses:
                return f"{streak} losing trades in a row (limit {s.max_consecutive_losses})"
        return None

    def _losses_since(self, ts: float) -> int:
        n = 0
        for p in self.store.closed_positions(100):
            if p["closed_at"] < ts:
                break
            if p["realized_usd"] - p["cost_usd"] <= 0:
                n += 1
            else:
                break
        return n

    def in_cooldown(self, s: Settings) -> bool:
        if s.cooldown_minutes_after_loss <= 0:
            return False
        last = self.store.closed_positions(1)
        if not last or last[0]["realized_usd"] - last[0]["cost_usd"] > 0:
            return False
        return time.time() - last[0]["closed_at"] < s.cooldown_minutes_after_loss * 60
