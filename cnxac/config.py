"""Reads a config folder's env.yaml, and the credentials.yaml section it names.

Credential values are never printed: error messages name the file, the environment and
the missing keys only, and YAML errors report a line number without the line itself.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

import yaml

TOKEN_URL = "https://sso.common.cloud.hpe.com/as/token.oauth2"
PLACEHOLDER = "CHANGE_ME"
ENV_KEYS = {"environment", "protected", "ignore"}


class ConfigError(Exception):
    """A config folder file is missing or invalid."""


@dataclass(frozen=True)
class Credentials:
    base_url: str
    client_id: str
    client_secret: str = field(repr=False)
    token_url: str = TOKEN_URL


@dataclass(frozen=True)
class EnvConfig:
    name: str                         # the credentials.yaml section
    protected: bool = False
    ignore: tuple[str, ...] = ()

    def ignored(self, name: str) -> bool:
        """True if a name matches one of the env's ignore globs."""
        return any(fnmatchcase(name, glob) for glob in self.ignore)


def load_yaml(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8-sig"))   # some Windows editors add a byte order mark
    except UnicodeDecodeError:   # e.g. UTF-16, from Windows PowerShell 5.1's echo > file
        raise ConfigError(f"{path} isn't UTF-8 text; save it as UTF-8") from None
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        where = f" (line {mark.line + 1})" if mark is not None else ""
        raise ConfigError(f"{path} isn't valid YAML{where}") from None


def load_env(config_dir: Path) -> EnvConfig:
    """The folder's env.yaml, which every config folder has."""
    path = config_dir / "env.yaml"
    if not path.is_file():
        raise ConfigError(f"{path} not found. A config folder needs an env.yaml that names its environment.")
    data = load_yaml(path) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must be a mapping")
    unknown = sorted(set(data) - ENV_KEYS)
    if unknown:
        raise ConfigError(f"{path}: unknown keys {', '.join(unknown)}")
    name = data.get("environment")
    protected = data.get("protected", False)
    ignore = data.get("ignore") or []
    if not isinstance(name, str) or not name:
        raise ConfigError(f"{path}: environment must name a section of credentials.yaml")
    if not isinstance(protected, bool):
        raise ConfigError(f"{path}: protected must be true or false")
    if not isinstance(ignore, list) or not all(isinstance(g, str) for g in ignore):
        raise ConfigError(f"{path}: ignore must be a list of name globs")
    return EnvConfig(name, protected, tuple(ignore))


def credentials_path(config_dir: Path) -> Path | None:
    """credentials.yaml in the config folder, or else the folder above it."""
    for folder in (config_dir, config_dir.resolve().parent):
        if (folder / "credentials.yaml").is_file():
            return folder / "credentials.yaml"
    return None


def load_credentials(config_dir: Path, env: str) -> Credentials:
    path = credentials_path(config_dir)
    if path is None:
        raise ConfigError(f"no credentials.yaml in {config_dir} or the folder above it. "
                          "Copy credentials.example.yaml there and fill it in.")
    data = load_yaml(path) or {}
    entry = data.get(env) if isinstance(data, dict) else None
    if not isinstance(entry, dict):
        raise ConfigError(f"{path} has no '{env}' section")
    missing = [k for k in ("base_url", "client_id", "client_secret") if entry.get(k) in (None, "", PLACEHOLDER)]
    if missing:
        raise ConfigError(f"'{env}' in {path} is missing {', '.join(missing)}")
    return Credentials(
        base_url=str(entry["base_url"]),
        client_id=str(entry["client_id"]),
        client_secret=str(entry["client_secret"]),
        token_url=str(entry.get("token_url") or TOKEN_URL),
    )
