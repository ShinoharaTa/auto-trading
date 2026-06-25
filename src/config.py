"""config.toml を読む薄いラッパ。無ければ config.example.toml にフォールバック。"""
from __future__ import annotations

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # 3.9 / 3.10
    import tomli as tomllib

from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent


def load() -> dict[str, Any]:
    for name in ("config.toml", "config.example.toml"):
        p = ROOT / name
        if p.exists():
            with p.open("rb") as f:
                return tomllib.load(f)
    raise FileNotFoundError("config.toml も config.example.toml も見つかりません")
