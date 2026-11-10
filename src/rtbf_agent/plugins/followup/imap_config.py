"""Local-only config resolution for `ImapReplyReader` (issue #102; R12, R13, R14).

Mirrors `plugins/senders/config.py`'s `resolve_smtp_config()` exactly (same rationale: no
centralized backend or third-party credential storage this project operates, per R12/R13; secrets
never committed, per R14) -- this repo's established convention is for each plugin's `config.py` to
duplicate this small resolution helper rather than share one across plugins (see
`plugins/llm/config.py`/`plugins/websearch/config.py`/`plugins/senders/config.py`, each with its own
`DEFAULT_CONFIG_PATH`/`_config_file_path()`), so this module follows that same shape rather than
importing a sibling plugin's private helpers:

1. Environment variables (`RTBF_IMAP_*`).
2. The same local, gitignored TOML config file the other plugins use
   (`~/.config/rtbf-agent/config.toml` by default, override with `RTBF_AGENT_CONFIG_PATH`), under a
   new `[imap]` table -- never a file inside this repository.

This exists for issue #102: a user without a Gmail OAuth app (or on a different mail provider
entirely) has no way to get follow-up replies detected today, since `GmailReplyReader`
(`plugins/followup/gmail_reader.py`) is the only production `MailboxReader`. `ImapReplyReader`
(`plugins/followup/imap_reader.py`) is the alternative this config resolves credentials for --
authenticated over plain IMAP with a username/password, no OAuth handshake at all.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "rtbf-agent" / "config.toml"
"""Same default file every other plugin's `config.py` resolves against -- one local config file,
multiple `[section]`s. Never inside this repository; also listed in `.gitignore` patterns for
anyone who points `RTBF_AGENT_CONFIG_PATH` at a repo-local path instead."""

_DEFAULT_PORT = 993
"""IMAP-over-TLS's standard port (`imaplib.IMAP4_SSL`'s own default) -- this reader is TLS-only by
design (see `ImapConfig`'s docstring), so there is no plaintext-IMAP default (port 143) to fall
back to."""


@dataclass(frozen=True)
class ImapConfig:
    """A fully-resolved IMAP credential set (R14), for a TLS-only (`imaplib.IMAP4_SSL`) connection.

    `password` is `repr=False` so an accidental `repr()`/`str()` of this object (e.g. in a
    traceback, a debugger, or a stray `print`/log call) never surfaces the secret -- callers must
    still take care never to put it in an `Event.detail` or log line themselves, but this closes
    off the most common accidental-leak path at the type level. Mirrors `SMTPConfig`
    (`plugins/senders/config.py`) exactly for the same reason.

    There is deliberately no `use_tls`/plaintext option here (unlike `SMTPConfig`): this reader
    only ever connects via `imaplib.IMAP4_SSL`, never the plaintext `imaplib.IMAP4` -- an IMAP
    password is a real credential and this repo does not offer a config knob that would send it in
    the clear.
    """

    host: str
    port: int
    username: str
    password: str = field(repr=False)


def _config_file_path() -> Path:
    override = os.environ.get("RTBF_AGENT_CONFIG_PATH")
    # `.expanduser()`: many non-shell contexts that set env vars (systemd units, Docker `ENV`, CI)
    # never expand `~` themselves, so a literal `~/...` override would otherwise be treated as a
    # relative directory named `~`, silently fail to resolve, and every credential would resolve to
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


def resolve_imap_config() -> ImapConfig:
    """Resolve IMAP credentials from env vars, then the local config file -- nothing else.

    Each field independently prefers `RTBF_IMAP_*`, falling back to the `[imap]` table in the local
    config file. Raises `LookupError` naming every still-missing required field (`host`, `username`,
    `password`) after both strategies -- unlike `SMTPConfig`, `username`/`password` are always
    required here: there is no unauthenticated-relay case for reading a personal mailbox over IMAP.
    Raises `ValueError` if `port` resolves to something that isn't a valid integer.
    """
    imap_section = _load_config_file().get("imap", {})
    if not isinstance(imap_section, dict):
        raise ValueError(
            f"expected [imap] section to be a table in {_config_file_path()}, "
            f"got {type(imap_section).__name__}"
        )

    def _file_str(field_name: str) -> str | None:
        value = imap_section.get(field_name)
        if value is None or isinstance(value, str):
            return value
        raise ValueError(
            f"expected {field_name!r} to be a string in [imap] ({_config_file_path()}), "
            f"got {type(value).__name__}"
        )

    host = os.environ.get("RTBF_IMAP_HOST") or _file_str("host")
    username = os.environ.get("RTBF_IMAP_USERNAME") or _file_str("username")
    password = os.environ.get("RTBF_IMAP_PASSWORD") or _file_str("password")

    port_raw: Any = os.environ.get("RTBF_IMAP_PORT")
    if port_raw is None:
        port_raw = imap_section.get("port")
    port = _DEFAULT_PORT
    if port_raw is not None:
        # `bool` is an `int` subclass (`int(True) == 1`) and a TOML float like `993.5` truncates
        # silently under a bare `int(...)` -- both would produce a wrong port with no error, the
        # first landing an IMAP connection attempt on port 1 with no indication why. Mirrors
        # `resolve_smtp_config()`'s identical port-parsing guard.
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

    missing = [
        field_name
        for field_name, value in (
            ("host", host),
            ("username", username),
            ("password", password),
        )
        if not value
    ]
    if missing:
        raise LookupError(
            f"incomplete IMAP config: missing {', '.join(missing)} "
            f"(set RTBF_IMAP_* env vars or add [imap] to {_config_file_path()})"
        )
    assert isinstance(host, str)
    assert isinstance(username, str)
    assert isinstance(password, str)

    return ImapConfig(host=host, port=port, username=username, password=password)
