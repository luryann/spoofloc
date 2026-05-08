from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import platformdirs
import tomli_w

APP_NAME = "spoofloc"

DEFAULTS: dict[str, Any] = {
    "device": {
        "default_udid": "",
        "ios_version_cache": {},
    },
    "tunnel": {
        "mode": "tunneld",
        "daemon_url": "http://127.0.0.1:49151/",
        "startup_timeout_s": 90,
        "preferred_connection_type": "auto",
        "auto_restart_on_failure": True,
        "reconnect_timeout_s": 10,
    },
    "web": {
        "host": "127.0.0.1",
        "port": 4780,
        "auto_open_browser": True,
    },
    "geocoding": {
        "user_agent": "spoofloc/0.1.0 (personal-use)",
        "max_cache_entries": 5000,
    },
    "route": {
        "default_speed_mph": 30.0,
        "tick_hz": 2.0,
        "loop": False,
    },
    "favorites": {},
}


def config_path() -> Path:
    return Path(platformdirs.user_config_dir(APP_NAME)) / "config.toml"


def cache_dir() -> Path:
    d = Path(platformdirs.user_cache_dir(APP_NAME))
    d.mkdir(parents=True, exist_ok=True)
    return d


def _deep_merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def load() -> dict[str, Any]:
    path = config_path()
    if not path.exists():
        return _deep_merge({}, DEFAULTS)
    with path.open("rb") as f:
        user = tomllib.load(f)
    return _deep_merge(DEFAULTS, user)


def save(cfg: dict[str, Any]) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as f:
        tomli_w.dump(cfg, f)


def set_key(key: str, value: str) -> None:
    cfg = load()
    parts = key.split(".")
    node = cfg
    for part in parts[:-1]:
        if part not in node or not isinstance(node[part], dict):
            node[part] = {}
        node = node[part]
    last = parts[-1]
    existing = node.get(last)
    if isinstance(existing, bool):
        node[last] = value.lower() in ("1", "true", "yes")
    elif isinstance(existing, int):
        node[last] = int(value)
    elif isinstance(existing, float):
        node[last] = float(value)
    else:
        node[last] = value
    save(cfg)


def get_key(key: str) -> Any:
    cfg = load()
    parts = key.split(".")
    node: Any = cfg
    for part in parts:
        if not isinstance(node, dict) or part not in node:
            raise KeyError(f"Config key not found: {key}")
        node = node[part]
    return node
