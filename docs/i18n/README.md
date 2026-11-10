# Documentation translations

Per the spec (§6): project documentation is written in English as the base language, structured
to receive community translations. This directory is where those translations live.

**This is not the runtime i18n system.** `plugins/i18n/` resolves user-facing bot/kanban copy at
runtime, per interface language, and already has its own contract (`I18nProvider`) and tests —
that's a separate, already-built concern. `docs/i18n/` is specifically for translating *project
documentation* (README, CONTRIBUTING, guides) for contributors and users who prefer to read it in
another language. The two systems don't share code or resources.

## Structure

One subdirectory per language, named by its lowercase ISO 639-1 code (matching the convention
`plugins/i18n/locales/<lang>.toml` already uses for runtime strings, for consistency — e.g. `es/`
for Spanish, `fr/` for French). Inside each, one file per translated document, mirroring the
English original's filename:

```
docs/i18n/
├── README.md          (this file)
└── <lang>/
    ├── README.md       (translation of the top-level README.md)
    └── CONTRIBUTING.md (translation of the top-level CONTRIBUTING.md)
```

No language subdirectories exist yet — add the first one via the process below.

## Contributing a translation

1. Pick the document you want to translate (start with `README.md` — it's the entry point most
   readers hit first).
2. Create `docs/i18n/<lang>/<same-filename>.md` with your translation. Keep the same heading
   structure and section order as the English original, so a maintainer diffing the two can spot
   what's out of date at a glance.
3. Add a one-line note at the top of your translated file: which commit/date of the English
   original it was translated from, so future updates to the English source can be tracked against
   it (e.g. `> Translated from README.md as of commit abc1234.`).
4. Open a PR. Translation PRs don't need to touch code, tests, or CI — a maintainer will do a
   light pass to confirm the structure matches, not to verify translation accuracy in a language
   they may not read (native/fluent speakers reviewing each other's translation PRs are especially
   welcome).

## Keeping translations in sync

Documentation translations will drift out of date as the English original changes — that's
expected and not a blocker to merging a first translation. When a maintainer changes a
substantially-translated English doc, a follow-up issue noting which translations may now be
stale is enough; nothing in CI enforces translation freshness (no code path or test can meaningfully
check that a Spanish paragraph still accurately reflects an English one).
