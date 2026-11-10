"""Interactive setup wizard: collect + validate config, and start selected services (issue #89).

Reachable both as a shell command (`setup`, `rtbf_agent.cli.shell_main()`) and a standalone
`python -m rtbf_agent setup` subcommand. Walks through five independently opt-in pieces -- LLM
provider, Telegram bot, SMTP, SearXNG (web search), kanban web UI -- collecting each piece's
values, validating them with a real (but non-destructive) check *before* writing anything to
`.env`, and, for the two pieces that need an actual running service (SearXNG, kanban), starting
that service too, not just collecting credentials for it. A companion `stop` command
(`stop_services()`) tears back down whatever this wizard started, tracked via a small local state
file (`SERVICE_STATE_PATH`) so a second `setup`/`stop` run knows what's already running rather than
launching a duplicate or trying to stop nothing.

**Validation, not just collection:** each piece's setup function makes one real, cheap,
non-destructive call before writing its values -- an LLM completion, an SMTP connect+auth (no
email sent), a Telegram `getMe` + a confirmation message, a SearXNG health-check poll -- so a
misconfiguration (the exact class of bug issue #86's `mock-classify` fix addressed: a real LLM call
failing with an unhelpful error deep inside a later `process` run) surfaces immediately, at setup
time, with the wizard still able to let the operator retry or skip.

**`.env` writes never silently clobber an existing value** (`_write_env_value()`): if a var the
wizard is about to write already has a non-blank value, the old and new values are shown and the
operator is asked before overwriting -- an unset/blank var is written without asking, since there
is nothing to lose.

**Local infrastructure changes are confirmed first, not just announced**: before running the
literal `docker run`/`uv run uvicorn` command, the wizard prints exactly what it's about to run
and asks yes/no -- mirroring this project's approval-gated approach to any action with a real,
non-trivial effect, extended here to the operator's own machine (starting a container or a
background process is not as reversible as writing a line to `.env`).

Not in scope (see issue #89): non-interactive/scripted setup, and any change to Telegram's
existing per-command polling lifecycle (`cli._resolve_notifier_and_run()`) -- this module only
collects/validates Telegram's credentials, it never starts a persistent bot process itself (there
isn't one to start; the existing design already starts/stops polling around each command).
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import signal
import smtplib
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from rtbf_agent.core.interfaces import LLMMessage, LLMRequest
from rtbf_agent.plugins.llm.anthropic import AnthropicAdapter
from rtbf_agent.plugins.llm.openai_compatible import OpenAICompatibleAdapter
from rtbf_agent.plugins.llm.registry import PROVIDER_REGISTRY

RTBF_AGENT_CONFIG_DIR = Path.home() / ".config" / "rtbf-agent"
"""Same local-state directory `cli.DEFAULT_GMAIL_TOKEN_PATH`/every `plugins/*/config.py`'s
`DEFAULT_CONFIG_PATH` already use -- one place for this project's non-repo local state, never
inside the repository itself."""

SERVICE_STATE_PATH = RTBF_AGENT_CONFIG_DIR / "services.json"
"""Tracks what this wizard has started (container name / pid), so a later `setup` or `stop` run
knows what's already running instead of guessing or launching a duplicate."""

SEARXNG_SETTINGS_PATH = RTBF_AGENT_CONFIG_DIR / "searxng_settings.yml"
SEARXNG_CONTAINER_NAME = "rtbf-agent-searxng"
SEARXNG_PORT = 8080
KANBAN_LOG_PATH = RTBF_AGENT_CONFIG_DIR / "kanban.log"
KANBAN_PORT = 8000

_SEARXNG_SETTINGS_TEMPLATE = """\
# Written by rtbf-agent's setup wizard. The override that matters here: JSON output, which
# SearXNGProvider (plugins/websearch/searxng.py) needs and most SearXNG instances (including the
# stock docker image) ship with disabled by default.
use_default_settings: true

general:
  instance_name: "rtbf-agent"

server:
  secret_key: "{secret_key}"
  limiter: false

search:
  formats:
    - html
    - json
"""


def _render_searxng_settings_yaml() -> str:
    """A fresh, random `secret_key` per install -- not the same well-known value every operator's
    instance would otherwise share (OCR review finding on #90): even though this container is
    bound to localhost only, a static, publicly-visible secret in every install's config is a
    needless defense-in-depth gap for something this cheap to make instance-specific."""
    return _SEARXNG_SETTINGS_TEMPLATE.format(secret_key=secrets.token_urlsafe(32))


def _confirm(prompt: str, *, input_fn: Callable[[str], str]) -> bool:
    return input_fn(f"{prompt} [y/N]: ").strip().lower() in ("y", "yes")


def _env_path() -> Path:
    """The `.env` file the wizard writes to -- whatever `dotenv.find_dotenv()` would find from the
    current working directory (the same file a real `load_dotenv()` call would pick up, see
    `rtbf_agent.__main__`/`rtbf_agent.api.app`), or `./.env` if none exists yet -- matching this
    project's documented `cp .env.example .env` workflow (run from the repo root)."""
    import dotenv

    found = dotenv.find_dotenv(usecwd=True)
    return Path(found) if found else Path.cwd() / ".env"


def _write_env_value(
    key: str,
    value: str,
    *,
    input_fn: Callable[[str], str],
    print_fn: Callable[[str], None],
) -> None:
    """Write `key=value` into `.env`, asking before overwriting an existing non-blank value."""
    import dotenv

    env_path = _env_path()
    env_path.touch(exist_ok=True)
    existing = dotenv.get_key(str(env_path), key)
    if existing and not _confirm(
        f"{key} is already set to {existing!r} in {env_path} -- overwrite with {value!r}?",
        input_fn=input_fn,
    ):
        print_fn(f"Keeping existing {key}.")
        return
    dotenv.set_key(str(env_path), key, value)
    print_fn(f"Wrote {key} to {env_path}.")


def _load_service_state() -> dict[str, Any]:
    if not SERVICE_STATE_PATH.is_file():
        return {}
    try:
        loaded = json.loads(SERVICE_STATE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _save_service_state(state: dict[str, Any]) -> None:
    RTBF_AGENT_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    SERVICE_STATE_PATH.write_text(json.dumps(state, indent=2))


# --- LLM provider ------------------------------------------------------------------------------


def _build_adapter_for_validation(protocol: str, base_url: str, api_key: str | None):
    if protocol == "anthropic":
        if not api_key:
            raise ValueError("an Anthropic API key is required")
        return AnthropicAdapter(api_key=api_key, base_url=base_url)
    return OpenAICompatibleAdapter(base_url=base_url, api_key=api_key)


async def setup_llm_provider(
    *, input_fn: Callable[[str], str], print_fn: Callable[[str], None]
) -> None:
    """Collect an LLM provider/model/API key, validate with one real completion call, then write
    `RTBF_LLM_PROVIDER`/`RTBF_LLM_MODEL`/the provider's API key env var to `.env`.

    Deliberately makes a real call here rather than trusting the values look right: issue #86's
    `mock-classify` bug was exactly this class of failure -- a bad model name that only ever
    surfaced deep inside a real `process` run. Surfacing it here, before anything is written,
    means a typo'd model/expired key is caught immediately, with the operator still free to retry
    or skip this piece entirely.
    """
    print_fn(f"Known providers: {', '.join(PROVIDER_REGISTRY)} (or type any other name for a")
    print_fn("self-hosted/custom OpenAI-compatible endpoint, e.g. 'ollama').")
    provider = input_fn("LLM provider: ").strip()
    if not provider:
        print_fn("No provider given -- skipping LLM setup.")
        return

    preset = PROVIDER_REGISTRY.get(provider)
    if preset is not None:
        protocol = preset.protocol
        base_url = preset.base_url
        if preset.models:
            print_fn(f"Known models for {provider!r}: {', '.join(preset.models)}")
        model = input_fn("Model (blank for the provider's first known model): ").strip()
        if not model:
            model = preset.models[0] if preset.models else ""
        env_var = preset.env_var
    else:
        protocol = input_fn("Protocol ('anthropic' or 'openai'): ").strip()
        base_url = input_fn("Base URL (e.g. http://localhost:11434/v1): ").strip()
        model = input_fn("Model: ").strip()
        env_var = "RTBF_LLM_API_KEY"

    if not model:
        print_fn("No model given -- skipping LLM setup.")
        return

    api_key = input_fn(f"API key for {env_var} (blank if none needed): ").strip() or None

    try:
        adapter = _build_adapter_for_validation(protocol, base_url, api_key)
        await adapter.complete(
            LLMRequest(
                messages=(LLMMessage(role="user", content="Reply with the word OK."),),
                model=model,
                max_tokens=8,
            )
        )
    except Exception as exc:
        print_fn(f"LLM validation failed: {exc}")
        if not _confirm("Write these values anyway (unvalidated)?", input_fn=input_fn):
            print_fn("Skipping LLM setup.")
            return
    else:
        print_fn("LLM provider responded successfully.")

    _write_env_value("RTBF_LLM_PROVIDER", provider, input_fn=input_fn, print_fn=print_fn)
    _write_env_value("RTBF_LLM_MODEL", model, input_fn=input_fn, print_fn=print_fn)
    if preset is None:
        _write_env_value("RTBF_LLM_PROTOCOL", protocol, input_fn=input_fn, print_fn=print_fn)
        _write_env_value("RTBF_LLM_BASE_URL", base_url, input_fn=input_fn, print_fn=print_fn)
    if api_key:
        _write_env_value(env_var, api_key, input_fn=input_fn, print_fn=print_fn)


# --- Telegram bot --------------------------------------------------------------------------------


async def setup_telegram(
    *, input_fn: Callable[[str], str], print_fn: Callable[[str], None]
) -> None:
    """Collect a Telegram bot token/chat id, validate with a real `getMe` + confirmation message,
    then write `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`/`RTBF_NOTIFIER_BACKEND=telegram` to `.env`.

    `chat_id` discovery automates the exact manual `getUpdates` curl call
    `docs/user-guide/telegram-bot.md` already documents -- not a new mechanism, just fewer manual
    steps: message the bot, then let this function find the resulting chat id itself.
    """
    token = input_fn("Telegram bot token (from @BotFather): ").strip()
    if not token:
        print_fn("No token given -- skipping Telegram setup.")
        return

    from telegram import Bot
    from telegram.error import TelegramError

    bot = Bot(token=token)
    try:
        me = await bot.get_me()
    except TelegramError as exc:
        print_fn(f"Could not reach Telegram with that token: {exc}")
        if not _confirm("Continue anyway?", input_fn=input_fn):
            print_fn("Skipping Telegram setup.")
            return
        me = None
    else:
        print_fn(f"Connected to bot @{me.username}.")

    chat_id = input_fn("Chat id (blank to auto-discover from a message you send the bot): ").strip()
    if not chat_id:
        try:
            stale_updates = await bot.get_updates()
        except TelegramError as exc:
            print_fn(f"Could not fetch updates: {exc}")
            print_fn("Skipping Telegram setup.")
            return
        offset = stale_updates[-1].update_id + 1 if stale_updates else None

        _ = input_fn("Send the bot any message now, then press enter here...")
        try:
            updates = await bot.get_updates(offset=offset)
        except TelegramError as exc:
            print_fn(f"Could not fetch updates: {exc}")
            print_fn("Skipping Telegram setup.")
            return
        if not updates:
            print_fn("No message received yet -- run setup again once you've messaged the bot.")
            return
        chat_id = str(updates[-1].message.chat.id) if updates[-1].message else ""
        if not chat_id:
            print_fn("Could not determine a chat id from the latest update.")
            return
        print_fn(f"Discovered chat id {chat_id}.")

    try:
        await bot.send_message(chat_id=chat_id, text="rtbf-agent setup: this chat is now linked.")
    except TelegramError as exc:
        print_fn(f"Could not send a confirmation message to chat {chat_id}: {exc}")
        if not _confirm("Write these values anyway?", input_fn=input_fn):
            print_fn("Skipping Telegram setup.")
            return
    else:
        print_fn("Sent a confirmation message -- check your Telegram chat.")

    _write_env_value("TELEGRAM_BOT_TOKEN", token, input_fn=input_fn, print_fn=print_fn)
    _write_env_value("TELEGRAM_CHAT_ID", chat_id, input_fn=input_fn, print_fn=print_fn)
    _write_env_value("RTBF_NOTIFIER_BACKEND", "telegram", input_fn=input_fn, print_fn=print_fn)


# --- SMTP ------------------------------------------------------------------------------------


def _validate_smtp_sync(
    host: str, port: int, username: str | None, password: str | None, use_tls: bool
) -> None:
    """Connect (and authenticate, if credentials given) but never send anything -- run off the
    event loop via `asyncio.to_thread`, mirroring `SMTPSender.send()`'s own reason for doing so
    (`smtplib` has no async API)."""
    client_cls = smtplib.SMTP_SSL if port == 465 else smtplib.SMTP
    with client_cls(host, port, timeout=30.0) as client:
        if use_tls and port != 465:
            client.starttls()
        if username and password:
            client.login(username, password)


async def setup_smtp(*, input_fn: Callable[[str], str], print_fn: Callable[[str], None]) -> None:
    """Collect SMTP credentials, validate with a real connect(+auth), never a real send, then
    write `RTBF_SMTP_*` to `.env`."""
    host = input_fn("SMTP host: ").strip()
    if not host:
        print_fn("No host given -- skipping SMTP setup.")
        return
    port_raw = input_fn("SMTP port (blank for 587): ").strip()
    if port_raw:
        try:
            port = int(port_raw)
        except ValueError:
            print_fn(f"Invalid port {port_raw!r} -- must be a number. Skipping SMTP setup.")
            return
    else:
        port = 587
    from_address = input_fn("From address: ").strip()
    username = input_fn("Username (blank if none): ").strip() or None
    password = input_fn("Password (blank if none): ").strip() or None
    use_tls_raw = input_fn("Use STARTTLS? [Y/n]: ").strip().lower()
    use_tls = use_tls_raw not in ("n", "no")

    try:
        await asyncio.to_thread(_validate_smtp_sync, host, port, username, password, use_tls)
    except Exception as exc:
        print_fn(f"SMTP validation failed: {exc}")
        if not _confirm("Write these values anyway (unvalidated)?", input_fn=input_fn):
            print_fn("Skipping SMTP setup.")
            return
    else:
        print_fn("SMTP connection succeeded (no email sent).")

    _write_env_value("RTBF_SMTP_HOST", host, input_fn=input_fn, print_fn=print_fn)
    _write_env_value("RTBF_SMTP_PORT", str(port), input_fn=input_fn, print_fn=print_fn)
    _write_env_value("RTBF_SMTP_FROM_ADDRESS", from_address, input_fn=input_fn, print_fn=print_fn)
    _write_env_value(
        "RTBF_SMTP_USE_TLS", str(use_tls).lower(), input_fn=input_fn, print_fn=print_fn
    )
    if username:
        _write_env_value("RTBF_SMTP_USERNAME", username, input_fn=input_fn, print_fn=print_fn)
    if password:
        _write_env_value("RTBF_SMTP_PASSWORD", password, input_fn=input_fn, print_fn=print_fn)


# --- SearXNG (starts a real service) --------------------------------------------------------


async def setup_searxng(*, input_fn: Callable[[str], str], print_fn: Callable[[str], None]) -> None:
    """Start a real SearXNG instance via Docker (the same config `.github/searxng/settings.yml`
    already verifies works in this project's own CI, embedded here as `_SEARXNG_SETTINGS_YAML` so
    this works from an installed package too, not just a repo checkout), wait for it to actually
    answer a JSON search before writing `RTBF_SEARXNG_BASE_URL` -- never writes a URL that doesn't
    work yet.

    Idempotent: if a container named `SEARXNG_CONTAINER_NAME` already exists (from a prior
    `setup` run), reuses it instead of failing on "name already in use" or starting a duplicate.
    """
    if shutil.which("docker") is None:
        print_fn("Docker isn't installed/on PATH -- skipping SearXNG setup.")
        print_fn("See docs/user-guide/web-search.md for the manual setup steps.")
        return

    state = _load_service_state()
    already_running = await asyncio.to_thread(_docker_container_running, SEARXNG_CONTAINER_NAME)
    if already_running:
        print_fn(f"A container named {SEARXNG_CONTAINER_NAME!r} is already running -- reusing it.")
    else:
        RTBF_AGENT_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        SEARXNG_SETTINGS_PATH.write_text(_render_searxng_settings_yaml())
        command = [
            "docker",
            "run",
            "-d",
            "--name",
            SEARXNG_CONTAINER_NAME,
            "-p",
            # Bound explicitly to localhost, not the default 0.0.0.0 a bare "host:container" port
            # mapping would use -- this instance has no auth and no rate limiting
            # (_SEARXNG_SETTINGS_YAML's `limiter: false`), so publishing it to every interface
            # would expose an open search proxy to the whole local network. Mirrors the kanban
            # web UI's own `--host 127.0.0.1` (below).
            f"127.0.0.1:{SEARXNG_PORT}:8080",
            "-v",
            f"{SEARXNG_SETTINGS_PATH}:/etc/searxng/settings.yml:ro",
            "searxng/searxng:latest",
        ]
        print_fn("About to run:")
        print_fn("  " + " ".join(command))
        if not _confirm("Proceed?", input_fn=input_fn):
            print_fn("Skipping SearXNG setup.")
            return
        # subprocess.run() blocks the calling thread for as long as the command takes -- a first
        # `docker run` can take many seconds while the image pulls, which would otherwise freeze
        # this whole async function (and the shell it's running in). asyncio.to_thread() moves it
        # off the event loop, mirroring setup_smtp()'s identical reason for wrapping
        # _validate_smtp_sync() the same way.
        await asyncio.to_thread(_docker_rm_if_exists, SEARXNG_CONTAINER_NAME)
        result = await asyncio.to_thread(
            lambda: subprocess.run(command, capture_output=True, text=True)
        )
        if result.returncode != 0:
            print_fn(f"Failed to start SearXNG: {result.stderr.strip()}")
            return

    base_url = f"http://localhost:{SEARXNG_PORT}"
    if not await _wait_for_http_ok(f"{base_url}/search?q=test&format=json", timeout_seconds=60):
        print_fn(f"SearXNG did not become healthy at {base_url} in time.")
        if not _confirm("Write RTBF_SEARXNG_BASE_URL anyway?", input_fn=input_fn):
            return
    else:
        print_fn(f"SearXNG is up at {base_url}.")

    state["searxng"] = {"container_name": SEARXNG_CONTAINER_NAME}
    _save_service_state(state)
    _write_env_value("RTBF_SEARXNG_BASE_URL", base_url, input_fn=input_fn, print_fn=print_fn)


def _docker_container_running(name: str) -> bool:
    result = subprocess.run(
        ["docker", "ps", "--filter", f"name=^{name}$", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0 and name in result.stdout


def _docker_rm_if_exists(name: str) -> None:
    """Remove a stopped/dead container of this name, if one exists -- so a fresh `docker run`
    never fails with "name already in use" left over from a container that crashed or was
    manually stopped outside this wizard."""
    subprocess.run(["docker", "rm", "-f", name], capture_output=True, text=True)


async def _wait_for_http_ok(url: str, *, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + timeout_seconds
    async with httpx.AsyncClient() as client:
        while time.monotonic() < deadline:
            try:
                response = await client.get(url, timeout=5.0)
                if response.status_code == 200:
                    return True
            except httpx.RequestError:
                pass
            await asyncio.sleep(2.0)
    return False


# --- Kanban web UI (starts a real service) --------------------------------------------------


async def setup_kanban(*, input_fn: Callable[[str], str], print_fn: Callable[[str], None]) -> None:
    """Launch the kanban web UI (`uv run uvicorn rtbf_agent.api:app`) as a detached background
    process, wait for `/health`, and record its pid so `stop_services()` can find it later.

    Supersedes issue #82's "not in scope: a managed background kanban server process" now that
    there's an explicit place (this wizard, plus `stop_services()`) for that lifecycle to live.
    """
    state = _load_service_state()
    existing = state.get("kanban", {}).get("pid")
    if existing and _pid_running(existing):
        print_fn(f"Kanban server already running (pid {existing}).")
        return

    command = [
        "uv",
        "run",
        "uvicorn",
        "rtbf_agent.api:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(KANBAN_PORT),
    ]
    print_fn("About to run (in the background):")
    print_fn("  " + " ".join(command))
    if not _confirm("Proceed?", input_fn=input_fn):
        print_fn("Skipping kanban setup.")
        return

    RTBF_AGENT_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(KANBAN_LOG_PATH, "ab") as log_file:
        process = subprocess.Popen(
            command,
            stdout=log_file,
            stderr=log_file,
            start_new_session=True,
        )

    base_url = f"http://127.0.0.1:{KANBAN_PORT}"
    if await _wait_for_http_ok(f"{base_url}/health", timeout_seconds=30):
        print_fn(f"Kanban web UI is up at {base_url}.")
    else:
        print_fn(f"Kanban server started (pid {process.pid}) but didn't answer /health in time.")
        print_fn(f"Check {KANBAN_LOG_PATH} for details.")

    state["kanban"] = {"pid": process.pid, "port": KANBAN_PORT}
    _save_service_state(state)


def _pid_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


# --- Wizard orchestration + stop phase -------------------------------------------------------


async def run_setup_wizard(
    *,
    input_fn: Callable[[str], str] = input,
    print_fn: Callable[[str], None] = print,
) -> None:
    """The `setup` command (shell + `python -m rtbf_agent setup`): walk through every piece below,
    each independently opt-in, collecting/validating config and starting whatever real service
    that piece needs. One piece's failure is reported and skipped, never aborts the rest.

    The five `(label, setup_fn)` pairs are built fresh on every call, inside this function body,
    deliberately *not* hoisted into a module-level constant: a module-level tuple would capture
    each `setup_*` function object once, at import time, which a test's
    `monkeypatch.setattr(setup_wizard, "setup_llm_provider", fake)` could never actually reach
    (the tuple would still hold the original, real function) -- the same class of "bound at the
    wrong time" gotcha `cli.py`'s `input_fn: Callable[[str], str] = input` default parameter hit
    (see `tests/test_dotenv_loading.py`'s docstring for that one). Building this list inside the
    function means each `setup_llm_provider`/etc. reference is a fresh global-namespace lookup at
    call time, which *does* see a monkeypatched replacement.
    """
    print_fn("rtbf-agent setup wizard. Answer 'y' to configure a piece, anything else to skip it.")
    pieces = (
        ("LLM provider", setup_llm_provider),
        ("Telegram bot", setup_telegram),
        ("SMTP", setup_smtp),
        ("SearXNG (web search)", setup_searxng),
        ("Kanban web UI", setup_kanban),
    )
    for label, setup_fn in pieces:
        if not _confirm(f"Configure {label}?", input_fn=input_fn):
            print_fn(f"Skipping {label}.")
            continue
        try:
            await setup_fn(input_fn=input_fn, print_fn=print_fn)
        except Exception as exc:
            print_fn(f"{label} setup failed unexpectedly: {exc}")
    print_fn("Setup complete. Run 'stop' (shell) or 'python -m rtbf_agent stop' to stop any")
    print_fn("services this wizard started (SearXNG, kanban).")


async def stop_services(*, print_fn: Callable[[str], None] = print) -> None:
    """The `stop` command: tear down whatever `setup_searxng()`/`setup_kanban()` started, per
    `SERVICE_STATE_PATH` -- never guesses at a container/pid it didn't itself record."""
    state = _load_service_state()
    if not state:
        print_fn("Nothing to stop -- no services recorded as started by this wizard.")
        return

    searxng = state.pop("searxng", None)
    if searxng:
        name = searxng.get("container_name", SEARXNG_CONTAINER_NAME)
        result = await asyncio.to_thread(
            lambda: subprocess.run(["docker", "rm", "-f", name], capture_output=True, text=True)
        )
        if result.returncode == 0:
            print_fn(f"Stopped and removed SearXNG container {name!r}.")
        else:
            print_fn(f"Could not stop SearXNG container {name!r}: {result.stderr.strip()}")

    kanban = state.pop("kanban", None)
    if kanban:
        pid = kanban.get("pid")
        if pid and _pid_running(pid):
            os.kill(pid, signal.SIGTERM)
            print_fn(f"Stopped kanban server (pid {pid}).")
        else:
            print_fn("Kanban server wasn't running.")

    if not searxng and not kanban:
        print_fn("Nothing to stop -- no services recorded as started by this wizard.")

    _save_service_state(state)
