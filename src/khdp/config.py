"""Configuration loading for the KHDP connector.

KHDP authentication is identified by an ``app_id`` (UUID) registered
with KHDP and a ``redirect_url`` allowlisted for that app. Both values
are required for any login or token-refresh call.

Resolution order (highest priority first):

1. Environment variables: ``KHDP_*``
2. ``khdp.local.toml`` in the current working directory
3. ``$XDG_CONFIG_HOME/khdp/config.toml`` (or platform equivalent)
4. Built-in defaults pointing at the production KHDP API.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from platformdirs import user_config_dir

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib


# KHDP central API base — observed at https://khdp.net/_api.
DEFAULT_API_BASE = "https://khdp.net/_api"


@dataclass(frozen=True)
class Config:
    """Resolved configuration for the connector."""

    # KHDP app registration. ``app_id`` is a UUID issued by KHDP at app
    # registration time. ``redirect_url`` is the URL allowlisted for the
    # app; for the PKCE Loopback flow the CLI uses
    # ``http://127.0.0.1:<dynamic-port>/callback`` and the KHDP backend
    # matches IP-literal loopbacks ignoring port (RFC 8252 §7.3).
    app_id: str = ""
    redirect_url: str = ""
    # KHDP API base. Override for staging / on-prem deployments.
    api_base: str = DEFAULT_API_BASE
    # KHDP web URL the user's browser is sent to during PKCE login.
    # If empty, derived from ``api_base`` host (path = /external/oauth-login).
    authorize_url: str = ""
    # Where tokens go on disk. Defaults to platform user-config dir.
    token_dir: Path = field(default_factory=lambda: Path(user_config_dir("khdp")))
    # Use OS keychain via the optional ``keyring`` extra when available.
    use_keyring: bool = True

    extras: dict[str, Any] = field(default_factory=dict)


def _config_path_user() -> Path:
    return Path(user_config_dir("khdp")) / "config.toml"


def _config_path_local() -> Path:
    return Path.cwd() / "khdp.local.toml"


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    with path.open("rb") as fh:
        return tomllib.load(fh)


def _env_overrides() -> dict[str, Any]:
    mapping = {
        "app_id": "KHDP_APP_ID",
        "redirect_url": "KHDP_REDIRECT_URL",
        "api_base": "KHDP_API_BASE",
        "authorize_url": "KHDP_AUTHORIZE_URL",
    }
    out: dict[str, Any] = {}
    for key, env in mapping.items():
        if (val := os.environ.get(env)) is not None:
            out[key] = val
    if (token_dir := os.environ.get("KHDP_TOKEN_DIR")) is not None:
        out["token_dir"] = Path(token_dir).expanduser()
    if (use_kr := os.environ.get("KHDP_USE_KEYRING")) is not None:
        out["use_keyring"] = use_kr.lower() not in {"0", "false", "no", "off"}
    return out


def load_config(*, extra_path: Path | None = None) -> Config:
    """Resolve config from defaults + files + environment."""
    layers: list[dict[str, Any]] = [_read_toml(_config_path_user())]
    if extra_path is not None:
        layers.append(_read_toml(extra_path))
    layers.append(_read_toml(_config_path_local()))
    layers.append(_env_overrides())

    merged: dict[str, Any] = {}
    for layer in layers:
        merged.update(layer)

    if "token_dir" in merged and not isinstance(merged["token_dir"], Path):
        merged["token_dir"] = Path(merged["token_dir"]).expanduser()

    valid_fields = {f for f in Config.__dataclass_fields__ if f != "extras"}
    extras = {k: v for k, v in merged.items() if k not in valid_fields}
    primary = {k: v for k, v in merged.items() if k in valid_fields}

    return Config(extras=extras, **primary)
