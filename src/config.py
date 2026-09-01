"""Configuration and secret loading."""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent

DISCLAIMER = (
    "Monitoring/alerting tool built on free forecast-model data. "
    "NOT a summit go/no-go decision tool. "
    "Real expeditions must use a dedicated mountain forecaster."
)


ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def expand_env(value: Any) -> Any:
    """Recursively expand ${VAR} references in config values.

    This is what keeps personal data out of a public repository: config.yaml commits
    the *name* of a variable, never its value. An unset variable expands to an empty
    string, which the channels treat as "not configured" and skip -- deliberately
    safer than leaving a literal "${VAR}" to be handed to an SMTP or SMS API.
    """
    if isinstance(value, str):
        return ENV_PATTERN.sub(lambda m: os.environ.get(m.group(1), "").strip(), value)
    if isinstance(value, dict):
        return {k: expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_env(v) for v in value]
    return value


def load_config(path: Optional[Path] = None) -> Dict[str, Any]:
    """Read config.yaml, expanding ${VAR} references from the environment.

    Paths inside it are resolved against the repo root.
    """
    cfg_path = Path(path) if path else REPO_ROOT / "config.yaml"
    with open(cfg_path, "r", encoding="utf-8") as fh:
        cfg = expand_env(yaml.safe_load(fh))

    for section, key in (("storage", "db_path"), ("runtime", "log_path")):
        raw = cfg.get(section, {}).get(key)
        if raw and not os.path.isabs(raw):
            cfg[section][key] = str(REPO_ROOT / raw)
    return cfg


def load_env() -> None:
    """Load .env if present. Real environment variables always win."""
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return
    try:
        from dotenv import load_dotenv

        load_dotenv(env_path, override=False)
    except ImportError:  # dotenv is optional; parse the simple case ourselves
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def secret(name: str) -> Optional[str]:
    """Fetch a secret from the environment. Empty string counts as missing."""
    value = os.environ.get(name, "").strip()
    return value or None


def setup_logging(cfg: Dict[str, Any]) -> logging.Logger:
    runtime = cfg.get("runtime", {})
    level = getattr(logging, str(runtime.get("log_level", "INFO")).upper(), logging.INFO)
    handlers = [logging.StreamHandler()]

    log_path = runtime.get("log_path")
    if log_path:
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_path, encoding="utf-8"))

    logging.basicConfig(
        level=level,
        format="%(asctime)sZ %(levelname)-7s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )
    logging.Formatter.converter = __import__("time").gmtime
    return logging.getLogger("manaslu")
