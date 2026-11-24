"""Bot configuration.

Two layers:
  * ``Secrets``  - read once from environment variables / .env (keys, tokens).
  * ``Settings`` - tunable trading parameters, editable live from the dashboard
                   and persisted in the database.
"""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path
from typing import Any


def load_dotenv(path: str | os.PathLike = ".env") -> None:
    """Minimal .env loader (no extra dependency). Existing env vars win."""
    p = Path(path)
    if not p.exists():
        return
    for raw in p.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


@dataclass
class Secrets:
    dashboard_token: str = ""
    host: str = "127.0.0.1"  # local only by default; use a tunnel for phone access
    port: int = 8000
    db_path: str = "sniperbot.db"
    mode: str = "paper"  # paper | live
    solana_private_key: str = ""
    solana_rpc_url: str = "https://api.mainnet-beta.solana.com"
    jupiter_api_key: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    discord_webhook_url: str = ""
    ntfy_topic: str = ""
    ntfy_server: str = "https://ntfy.sh"
    # live-trading interlocks: MODE=live alone is not enough
    live_confirm: str = ""
    live_max_trade_sol: float = 0.0     # hard cap per buy, cannot be raised from the dashboard
    live_max_deployed_sol: float = 0.0  # hard cap on total SOL in open positions
    heartbeat_url: str = ""             # optional dead-man's-switch ping (e.g. healthchecks.io)

    LIVE_CONFIRM_PHRASE = "I_ACCEPT_REAL_FUNDS_RISK"

    def live_problems(self) -> list[str]:
        """Reasons live trading must not start (empty list = interlocks satisfied)."""
        probs = []
        if not self.solana_private_key:
            probs.append("SOLANA_PRIVATE_KEY is not set")
        if self.live_confirm != self.LIVE_CONFIRM_PHRASE:
            probs.append(f"LIVE_TRADING_CONFIRM must equal {self.LIVE_CONFIRM_PHRASE}")
        if self.live_max_trade_sol <= 0:
            probs.append("LIVE_MAX_TRADE_SOL must be > 0")
        if self.live_max_deployed_sol <= 0:
            probs.append("LIVE_MAX_DEPLOYED_SOL must be > 0")
        if not self.dashboard_token or len(self.dashboard_token) < 16:
            probs.append("DASHBOARD_TOKEN must be set and at least 16 characters")
        return probs

    @classmethod
    def from_env(cls) -> "Secrets":
        e = os.environ.get
        return cls(
            dashboard_token=e("DASHBOARD_TOKEN", ""),
            host=e("HOST", "127.0.0.1"),
            port=int(e("PORT", "8000")),
            db_path=e("DB_PATH", "sniperbot.db"),
            mode=e("MODE", "paper").lower(),
            solana_private_key=e("SOLANA_PRIVATE_KEY", ""),
            solana_rpc_url=e("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com"),
            jupiter_api_key=e("JUPITER_API_KEY", ""),
            telegram_bot_token=e("TELEGRAM_BOT_TOKEN", ""),
            telegram_chat_id=e("TELEGRAM_CHAT_ID", ""),
            discord_webhook_url=e("DISCORD_WEBHOOK_URL", ""),
            ntfy_topic=e("NTFY_TOPIC", ""),
            ntfy_server=e("NTFY_SERVER", "https://ntfy.sh"),
            live_confirm=e("LIVE_TRADING_CONFIRM", ""),
            live_max_trade_sol=float(e("LIVE_MAX_TRADE_SOL", "0") or 0),
            live_max_deployed_sol=float(e("LIVE_MAX_DEPLOYED_SOL", "0") or 0),
            heartbeat_url=e("HEARTBEAT_URL", ""),
        )


@dataclass
class Settings:
    # --- Master switch -------------------------------------------------
    enabled: bool = False  # bot starts OFF; turn on from dashboard

    # --- Position sizing -----------------------------------------------
    trade_size: float = 25.0          # amount per trade ...
    trade_unit: str = "USD"           # ... in "USD" or "SOL"
    max_capital_usd: float = 250.0    # total USD the bot may have deployed at once
    max_open_positions: int = 5
    paper_starting_sol: float = 10.0  # paper wallet starting balance

    # --- Exits -----------------------------------------------------------
    stop_loss_pct: float = 10.0        # hard stop loss (% below entry)
    take_profit_pct: float = 40.0      # sell 50% here ...
    take_profit_2_pct: float = 100.0   # ... and the rest here
    trailing_stop_pct: float = 15.0    # trail from peak once in profit
    trailing_activate_pct: float = 15.0
    max_hold_minutes: int = 240
    use_signal_exits: bool = True      # exit early when chart turns bearish

    # --- Entry filters ---------------------------------------------------
    min_liquidity_usd: float = 15000
    max_liquidity_usd: float = 5_000_000
    min_volume_5m_usd: float = 2000
    min_pair_age_minutes: float = 3
    max_pair_age_hours: float = 72
    min_buy_sell_ratio: float = 1.1
    max_fdv_usd: float = 50_000_000
    require_safety_check: bool = True  # RugCheck: mint/freeze authority, LP, holders
    max_rugcheck_score: int = 5000     # rugcheck "score" (higher = riskier)
    min_entry_score: float = 65.0      # 0-100 combined signal score to buy
    enable_early: bool = True
    enable_momentum: bool = True
    enable_dip: bool = True
    enable_breakout: bool = True
    slippage_bps: int = 300            # 3% max slippage on swaps
    priority_fee_lamports: int = 100_000

    # --- Risk kill switch -------------------------------------------------
    max_daily_loss_usd: float = 75.0     # halt when today's realized loss exceeds this
    max_drawdown_pct: float = 25.0       # halt when equity falls this % from its peak
    max_consecutive_losses: int = 5      # halt after N losing trades in a row
    cooldown_minutes_after_loss: int = 0  # optional pause after each loss

    # --- Loop timing -------------------------------------------------------
    scan_interval_sec: int = 30
    monitor_interval_sec: int = 5

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def update(self, data: dict[str, Any]) -> list[str]:
        """Apply a partial update with type coercion + validation (all-or-nothing).

        Returns the keys that changed. Raises ValueError and leaves settings untouched on bad input.
        """
        names = {f.name for f in fields(self)}
        candidate = replace(self)
        changed: list[str] = []
        for key, value in data.items():
            if key not in names:
                continue
            current = getattr(self, key)
            try:
                if isinstance(current, bool):
                    new = value if isinstance(value, bool) else str(value).lower() in ("1", "true", "yes", "on")
                elif isinstance(current, int):
                    new = int(float(value))
                elif isinstance(current, float):
                    new = float(value)
                else:
                    new = str(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Invalid value for {key}: {value!r}") from exc
            setattr(candidate, key, new)
        candidate.validate()
        for key in names:
            if getattr(candidate, key) != getattr(self, key):
                setattr(self, key, getattr(candidate, key))
                changed.append(key)
        return changed

    def validate(self) -> None:
        self.trade_unit = self.trade_unit.upper()
        if self.trade_unit not in ("USD", "SOL"):
            raise ValueError("trade_unit must be USD or SOL")
        if self.trade_size <= 0:
            raise ValueError("trade_size must be > 0")
        if not 0.5 <= self.stop_loss_pct <= 90:
            raise ValueError("stop_loss_pct must be between 0.5 and 90")
        if self.max_open_positions < 1:
            raise ValueError("max_open_positions must be >= 1")
        if self.max_capital_usd <= 0:
            raise ValueError("max_capital_usd must be > 0")
        if self.scan_interval_sec < 5 or self.monitor_interval_sec < 1:
            raise ValueError("intervals too small")
        if not 0 <= self.min_entry_score <= 100:
            raise ValueError("min_entry_score must be 0-100")
