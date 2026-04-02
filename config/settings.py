from __future__ import annotations

import os
from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from typing import Any

try:
    import yaml
except ModuleNotFoundError as exc:
    raise ModuleNotFoundError(
        "YAML config support requires the 'PyYAML' package. "
        "Install it with: pip install pyyaml"
    ) from exc


ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT_DIR / ".env"
CONFIG_FILE = ROOT_DIR / "config" / "config.yaml"

_SENSITIVE_KEYS = {
    "api_key",
    "plus_api_token",
    "app_key",
    "app_secret",
    "app_code",
    "email",
    "mailto",
    "password",
    "session_cookie",
}


def _load_dotenv(env_file: Path = ENV_FILE) -> None:
    if not env_file.exists():
        return

    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()

        if not key:
            continue

        if value and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]

        os.environ.setdefault(key, value)


def _inject_env_values(node: Any) -> Any:
    if isinstance(node, dict):
        resolved = {key: _inject_env_values(value) for key, value in node.items()}
        for key, env_name in list(resolved.items()):
            if key.endswith("_env") and isinstance(env_name, str) and env_name:
                target_key = key[:-4]
                resolved[target_key] = os.getenv(env_name)
        return resolved

    if isinstance(node, list):
        return [_inject_env_values(item) for item in node]

    return node


def _redact(node: Any) -> Any:
    if isinstance(node, dict):
        redacted: dict[str, Any] = {}
        for key, value in node.items():
            if key in _SENSITIVE_KEYS and value:
                redacted[key] = "***"
            else:
                redacted[key] = _redact(value)
        return redacted

    if isinstance(node, list):
        return [_redact(item) for item in node]

    return node


@lru_cache(maxsize=1)
def get_settings(config_file: str | Path = CONFIG_FILE) -> dict[str, Any]:
    _load_dotenv()

    config_path = Path(config_file)
    with config_path.open("r", encoding="utf-8") as fp:
        raw = yaml.safe_load(fp) or {}

    settings = _inject_env_values(raw)
    settings["paths"] = {
        "root_dir": str(ROOT_DIR),
        "env_file": str(ENV_FILE),
        "config_file": str(config_path),
    }
    return settings


def get_source_settings(source_name: str, config_file: str | Path = CONFIG_FILE) -> dict[str, Any]:
    settings = get_settings(config_file)
    return deepcopy(settings.get("sources", {}).get(source_name, {}))


def get_redacted_settings(config_file: str | Path = CONFIG_FILE) -> dict[str, Any]:
    return _redact(get_settings(config_file))


if __name__ == "__main__":
    import json

    print(json.dumps(get_redacted_settings(), ensure_ascii=False, indent=2))
