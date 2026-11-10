"""Local-only global config resolution for requester identity and per-case defaults (R18, R22).

Mirrors `plugins/llm/config.py`'s resolution *shape* on purpose (see that module's docstring for
the full rationale): a short-circuiting, priority-ordered chain --

1. Environment variables (`RTBF_REQUESTER_NAME`, `RTBF_DEFAULT_APPLICABLE_JURISDICTION`,
   `RTBF_DEFAULT_DRAFTING_LANGUAGE`).
2. The same local, gitignored TOML config file `plugins/llm/config.py` uses
   (`~/.config/rtbf-agent/config.toml` by default, override with `RTBF_AGENT_CONFIG_PATH`), under
   a `[case_defaults]` section instead of `[llm.<provider>]`.

There is no third fallback layer here (no registry preset applies to requester identity or
case defaults) -- a field left unset by both strategies simply resolves to `None`. Nothing in this
module knows what a "valid" jurisdiction or drafting language looks like; it only resolves the
configured *strings*, exactly as `core/models.py`'s `Case.resolved_applicable_jurisdiction`/
`resolved_drafting_language` expect for their `global_default` argument (this module never
touches `core/`, keeping jurisdiction/language *values* entirely in `plugins/legal_templates/` and
config, never hardcoded here or in `core/`).

This module deliberately lives at the top level (a sibling of `agent_loop.py`), not under
`core/` or `plugins/`: it isn't a plugin implementing one of `core/interfaces.py`'s ABCs (there's
no jurisdiction-/language-/channel-specific behavior to abstract over here, just generic config
plumbing), and putting case-default *values* inside `core/` would contradict `core/models.py`'s
own docstring, which is explicit that "this module does not know what the global defaults are" --
resolving them is deliberately the orchestrator's job, not `core/`'s.

Also resolves `INTERFACE_LANGUAGE` (`resolve_interface_language()`) -- the single interface
language a whole-process caller (the Telegram bot, issue #14; the kanban UI, issue #18) picks once
and passes as `lang` on every `I18nProvider.translate()` call it makes. Deliberately independent of
`RTBF_DEFAULT_DRAFTING_LANGUAGE`/`RTBF_DEFAULT_APPLICABLE_JURISDICTION` above -- no code path here
derives one setting from another (R22): a Spanish-speaking operator drafting GDPR requests in
English is a perfectly valid combination.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "rtbf-agent" / "config.toml"
"""Same default path `plugins/llm/config.py` uses -- one local config file, multiple sections."""

DEFAULT_INTERFACE_LANGUAGE = "en"
"""Fallback when `INTERFACE_LANGUAGE` is unset -- a config default, not hardcoded interface logic:
nothing here changes behavior based on the language, it only picks which `I18nProvider.translate()`
`lang` argument a whole-process caller uses (R20 is about behavior that only makes sense for one
language; this is a bootstrapping default for a value plugins/i18n/ resolves generically)."""


@dataclass(frozen=True)
class GlobalConfig:
    """Resolved global defaults: requester identity plus per-case-overridable defaults (R22)."""

    requester_name: str | None
    default_applicable_jurisdiction: str | None
    default_drafting_language: str | None


def _config_file_path() -> Path:
    override = os.environ.get("RTBF_AGENT_CONFIG_PATH")
    # `.expanduser()`: many non-shell contexts that set env vars (systemd units, Docker `ENV`, CI)
    # never expand `~` themselves, so a literal `~/...` override would otherwise be treated as a
    # relative directory named `~`, silently fail to resolve, and every default would resolve to
    # `None` with no indication why.
    return Path(override).expanduser() if override else DEFAULT_CONFIG_PATH


def _load_config_file() -> dict[str, Any]:
    """Load the local TOML config file, or `{}` if it doesn't exist. Never touches the network."""
    path = _config_file_path()
    if not path.exists():
        return {}
    if not path.is_file():
        # A misconfigured `RTBF_AGENT_CONFIG_PATH` pointing at a directory must fail loud here,
        # not silently resolve to `{}` -- otherwise every global default resolves to `None` and
        # the operator only finds out later, from a `_classify()` ValueError that looks like a
        # code bug rather than the actual misconfiguration.
        raise ValueError(f"config path {path} exists but is not a regular file")
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except OSError as exc:
        raise ValueError(f"cannot read config file {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"invalid TOML in config file {path}: {exc}") from exc


def resolve_global_config() -> GlobalConfig:
    """Resolve `requester_name` and the case-default `applicable_jurisdiction`/`drafting_language`.

    Each field independently prefers its env var, falling back to `[case_defaults]` in the local
    config file, then `None` if neither strategy sets it -- there is no further fallback (unlike
    `plugins/llm/config.py`'s registry-preset step, nothing here has a sensible hardcoded default:
    a jurisdiction or drafting language guessed by this module would be exactly the kind of
    hardcoded jurisdiction logic `CLAUDE.md` forbids in shared code). Raises `ValueError` if the
    `[case_defaults]` section (or one of its string fields) is present but malformed -- never
    silently ignored.
    """
    config_path = _config_file_path()
    case_defaults_section = _load_config_file().get("case_defaults", {})
    if not isinstance(case_defaults_section, dict):
        raise ValueError(
            f"expected [case_defaults] section to be a table in {config_path}, "
            f"got {type(case_defaults_section).__name__}"
        )

    def _file_str(field_name: str) -> str | None:
        value = case_defaults_section.get(field_name)
        if value is None:
            return None
        if isinstance(value, str):
            # An explicitly empty (or whitespace-only, e.g. a stray trailing space) string in the
            # config file carries no information -- treat it the same as "not set" so callers only
            # ever see `None` vs. a real, meaningful value. Stripping also guards against a value
            # like `"GDPR "` silently failing template lookup in `_draft()` with no obvious cause.
            stripped = value.strip()
            return stripped or None
        raise ValueError(
            f"expected {field_name!r} to be a string in [case_defaults] "
            f"({config_path}), got {type(value).__name__}"
        )

    def _resolved(env_var: str, file_field: str) -> str | None:
        # `os.environ.get(...) or _file_str(...)` would treat an env var explicitly set to `""`
        # as "not set" and fall through to the config file -- checking `is not None` instead means
        # only an *absent* env var falls through, not an empty one.
        env_value = os.environ.get(env_var)
        if env_value is not None:
            stripped = env_value.strip()
            return stripped or None
        return _file_str(file_field)

    requester_name = _resolved("RTBF_REQUESTER_NAME", "requester_name")
    default_applicable_jurisdiction = _resolved(
        "RTBF_DEFAULT_APPLICABLE_JURISDICTION", "applicable_jurisdiction"
    )
    default_drafting_language = _resolved("RTBF_DEFAULT_DRAFTING_LANGUAGE", "drafting_language")

    return GlobalConfig(
        requester_name=requester_name,
        default_applicable_jurisdiction=default_applicable_jurisdiction,
        default_drafting_language=default_drafting_language,
    )


def resolve_interface_language() -> str:
    """Resolve the single interface language a whole-process caller (Telegram bot, kanban UI) uses.

    Reads `INTERFACE_LANGUAGE` only -- no local-config-file fallback (unlike `resolve_global_config`
    above): the interface language is a single-process-wide setting a self-hosting operator sets
    once alongside `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`, not a per-case override. Falls back to
    `DEFAULT_INTERFACE_LANGUAGE` ("en") if unset or blank -- never raises, since every deployment
    needs *some* interface language and "en" is guaranteed to resolve against
    `plugins/i18n/locales/en.toml`.
    """
    raw = os.environ.get("INTERFACE_LANGUAGE")
    if raw is None:
        return DEFAULT_INTERFACE_LANGUAGE
    stripped = raw.strip()
    return stripped or DEFAULT_INTERFACE_LANGUAGE
