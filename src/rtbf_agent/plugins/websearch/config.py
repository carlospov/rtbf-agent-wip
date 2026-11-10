"""Local-only config resolution for `SearXNGProvider` (R16, R18-style: no repo-embedded endpoint).

Mirrors `plugins/senders/config.py`'s resolution chain exactly (same rationale: no centralized
backend or third-party credential storage this project operates, per R12/R13's "your own X, never
a project-operated relay" principle, applied here to search the same way it applies to SMTP):

1. Environment variables (`RTBF_SEARXNG_*`).
2. The same local, gitignored TOML config file the LLM/SMTP config uses
   (`~/.config/rtbf-agent/config.toml` by default, override with `RTBF_AGENT_CONFIG_PATH`), under
   a `[websearch]` table — never a file inside this repository.

No shell-rc parsing, no piggybacking on another tool's config, no vendor registry (unlike
`plugins/llm/registry.py` — there is exactly one concrete `WebSearchProvider` adapter today, and
unlike an LLM vendor, a SearXNG instance has no meaningful "well-known preset" to fall back to: it
is, by design, whatever instance *you* point it at, most often one you run yourself).
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "rtbf-agent" / "config.toml"
"""Same default file `plugins/llm/config.py`/`plugins/senders/config.py` resolve against -- one
local config file, multiple `[section]`s. Never inside this repository; also listed in
`.gitignore` patterns for anyone who points `RTBF_AGENT_CONFIG_PATH` at a repo-local path instead.
"""


@dataclass(frozen=True)
class SearXNGConfig:
    """A fully-resolved `{base_url, api_key}` for one SearXNG instance.

    `api_key` is `repr=False` so an accidental `repr()`/`str()` of this object (e.g. in a
    traceback, a debugger, or a stray `print`/log call) never surfaces the secret -- callers must
    still take care never to put it in an `Event.detail` or log line themselves, but this closes
    off the most common accidental-leak path at the type level.
    """

    base_url: str
    api_key: str | None = field(default=None, repr=False)
    """Most SearXNG instances (including a self-hosted one) require no API key at all -- this
    exists only for a private instance placed behind an auth-requiring reverse proxy that expects
    a bearer token. Never set for the common case."""


def _config_file_path() -> Path:
    override = os.environ.get("RTBF_AGENT_CONFIG_PATH")
    # `.expanduser()`: many non-shell contexts that set env vars (systemd units, Docker `ENV`, CI)
    # never expand `~` themselves, so a literal `~/...` override would otherwise be treated as a
    # relative directory named `~`, silently fail to resolve, and every default would resolve to
    # missing with no indication why.
    return Path(override).expanduser() if override else DEFAULT_CONFIG_PATH


def _load_config_file() -> dict[str, Any]:
    """Load the local TOML config file, or `{}` if it doesn't exist. Never touches the network."""
    path = _config_file_path()
    if not path.is_file():
        return {}
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"invalid TOML in config file {path}: {exc}") from exc


def resolve_searxng_config() -> SearXNGConfig:
    """Resolve a SearXNG instance's config from env vars, then the local config file -- nothing
    else.

    `base_url` prefers `RTBF_SEARXNG_BASE_URL`, falling back to the `[websearch]` table's
    `base_url` in the local config file. Raises `LookupError` if `base_url` is still missing after
    both strategies -- unlike an LLM provider, there is no hosted default to fall back to; the
    whole point of this adapter is that you point it at an instance you trust (often one you run
    yourself). `api_key` is optional and resolved the same way, defaulting to `None`.

    Raises `ValueError` if the config file contains invalid TOML, if its `[websearch]` section
    exists but isn't a table, or if `base_url`/`api_key` is present but isn't a string.
    """
    websearch_section = _load_config_file().get("websearch", {})
    if not isinstance(websearch_section, dict):
        raise ValueError(
            f"expected [websearch] section to be a table in {_config_file_path()}, "
            f"got {type(websearch_section).__name__}"
        )

    def _file_str(field_name: str) -> str | None:
        value = websearch_section.get(field_name)
        if value is None or isinstance(value, str):
            return value
        raise ValueError(
            f"expected {field_name!r} to be a string in [websearch] ({_config_file_path()}), "
            f"got {type(value).__name__}"
        )

    base_url = os.environ.get("RTBF_SEARXNG_BASE_URL") or _file_str("base_url")
    api_key = os.environ.get("RTBF_SEARXNG_API_KEY") or _file_str("api_key")

    if not base_url:
        raise LookupError(
            "incomplete SearXNG config: missing base_url "
            f"(set RTBF_SEARXNG_BASE_URL or add [websearch] to {_config_file_path()})"
        )

    return SearXNGConfig(base_url=base_url, api_key=api_key)
