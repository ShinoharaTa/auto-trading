"""ペア別の運用設定ストア（JSON）。

各ペアが独立した「戦略 × 時間足 × パラメータ × OOS成績 × enabled」を持つ。
チューナ[tune.py]が書き込み、ペーパー/ライブ層がこれを読んで「どのペアを・どの設定で
売買するか」を決める。enabled=False のペアは信頼性不足として売買対象から外す。

実体は state/pair_settings.json。秘密情報は含まないが、生成物なので git 管理外。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.config import ROOT

STORE = ROOT / "state" / "pair_settings.json"


def load_all() -> dict[str, Any]:
    if STORE.exists():
        return json.loads(STORE.read_text(encoding="utf-8"))
    return {}


def get(pair: str) -> dict[str, Any] | None:
    return load_all().get(pair)


def enabled_pairs() -> list[str]:
    """売買対象（enabled=True）のペア一覧。"""
    return [p for p, r in load_all().items() if r.get("enabled")]


def upsert(pair: str, record: dict[str, Any]) -> None:
    data = load_all()
    data[pair] = {**record,
                  "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    STORE.parent.mkdir(parents=True, exist_ok=True)
    STORE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
