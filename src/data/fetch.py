"""bitbank の 1分足ローソク足を日別に取得して SQLite に貯める（差分追記）。

bitbank public API（認証不要）:
  GET https://public.bitbank.cc/{pair}/candlestick/1min/{YYYYMMDD}
レスポンスの ohlcv は [open, high, low, close, volume, timestamp_ms] の配列（値は文字列）。

使い方:
  python -m src.data.fetch                 # config の start_date 〜 今日
  python -m src.data.fetch --start 20240601 --end 20240630
"""
from __future__ import annotations

import argparse
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

from src.config import ROOT, load

BASE = "https://public.bitbank.cc"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "auto-trading/0.1"})


def _connect(db_path: str) -> sqlite3.Connection:
    p = Path(db_path)
    if not p.is_absolute():
        p = ROOT / p
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS candles (
            pair   TEXT    NOT NULL,
            ts     INTEGER NOT NULL,   -- ローソク開始時刻(ms, UTC)
            open   REAL    NOT NULL,
            high   REAL    NOT NULL,
            low    REAL    NOT NULL,
            close  REAL    NOT NULL,
            volume REAL    NOT NULL,
            PRIMARY KEY (pair, ts)
        )
        """
    )
    return conn


def _fetched_days(conn: sqlite3.Connection, pair: str) -> set[str]:
    """既に1日分入っている日付(YYYYMMDD, UTC)。再取得スキップ用。"""
    rows = conn.execute(
        "SELECT DISTINCT strftime('%Y%m%d', ts/1000, 'unixepoch') FROM candles WHERE pair=?",
        (pair,),
    ).fetchall()
    return {r[0] for r in rows}


def fetch_day(pair: str, day: date) -> list[tuple]:
    ymd = day.strftime("%Y%m%d")
    url = f"{BASE}/{pair}/candlestick/1min/{ymd}"
    r = SESSION.get(url, timeout=15)
    r.raise_for_status()
    body = r.json()
    if body.get("success") != 1:
        # 未来日・データ無しは code 10000 系で返ることがある。空扱い。
        return []
    rows: list[tuple] = []
    for cs in body["data"]["candlestick"]:
        for o, h, l, c, v, ts in cs["ohlcv"]:
            rows.append((pair, int(ts), float(o), float(h), float(l), float(c), float(v)))
    return rows


def run(pair: str, db_path: str, start: date, end: date, pause: float = 0.3) -> int:
    conn = _connect(db_path)
    done = _fetched_days(conn, pair)
    today_utc = datetime.now(timezone.utc).date()
    inserted = 0
    day = start
    while day <= end:
        ymd = day.strftime("%Y%m%d")
        # 当日(UTC)はまだ確定していない足が混ざるので、過去日のみ「取得済み」スキップ対象
        if ymd in done and day < today_utc:
            day += timedelta(days=1)
            continue
        try:
            rows = fetch_day(pair, day)
        except requests.RequestException as e:
            print(f"[warn] {ymd}: {e} -- リトライせず継続")
            day += timedelta(days=1)
            continue
        if rows:
            conn.executemany(
                "INSERT OR REPLACE INTO candles VALUES (?,?,?,?,?,?,?)", rows
            )
            conn.commit()
            inserted += len(rows)
            print(f"[ok] {pair} {ymd}: {len(rows)} bars")
        day += timedelta(days=1)
        time.sleep(pause)  # 公開APIへの礼儀（レート制限回避）
    conn.close()
    return inserted


def main() -> None:
    cfg = load()
    d = cfg["data"]
    ap = argparse.ArgumentParser()
    ap.add_argument("--pair", default=d["pair"])
    ap.add_argument("--start", default=d.get("start_date") or "20240101")
    ap.add_argument("--end", default=None, help="YYYYMMDD。省略時は今日(UTC)")
    ap.add_argument("--db", default=d["db_path"])
    args = ap.parse_args()

    start = datetime.strptime(args.start, "%Y%m%d").date()
    end = (
        datetime.strptime(args.end, "%Y%m%d").date()
        if args.end
        else datetime.now(timezone.utc).date()
    )
    total = run(args.pair, args.db, start, end)
    print(f"done: {total} bars inserted/updated -> {args.db}")


if __name__ == "__main__":
    main()
