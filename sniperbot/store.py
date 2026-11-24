"""SQLite persistence: settings, positions, trades, events, equity history."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mint TEXT NOT NULL,
    symbol TEXT,
    name TEXT,
    pair_address TEXT,
    strategy TEXT,
    score REAL,
    reasons TEXT,
    entry_price REAL NOT NULL,
    qty REAL NOT NULL,
    initial_qty REAL NOT NULL,
    cost_sol REAL NOT NULL,
    cost_usd REAL NOT NULL,
    peak_price REAL NOT NULL,
    last_price REAL,
    tp1_done INTEGER DEFAULT 0,
    realized_sol REAL DEFAULT 0,
    realized_usd REAL DEFAULT 0,
    opened_at REAL NOT NULL,
    closed_at REAL,
    status TEXT NOT NULL DEFAULT 'open',
    exit_reason TEXT,
    mode TEXT
);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id INTEGER,
    ts REAL NOT NULL,
    side TEXT NOT NULL,
    mint TEXT NOT NULL,
    symbol TEXT,
    price REAL,
    qty REAL,
    sol REAL,
    usd REAL,
    pnl_usd REAL,
    reason TEXT,
    tx TEXT,
    mode TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    level TEXT NOT NULL,
    title TEXT NOT NULL,
    message TEXT
);
CREATE TABLE IF NOT EXISTS equity (ts REAL PRIMARY KEY, equity_usd REAL NOT NULL);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    mint TEXT NOT NULL,
    symbol TEXT,
    pair_address TEXT,
    action TEXT NOT NULL,
    note TEXT,
    strategy TEXT,
    score REAL,
    scores TEXT,
    reasons TEXT,
    metrics TEXT,
    pair TEXT,
    safety TEXT
);
CREATE TABLE IF NOT EXISTS price_ticks (
    ts REAL NOT NULL,
    position_id INTEGER NOT NULL,
    price REAL,
    liquidity_usd REAL,
    source TEXT
);
CREATE INDEX IF NOT EXISTS idx_ticks_pos ON price_ticks(position_id, ts);
CREATE INDEX IF NOT EXISTS idx_decisions_ts ON decisions(ts);
CREATE INDEX IF NOT EXISTS idx_pos_status ON positions(status);
CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(ts);
"""


class Store:
    def __init__(self, path: str = "sniperbot.db") -> None:
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(positions)")}
        if "entry_liq" not in cols:  # migration: pool liquidity at entry (detects liquidity pulls)
            self.conn.execute("ALTER TABLE positions ADD COLUMN entry_liq REAL")
        self.lock = threading.Lock()

    # -- helpers ---------------------------------------------------------
    def _exec(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self.lock:
            return self.conn.execute(sql, params)

    def _rows(self, sql: str, params: tuple | dict = ()) -> list[dict[str, Any]]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    # -- key/value -------------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        rows = self._rows("SELECT value FROM kv WHERE key=?", (key,))
        return json.loads(rows[0]["value"]) if rows else default

    def set(self, key: str, value: Any) -> None:
        self._exec("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (key, json.dumps(value)))

    # -- positions -------------------------------------------------------
    def open_position(self, **p: Any) -> int:
        p.setdefault("opened_at", time.time())
        p.setdefault("peak_price", p["entry_price"])
        p.setdefault("last_price", p["entry_price"])
        p.setdefault("initial_qty", p["qty"])
        cols = ",".join(p)
        marks = ",".join(f":{k}" for k in p)
        return self._exec(f"INSERT INTO positions({cols}) VALUES({marks})", p).lastrowid

    def update_position(self, pid: int, **fields_: Any) -> None:
        sets = ",".join(f"{k}=:{k}" for k in fields_)
        self._exec(f"UPDATE positions SET {sets} WHERE id=:_id", {**fields_, "_id": pid})

    def open_positions(self) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM positions WHERE status='open' ORDER BY opened_at")

    def unresolved_positions(self) -> list[dict[str, Any]]:
        """Buys whose outcome is not known yet (process died mid-buy, or a live buy errored)."""
        return self._rows("SELECT * FROM positions WHERE status IN ('pending','unknown') ORDER BY opened_at")

    def position(self, pid: int) -> dict[str, Any] | None:
        rows = self._rows("SELECT * FROM positions WHERE id=?", (pid,))
        return rows[0] if rows else None

    def closed_positions(self, limit: int = 200) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM positions WHERE status='closed' ORDER BY closed_at DESC LIMIT ?", (limit,))

    def recently_traded(self, mint: str, since: float) -> bool:
        return bool(self._rows("SELECT 1 FROM positions WHERE mint=? AND opened_at>=? LIMIT 1", (mint, since)))

    # -- trades ----------------------------------------------------------
    def add_trade(self, **t: Any) -> int:
        t.setdefault("ts", time.time())
        cols = ",".join(t)
        marks = ",".join(f":{k}" for k in t)
        return self._exec(f"INSERT INTO trades({cols}) VALUES({marks})", t).lastrowid

    def trades(self, limit: int = 200) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM trades ORDER BY ts DESC LIMIT ?", (limit,))

    # -- research logs ---------------------------------------------------
    def add_decision(self, **d: Any) -> None:
        d.setdefault("ts", time.time())
        cols = ",".join(d)
        self._exec(f"INSERT INTO decisions({cols}) VALUES({','.join(':' + k for k in d)})", d)

    def decisions(self, limit: int = 200) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM decisions ORDER BY id DESC LIMIT ?", (limit,))

    def add_ticks(self, rows: list[tuple]) -> None:
        with self.lock:
            self.conn.executemany("INSERT INTO price_ticks VALUES(?,?,?,?,?)", rows)

    def ticks(self, position_id: int) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM price_ticks WHERE position_id=? ORDER BY ts", (position_id,))

    # -- events ----------------------------------------------------------
    def add_event(self, level: str, title: str, message: str = "") -> None:
        self._exec("INSERT INTO events(ts,level,title,message) VALUES(?,?,?,?)",
                   (time.time(), level, title, message))
        self._exec("DELETE FROM events WHERE id < (SELECT MAX(id) - 2000 FROM events)")

    def events(self, limit: int = 100) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))

    # -- equity ----------------------------------------------------------
    def add_equity(self, equity_usd: float) -> None:
        self._exec("INSERT OR REPLACE INTO equity(ts,equity_usd) VALUES(?,?)", (time.time(), equity_usd))

    def equity_history(self, since: float = 0, max_points: int = 500) -> list[dict[str, Any]]:
        rows = self._rows("SELECT ts, equity_usd FROM equity WHERE ts>=? ORDER BY ts", (since,))
        if len(rows) > max_points:
            step = len(rows) / max_points
            rows = [rows[int(i * step)] for i in range(max_points)] + [rows[-1]]
        return rows

    # -- stats -----------------------------------------------------------
    def stats(self, since: float = 0) -> dict[str, Any]:
        rows = self._rows(
            "SELECT realized_usd, cost_usd FROM positions WHERE status='closed' AND closed_at>=?", (since,))
        pnl = [r["realized_usd"] - r["cost_usd"] for r in rows]
        wins = [x for x in pnl if x > 0]
        losses = [x for x in pnl if x <= 0]
        return {
            "trades": len(pnl),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": (len(wins) / len(pnl) * 100) if pnl else 0.0,
            "realized_pnl_usd": sum(pnl),
            "gross_profit_usd": sum(wins),
            "gross_loss_usd": sum(losses),
            "avg_win_usd": (sum(wins) / len(wins)) if wins else 0.0,
            "avg_loss_usd": (sum(losses) / len(losses)) if losses else 0.0,
            "best_usd": max(pnl) if pnl else 0.0,
            "worst_usd": min(pnl) if pnl else 0.0,
            "profit_factor": (sum(wins) / abs(sum(losses))) if losses and sum(losses) else None,
        }

    def consecutive_losses(self) -> int:
        rows = self._rows("SELECT realized_usd, cost_usd FROM positions WHERE status='closed' ORDER BY closed_at DESC LIMIT 50")
        n = 0
        for r in rows:
            if r["realized_usd"] - r["cost_usd"] <= 0:
                n += 1
            else:
                break
        return n
