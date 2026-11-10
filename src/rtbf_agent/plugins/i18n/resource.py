"""`ResourceI18nProvider` -- a flat, TOML-resource-file `core.interfaces.I18nProvider` adapter.

Chosen for consistency with `plugins/llm/config.py`, the only other module in this repo that
already does file-based config parsing -- it reads local TOML via `tomllib`, so this module reuses
the same format rather than introducing a second one. One file per language
(`plugins/i18n/locales/<lang>.toml`), each a flat `key = "value"` table -- no nested sections, so
adding a language is "drop a new file in `locales/`", never a change to this module or to
`core/interfaces.py` (R21).

Per `core.interfaces.I18nProvider`, a missing language or a missing key must fail loud: this
module never falls back to another language, the key string itself, or an empty string (R23).
"""

from __future__ import annotations

import re
import tomllib
from importlib import resources
from pathlib import Path

from rtbf_agent.core.interfaces import I18nProvider

_DEFAULT_LOCALES_PACKAGE = "rtbf_agent.plugins.i18n.locales"

# BCP-47-ish: a lowercase primary subtag, optionally followed by "-" and an uppercase region
# subtag (e.g. "en", "en-US", "pt-BR"). Deliberately strict rather than permissive: `lang` is
# concatenated straight into a filename (`f"{lang}.toml"`) below, so anything containing a path
# separator or ".." must be rejected before it ever reaches the filesystem/`importlib.resources`
# lookup -- a caller (e.g. a Telegram bot's per-user language setting, or a kanban UI's
# Accept-Language header) may pass through attacker-influenced text here with no validation of
# its own, since the entire point of this contract is that callers don't reimplement lookup
# logic themselves.
_VALID_LANG_RE = re.compile(r"^[a-z]{2,3}(-[A-Z]{2})?\Z")


class ResourceI18nProvider(I18nProvider):
    """Resolves `translate(key, lang)` against flat `<lang>.toml` resource files.

    By default reads the packaged `locales/` directory shipped alongside this module; pass
    `locales_dir` to point at a different directory instead (e.g. in a test). Files are loaded
    lazily and cached per language the first time that language is requested.
    """

    def __init__(self, *, locales_dir: Path | None = None) -> None:
        self._locales_dir = locales_dir
        self._loaded: dict[str, dict[str, str]] = {}

    def _load_lang(self, lang: str) -> dict[str, str]:
        cached = self._loaded.get(lang)
        if cached is not None:
            return cached

        if not _VALID_LANG_RE.match(lang):
            # Reject before `lang` ever reaches a filesystem/resource path -- a permissive check
            # here would let a caller-supplied language code (e.g. `"../../secret"`) traverse
            # outside `locales_dir`/the packaged resource directory (see module docstring note).
            raise LookupError(f"not a valid language code: {lang!r}")

        if self._locales_dir is not None:
            if not self._locales_dir.is_dir():
                raise LookupError(
                    f"locales_dir {self._locales_dir!r} does not exist or is not a directory"
                )
            path = self._locales_dir / f"{lang}.toml"
            if not path.is_file():
                raise LookupError(f"no i18n resource file for language {lang!r} at {path}")
            try:
                raw = path.read_bytes()
            except OSError as exc:
                raise LookupError(
                    f"cannot read i18n resource file for language {lang!r} at {path}"
                ) from exc
        else:
            try:
                resource = resources.files(_DEFAULT_LOCALES_PACKAGE).joinpath(f"{lang}.toml")
                raw = resource.read_bytes()
            except (FileNotFoundError, OSError, ImportError) as exc:
                raise LookupError(f"no packaged i18n resource file for language {lang!r}") from exc

        try:
            table = tomllib.loads(raw.decode("utf-8"))
        except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
            raise ValueError(f"invalid TOML in i18n resource for language {lang!r}: {exc}") from exc

        flat: dict[str, str] = {}
        for k, v in table.items():
            if not isinstance(v, str):
                raise ValueError(
                    f"i18n resource for language {lang!r} must be a flat key->string table; "
                    f"key {k!r} has value of type {type(v).__name__}"
                )
            flat[k] = v

        self._loaded[lang] = flat
        return flat

    def translate(self, key: str, lang: str, **params: object) -> str:
        """Resolve `key` in `lang`'s resource file and interpolate `params` via `str.format`.

        Raises `LookupError` if `lang` has no resource file, or if `key` isn't in it -- never
        substitutes `key` itself or an empty string (R23).
        """
        table = self._load_lang(lang)
        if key not in table:
            raise LookupError(f"no i18n translation for key {key!r} in language {lang!r}")
        try:
            return table[key].format(**params)
        except (KeyError, IndexError) as exc:
            raise LookupError(
                f"error formatting translation for key {key!r} in language {lang!r}: {exc}"
            ) from exc
        except (ValueError, TypeError) as exc:
            raise ValueError(
                f"malformed format string in resource for key {key!r} in language {lang!r}: {exc}"
            ) from exc
