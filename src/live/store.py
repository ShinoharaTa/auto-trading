"""ペーパートレードの状態ストア（SQLite）。

口座ごとの現金・建玉・指値待ち・約定ログ・資産スナップショットを保持。
スナップショットは Discord 通知が「1日/1週/2週前」の評価額を引くのに使う。
実発注は一切しない。すべて仮想。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from src.config import ROOT


class PaperStore:
    def __init__(self, db_path: str):
        p = Path(db_path)
        if not p.is_absolute():
            p = ROOT / p
        p.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(p)
        self.conn.row_factory = sqlite3.Row
        self._init()

    def _init(self) -> None:
        c = self.conn
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS account(
                name TEXT PRIMARY KEY, cash_jpy REAL, base_jpy REAL, started_at INTEGER);
            CREATE TABLE IF NOT EXISTS position(
                account TEXT, pair TEXT, units REAL, entry_price REAL, stop_price REAL,
                PRIMARY KEY(account, pair));
            CREATE TABLE IF NOT EXISTS pending(
                account TEXT, pair TEXT, side TEXT, limit_price REAL, placed_ts INTEGER,
                PRIMARY KEY(account, pair));
            CREATE TABLE IF NOT EXISTS cursor(
                account TEXT, pair TEXT, last_bar_ts INTEGER,
                PRIMARY KEY(account, pair));
            CREATE TABLE IF NOT EXISTS snapshot(
                account TEXT, ts INTEGER, equity_jpy REAL,
                PRIMARY KEY(account, ts));
            CREATE TABLE IF NOT EXISTS trade(
                id INTEGER PRIMARY KEY AUTOINCREMENT, account TEXT, pair TEXT,
                side TEXT, kind TEXT, price REAL, units REAL, ts INTEGER);
            CREATE TABLE IF NOT EXISTS fillstat(
                account TEXT PRIMARY KEY, placed INTEGER DEFAULT 0,
                filled INTEGER DEFAULT 0, missed INTEGER DEFAULT 0);
            """
        )
        c.commit()

    # --- account ---
    def ensure_account(self, name: str, base_jpy: float, now_ts: int) -> None:
        if self.conn.execute("SELECT 1 FROM account WHERE name=?", (name,)).fetchone() is None:
            self.conn.execute("INSERT INTO account VALUES (?,?,?,?)",
                              (name, base_jpy, base_jpy, now_ts))
            self.conn.execute("INSERT OR IGNORE INTO fillstat(account) VALUES (?)", (name,))
            self.conn.commit()

    def get_cash(self, name: str) -> float:
        return float(self.conn.execute(
            "SELECT cash_jpy FROM account WHERE name=?", (name,)).fetchone()[0])

    def set_cash(self, name: str, cash: float) -> None:
        self.conn.execute("UPDATE account SET cash_jpy=? WHERE name=?", (cash, name))
        self.conn.commit()

    def base_jpy(self, name: str) -> float:
        return float(self.conn.execute(
            "SELECT base_jpy FROM account WHERE name=?", (name,)).fetchone()[0])

    # --- position ---
    def get_position(self, account: str, pair: str) -> dict[str, Any] | None:
        r = self.conn.execute("SELECT * FROM position WHERE account=? AND pair=?",
                              (account, pair)).fetchone()
        return dict(r) if r else None

    def set_position(self, account: str, pair: str, units: float,
                     entry_price: float, stop_price: float) -> None:
        self.conn.execute("INSERT OR REPLACE INTO position VALUES (?,?,?,?,?)",
                          (account, pair, units, entry_price, stop_price))
        self.conn.commit()

    def clear_position(self, account: str, pair: str) -> None:
        self.conn.execute("DELETE FROM position WHERE account=? AND pair=?", (account, pair))
        self.conn.commit()

    def all_positions(self, account: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM position WHERE account=?", (account,)).fetchall()]

    # --- pending (maker 指値待ち) ---
    def get_pending(self, account: str, pair: str) -> dict[str, Any] | None:
        r = self.conn.execute("SELECT * FROM pending WHERE account=? AND pair=?",
                              (account, pair)).fetchone()
        return dict(r) if r else None

    def set_pending(self, account: str, pair: str, side: str,
                    limit_price: float, placed_ts: int) -> None:
        self.conn.execute("INSERT OR REPLACE INTO pending VALUES (?,?,?,?,?)",
                          (account, pair, side, limit_price, placed_ts))
        self.conn.commit()

    def clear_pending(self, account: str, pair: str) -> None:
        self.conn.execute("DELETE FROM pending WHERE account=? AND pair=?", (account, pair))
        self.conn.commit()

    # --- cursor (処理済みバー) ---
    def last_bar_ts(self, account: str, pair: str) -> int:
        r = self.conn.execute("SELECT last_bar_ts FROM cursor WHERE account=? AND pair=?",
                             (account, pair)).fetchone()
        return int(r[0]) if r else 0

    def set_last_bar_ts(self, account: str, pair: str, ts: int) -> None:
        self.conn.execute("INSERT OR REPLACE INTO cursor VALUES (?,?,?)", (account, pair, ts))
        self.conn.commit()

    # --- trade log / fill stats ---
    def add_trade(self, account: str, pair: str, side: str, kind: str,
                  price: float, units: float, ts: int) -> None:
        self.conn.execute(
            "INSERT INTO trade(account,pair,side,kind,price,units,ts) VALUES (?,?,?,?,?,?,?)",
            (account, pair, side, kind, price, units, ts))
        self.conn.commit()

    def bump_fillstat(self, account: str, field: str) -> None:
        assert field in ("placed", "filled", "missed")
        self.conn.execute(f"UPDATE fillstat SET {field}={field}+1 WHERE account=?", (account,))
        self.conn.commit()

    def fillstat(self, account: str) -> dict[str, Any]:
        r = self.conn.execute("SELECT * FROM fillstat WHERE account=?", (account,)).fetchone()
        return dict(r) if r else {"placed": 0, "filled": 0, "missed": 0}

    # --- snapshots ---
    def add_snapshot(self, account: str, ts: int, equity: float) -> None:
        self.conn.execute("INSERT OR REPLACE INTO snapshot VALUES (?,?,?)",
                          (account, ts, equity))
        self.conn.commit()

    def latest_equity(self, account: str) -> float | None:
        r = self.conn.execute(
            "SELECT equity_jpy FROM snapshot WHERE account=? ORDER BY ts DESC LIMIT 1",
            (account,)).fetchone()
        return float(r[0]) if r else None

    def equity_at_or_before(self, account: str, ts: int) -> float | None:
        """ts 以前で最も新しいスナップショット（無ければ最古、それも無ければ None）。"""
        r = self.conn.execute(
            "SELECT equity_jpy FROM snapshot WHERE account=? AND ts<=? ORDER BY ts DESC LIMIT 1",
            (account, ts)).fetchone()
        if r:
            return float(r[0])
        r = self.conn.execute(
            "SELECT equity_jpy FROM snapshot WHERE account=? ORDER BY ts ASC LIMIT 1",
            (account,)).fetchone()
        return float(r[0]) if r else None

    def accounts(self) -> list[str]:
        return [r[0] for r in self.conn.execute("SELECT name FROM account").fetchall()]
