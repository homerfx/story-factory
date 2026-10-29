from __future__ import annotations

import os

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_env(path: str | None = None) -> None:
    """Load KEY=value lines from .env into the environment (without overwriting real env vars)."""
    path = path or os.path.join(ROOT, ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip().strip('"').strip("'")
            if key and value and key not in os.environ:
                os.environ[key] = value


def load_config(path: str | None = None) -> dict:
    path = path or os.path.join(ROOT, "config.yaml")
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    base = os.path.dirname(os.path.abspath(path))
    # Paths in the config are relative to the config file's folder.
    for key in ("state_path", "rules_path", "posted_path", "last_run_path"):
        if key in cfg and not os.path.isabs(cfg[key]):
            cfg[key] = os.path.join(base, cfg[key])
    cfg["exclude_paths"] = [p if os.path.isabs(p) else os.path.join(base, p) for p in cfg.get("exclude_paths", [])]
    return cfg
