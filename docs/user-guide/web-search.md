# Automatic jurisdiction/recipient suggestions (site crawl + optional SearXNG fallback)

Without this, a case's jurisdiction stays unset (deferring to the global default) and you always
type its real deletion-request recipient yourself, with no suggestion. With it, the agent
researches a *suggestion* for each: jurisdiction is applied to the case automatically (no
interaction — see below for how to review or override it); recipient is still shown to you as a
suggestion you must type/confirm yourself, never auto-accepted.

**The agent tries the site itself first.** When you `add` a case, you already gave it that site's
URL — so for each case it fetches that site's own homepage, a few of its own links that look
relevant (privacy/contact/legal-notice pages), and a short list of common paths
(`/privacy`, `/contact`, `/legal`, ...), then asks the LLM to read what it found. **This part needs
no configuration and no external service** — it's a direct HTTP fetch of the site you already
pointed the agent at, nothing else. Only if the site itself yields nothing usable does the agent
fall back to a generic SearXNG keyword search across the web (optional, see below).

**Recipient discovery's suggestion is never applied on its own** — it never accepts a blank answer
as "use the suggestion"; you always type the actual address yourself, confirming you looked at it.
**Jurisdiction is different: a confident suggestion is applied to the case automatically**, with no
prompt at all — recorded on the case's own audit trail either way, so you can always see *why* a
value ended up where it did. You can still review or override the applied value (or set it from
scratch) any time before the case is classified, with `set-jurisdiction <case_id> <value>` (CLI,
shell, or the kanban case-detail page) — see the main [user guide](README.md#running-a-real-case-what-actually-happens).
Neither path lets the agent send, attach, or otherwise act on anything unattended.

Implemented in `src/rtbf_agent/plugins/webfetch/` (the site-crawl path, always active),
`plugins/websearch/searxng.py` (the optional web-search fallback), `plugins/jurisdiction_detection/`,
`plugins/recipient_detection/`.

## What "the agent fetches your site" means in practice

Adding a case (`add <url>`) or processing one already added causes real HTTP GET requests to that
site's own server — its homepage, a handful of its own links, and a few common paths — before any
human has approved anything past that read. This is the same class of "look something up before a
human decides" step discovery/classification already do; nothing found this way is ever sent,
attached, or written to the site's own server, and no suggestion it produces is ever applied
without you typing/confirming it yourself (see above). There is currently no config flag to disable
this fetch step — only the *web-search fallback* (below) is optional.

## Optional fallback: SearXNG web search

If crawling the site itself doesn't turn up anything usable (e.g. its privacy/contact info isn't
reachable from its own homepage within a few hops), the agent can fall back to a generic keyword
web search via [SearXNG](https://docs.searxng.org/), a self-hosted metasearch engine — this project
talks to *your own* instance's JSON search API, never a project-operated search relay (same "no
centralized backend" principle as everything else here). This part is fully optional: without it,
a case whose own site doesn't yield an answer just falls through to no jurisdiction suggestion at
all (deferring to the global default) and a manual-only recipient prompt, exactly as before
jurisdiction/recipient detection existed.

### Setup steps

1. Run a SearXNG instance. The simplest way is Docker:
   ```bash
   docker run -d --name searxng -p 8080:8080 searxng/searxng
   ```
2. Enable JSON output — SearXNG ships with it disabled. Edit the instance's `settings.yml`
   (inside the container, or a mounted volume) and add `json` to `search.formats`:
   ```yaml
   search:
     formats:
       - html
       - json
   ```
   Restart the container after editing.
3. Set in `.env`:
   ```bash
   RTBF_SEARXNG_BASE_URL=http://localhost:8080
   # Only needed if your instance sits behind a reverse proxy expecting a bearer token — most
   # self-hosted instances need no key at all.
   RTBF_SEARXNG_API_KEY=
   ```
4. Run `python -m rtbf_agent` as usual. Sites whose own crawl doesn't yield a jurisdiction/recipient
   now also get a keyword-search attempt before jurisdiction falls back to no suggestion (global
   default) and recipient falls back to a bare manual prompt.

### If you don't set this up

Nothing breaks — sites whose own crawl already finds an answer are unaffected either way; sites
whose crawl finds nothing just skip straight to no jurisdiction suggestion (global default applies)
and the manual-only recipient prompt, instead of also trying a web search first.
`RTBF_SEARXNG_BASE_URL` unset means the config resolver raises a `LookupError`
internally, which the CLI catches and treats as "no search fallback available" — not an error. A
malformed config (not "unset", but genuinely broken — e.g. a bad value in
`~/.config/rtbf-agent/config.toml`) is the one case that still raises loud, rather than being
silently swallowed.

## Troubleshooting

- **No suggestion ever appears, and you expected one** — a detector failure (site unreachable,
  SearXNG unreachable/misconfigured, LLM not confident) is swallowed as "no proposal" by design,
  not surfaced as an error, so check the case's own event history (`/cases/<id>`) for a
  `jurisdiction_no_proposal`/`recipient_no_proposal`/`*_detection_failed` event rather than
  expecting CLI output — its `query` field shows whether the crawl or the search path was tried
  (a `crawl https://...` marker vs. a keyword-search string), and its `results` show what was
  actually found.
- **You want to confirm the SearXNG fallback specifically is reachable** — `curl
  $RTBF_SEARXNG_BASE_URL/search?q=test&format=json` should return JSON, not an error page or HTML.
- **`format=json` returns an error from SearXNG itself** — `search.formats` in `settings.yml`
  doesn't include `json` yet (setup step 2 above); most default installs need this enabled
  explicitly.
