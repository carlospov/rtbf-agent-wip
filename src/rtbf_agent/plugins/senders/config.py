"""Local-only config resolution for `SMTPSender` (R12, R13, R14).

Mirrors `plugins/llm/config.py`'s resolution chain exactly (same rationale: no centralized backend
or third-party credential storage this project operates, per R12/R13; secrets never committed, per
R14):

1. Environment variables (`RTBF_SMTP_*`).
2. The same local, gitignored TOML config file the LLM config uses
   (`~/.config/rtbf-agent/config.toml` by default, override with `RTBF_AGENT_CONFIG_PATH`), under
   a `[smtp]` table — never a file inside this repository.

No shell-rc parsing, no piggybacking on another tool's config, no registry of hosted SMTP presets
(unlike `plugins/llm/registry.py`, there's no third-party-vendor default to fall back to here —
every field must be resolved from env vars or the config file, or resolution fails loud).
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "rtbf-agent" / "config.toml"
"""Same default file `plugins/llm/config.py` resolves against -- one local config file, multiple
`[section]`s. Never inside this repository; also listed in `.gitignore` patterns for anyone who
points `RTBF_AGENT_CONFIG_PATH` at a repo-local path instead."""


@dataclass(frozen=True)
class SMTPConfig:
    """A fully-resolved SMTP credential set (R14).

    `password` is `repr=False` so an accidental `repr()`/`str()` of this object (e.g. in a
    traceback, a debugger, or a stray `print`/log call) never surfaces the secret -- callers must
    still take care never to put it in an `Event.detail` or log line themselves, but this closes
    off the most common accidental-leak path at the type level.
    """

    host: str
    port: int
    username: str | None
    password: str | None = field(repr=False)
    from_address: str
    use_tls: bool = True


def _config_file_path() -> Path:
    override = os.environ.get("RTBF_AGENT_CONFIG_PATH")
    # `.expanduser()`: many non-shell contexts that set env vars (systemd units, Docker `ENV`, CI)
    # never expand `~` themselves, so a literal `~/...` override would otherwise be treated as a
    # relative directory named `~`, silently fail to resolve, and every credential would resolve
    # to missing with no indication why.
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


def resolve_smtp_config() -> SMTPConfig:
    """Resolve SMTP credentials from env vars, then the local config file -- nothing else.

    Each field independently prefers `RTBF_SMTP_*`, falling back to the `[smtp]` table in the local
    config file. Raises `LookupError` if `host`, `from_address`, or (when `username` is set)
    `password` is still missing after both strategies -- `username`/`password` may both be left
    unset for a relay that doesn't require authentication, but a `username` without a `password` is
    always a misconfiguration, never silently sent unauthenticated. Raises `ValueError` if `port`
    resolves to something that isn't a valid integer, or `use_tls` to something that isn't a valid
    boolean-ish string.
    """
    smtp_section = _load_config_file().get("smtp", {})
    if not isinstance(smtp_section, dict):
        raise ValueError(
            f"expected [smtp] section to be a table in {_config_file_path()}, "
            f"got {type(smtp_section).__name__}"
        )

    def _file_str(field_name: str) -> str | None:
        value = smtp_section.get(field_name)
        if value is None or isinstance(value, str):
            return value
        raise ValueError(
            f"expected {field_name!r} to be a string in [smtp] ({_config_file_path()}), "
            f"got {type(value).__name__}"
        )

    host = os.environ.get("RTBF_SMTP_HOST") or _file_str("host")
    from_address = os.environ.get("RTBF_SMTP_FROM_ADDRESS") or _file_str("from_address")
    username = os.environ.get("RTBF_SMTP_USERNAME") or _file_str("username")
    password = os.environ.get("RTBF_SMTP_PASSWORD") or _file_str("password")

    port_raw: Any = os.environ.get("RTBF_SMTP_PORT")
    if port_raw is None:
        port_raw = smtp_section.get("port")
    port = 587
    if port_raw is not None:
        # `bool` is an `int` subclass (`int(True) == 1`) and a TOML float like `587.5` truncates
        # silently under a bare `int(...)` -- both would produce a wrong port with no error, the
        # first landing an SMTP connection attempt on port 1 with no indication why.
        if isinstance(port_raw, bool):
            raise ValueError(f"expected 'port' to be an integer, got bool ({port_raw!r})")
        if isinstance(port_raw, float) and not port_raw.is_integer():
            raise ValueError(
                f"expected 'port' to be an integer, got non-integer float {port_raw!r}"
            )
        try:
            port = int(port_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"expected 'port' to be an integer, got {port_raw!r}") from exc

    use_tls_raw: Any = os.environ.get("RTBF_SMTP_USE_TLS")
    if use_tls_raw is None:
        use_tls_raw = smtp_section.get("use_tls", True)
    if isinstance(use_tls_raw, bool):
        use_tls = use_tls_raw
    elif isinstance(use_tls_raw, str):
        lowered = use_tls_raw.strip().lower()
        if lowered in ("true", "1", "yes", "on"):
            use_tls = True
        elif lowered in ("false", "0", "no", "off"):
            use_tls = False
        else:
            raise ValueError(f"expected 'use_tls' to be a boolean-ish value, got {use_tls_raw!r}")
    else:
        raise ValueError(f"expected 'use_tls' to be a boolean-ish value, got {use_tls_raw!r}")

    missing = [
        field_name
        for field_name, value in (("host", host), ("from_address", from_address))
        if not value
    ]
    if username and not password:
        missing.append("password")
    if missing:
        raise LookupError(
            f"incomplete SMTP config: missing {', '.join(missing)} "
            f"(set RTBF_SMTP_* env vars or add [smtp] to {_config_file_path()})"
        )
    assert isinstance(host, str)
    assert isinstance(from_address, str)

    return SMTPConfig(
        host=host,
        port=port,
        username=username,
        password=password,
        from_address=from_address,
        use_tls=use_tls,
    )
