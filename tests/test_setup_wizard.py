"""Tests for `rtbf_agent.setup_wizard` (issue #89).

Every test monkeypatches the module's local-state path constants
(`SERVICE_STATE_PATH`/`SEARXNG_SETTINGS_PATH`/`KANBAN_LOG_PATH`/`RTBF_AGENT_CONFIG_DIR`) into
`tmp_path` -- this module's whole job is writing to the real `~/.config/rtbf-agent/`/`.env` on a
real machine, which a test must never actually do. No test spawns a real Docker container, a real
`uvicorn` process, or makes a real network/Telegram/SMTP/LLM call -- every external effect is
mocked at the seam `setup_wizard.py` itself calls through (`subprocess.run`/`Popen`,
`_validate_smtp_sync`, `_build_adapter_for_validation`, `telegram.Bot`, `_wait_for_http_ok`).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rtbf_agent import setup_wizard


class _FakeIO:
    def __init__(self, answers: list[str]) -> None:
        self._answers = list(answers)
        self.printed: list[str] = []

    def input_fn(self, prompt: str) -> str:
        return self._answers.pop(0)

    def print_fn(self, text: str) -> None:
        self.printed.append(text)


@pytest.fixture(autouse=True)
def _isolate_local_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Redirect every local-state path this module writes to into `tmp_path` -- a test must never
    touch the real `~/.config/rtbf-agent/` on the machine running it."""
    monkeypatch.setattr(setup_wizard, "RTBF_AGENT_CONFIG_DIR", tmp_path)
    monkeypatch.setattr(setup_wizard, "SERVICE_STATE_PATH", tmp_path / "services.json")
    monkeypatch.setattr(setup_wizard, "SEARXNG_SETTINGS_PATH", tmp_path / "searxng_settings.yml")
    monkeypatch.setattr(setup_wizard, "KANBAN_LOG_PATH", tmp_path / "kanban.log")
    monkeypatch.chdir(tmp_path)


# --- _write_env_value / _env_path -------------------------------------------------------------


def test_write_env_value_creates_env_file_and_writes_key(tmp_path: Path) -> None:
    io = _FakeIO(answers=[])
    setup_wizard._write_env_value(
        "RTBF_TEST_KEY", "value1", input_fn=io.input_fn, print_fn=io.print_fn
    )

    env_path = tmp_path / ".env"
    assert env_path.is_file()
    assert "RTBF_TEST_KEY" in env_path.read_text()
    assert any("Wrote RTBF_TEST_KEY" in line for line in io.printed)


def test_write_env_value_asks_before_overwriting_existing_value(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text('RTBF_TEST_KEY="old"\n')
    io = _FakeIO(answers=["n"])

    setup_wizard._write_env_value(
        "RTBF_TEST_KEY", "new", input_fn=io.input_fn, print_fn=io.print_fn
    )

    assert 'RTBF_TEST_KEY="old"' in (tmp_path / ".env").read_text()
    assert any("Keeping existing" in line for line in io.printed)


def test_write_env_value_overwrites_when_confirmed(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text('RTBF_TEST_KEY="old"\n')
    io = _FakeIO(answers=["y"])

    setup_wizard._write_env_value(
        "RTBF_TEST_KEY", "new", input_fn=io.input_fn, print_fn=io.print_fn
    )

    assert "'new'" in (tmp_path / ".env").read_text()


# --- service state -------------------------------------------------------------------------


def test_render_searxng_settings_yaml_generates_a_fresh_random_secret_key() -> None:
    """OCR review finding on #90: every install used to share the exact same, publicly-visible
    `secret_key` -- must now be random and different per call."""
    first = setup_wizard._render_searxng_settings_yaml()
    second = setup_wizard._render_searxng_settings_yaml()
    assert "secret_key:" in first
    assert first != second


def test_service_state_round_trips() -> None:
    assert setup_wizard._load_service_state() == {}
    setup_wizard._save_service_state({"kanban": {"pid": 123}})
    assert setup_wizard._load_service_state() == {"kanban": {"pid": 123}}


def test_load_service_state_tolerates_corrupt_file() -> None:
    setup_wizard.SERVICE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    setup_wizard.SERVICE_STATE_PATH.write_text("not json")
    assert setup_wizard._load_service_state() == {}


# --- LLM provider ----------------------------------------------------------------------------


async def test_setup_llm_provider_writes_config_on_successful_validation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class _FakeAdapter:
        async def complete(self, request):
            return None

    monkeypatch.setattr(
        setup_wizard, "_build_adapter_for_validation", lambda *a, **k: _FakeAdapter()
    )
    io = _FakeIO(answers=["anthropic", "claude-sonnet-5", "sk-test"])

    await setup_wizard.setup_llm_provider(input_fn=io.input_fn, print_fn=io.print_fn)

    env_text = (tmp_path / ".env").read_text()
    assert "RTBF_LLM_PROVIDER" in env_text
    assert "claude-sonnet-5" in env_text
    assert "ANTHROPIC_API_KEY" in env_text
    assert any("responded successfully" in line for line in io.printed)


async def test_setup_llm_provider_asks_before_writing_on_validation_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class _FailingAdapter:
        async def complete(self, request):
            raise RuntimeError("model 'mock-classify' does not exist")

    monkeypatch.setattr(
        setup_wizard, "_build_adapter_for_validation", lambda *a, **k: _FailingAdapter()
    )
    io = _FakeIO(answers=["openai", "gpt-4o", "sk-test", "n"])

    await setup_wizard.setup_llm_provider(input_fn=io.input_fn, print_fn=io.print_fn)

    assert not (tmp_path / ".env").exists()
    assert any("validation failed" in line for line in io.printed)


async def test_setup_llm_provider_skips_on_blank_provider() -> None:
    io = _FakeIO(answers=[""])
    await setup_wizard.setup_llm_provider(input_fn=io.input_fn, print_fn=io.print_fn)
    assert any("skipping llm setup" in line.lower() for line in io.printed)


# --- Telegram ----------------------------------------------------------------------------------


async def test_setup_telegram_writes_config_after_successful_validation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class _FakeMe:
        username = "test_bot"

    class _FakeBot:
        def __init__(self, token: str) -> None:
            self.token = token

        async def get_me(self):
            return _FakeMe()

        async def send_message(self, chat_id, text):
            return None

    monkeypatch.setattr("telegram.Bot", _FakeBot)
    io = _FakeIO(answers=["123:ABC", "987654321"])

    await setup_wizard.setup_telegram(input_fn=io.input_fn, print_fn=io.print_fn)

    env_text = (tmp_path / ".env").read_text()
    assert "TELEGRAM_BOT_TOKEN" in env_text
    assert "987654321" in env_text
    assert "telegram" in env_text


async def test_setup_telegram_auto_discovers_chat_id_from_getUpdates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class _FakeChat:
        id = 555

    class _FakeMessage:
        chat = _FakeChat()

    class _FakeUpdate:
        update_id = 42
        message = _FakeMessage()

    class _FakeMe:
        username = "test_bot"

    class _FakeBot:
        def __init__(self, token: str) -> None:
            self.token = token

        async def get_me(self):
            return _FakeMe()

        async def get_updates(self, offset=None):
            return [_FakeUpdate()]

        async def send_message(self, chat_id, text):
            return None

    monkeypatch.setattr("telegram.Bot", _FakeBot)
    io = _FakeIO(answers=["123:ABC", "", ""])  # token, blank chat_id, "press enter"

    await setup_wizard.setup_telegram(input_fn=io.input_fn, print_fn=io.print_fn)

    assert "555" in (tmp_path / ".env").read_text()


async def test_setup_telegram_skips_on_blank_token() -> None:
    io = _FakeIO(answers=[""])
    await setup_wizard.setup_telegram(input_fn=io.input_fn, print_fn=io.print_fn)
    assert any("skipping telegram setup" in line.lower() for line in io.printed)


async def test_setup_telegram_reports_no_update_yet_and_returns(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class _FakeMe:
        username = "test_bot"

    class _FakeBot:
        def __init__(self, token: str) -> None:
            pass

        async def get_me(self):
            return _FakeMe()

        async def get_updates(self, offset=None):
            return []

    monkeypatch.setattr("telegram.Bot", _FakeBot)
    io = _FakeIO(answers=["123:ABC", "", ""])

    await setup_wizard.setup_telegram(input_fn=io.input_fn, print_fn=io.print_fn)

    assert not (tmp_path / ".env").exists()
    assert any("no message received yet" in line.lower() for line in io.printed)


async def test_setup_telegram_discovers_chat_id_using_an_offset_to_skip_stale_updates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class _FakeChat:
        id = 555

    class _FakeMessage:
        chat = _FakeChat()

    class _StaleUpdate:
        update_id = 41
        message = _FakeMessage()

    class _FreshUpdate:
        update_id = 42
        message = _FakeMessage()

    class _FakeMe:
        username = "test_bot"

    class _FakeBot:
        def __init__(self, token: str) -> None:
            self.offsets_seen: list[int | None] = []

        async def get_me(self):
            return _FakeMe()

        async def get_updates(self, offset=None):
            self.offsets_seen.append(offset)
            if offset is None:
                return [_StaleUpdate()]
            return [_FreshUpdate()]

        async def send_message(self, chat_id, text):
            return None

    fake_bot_holder: list[_FakeBot] = []

    def _make_bot(token: str) -> _FakeBot:
        bot = _FakeBot(token)
        fake_bot_holder.append(bot)
        return bot

    monkeypatch.setattr("telegram.Bot", _make_bot)
    io = _FakeIO(answers=["123:ABC", "", ""])

    await setup_wizard.setup_telegram(input_fn=io.input_fn, print_fn=io.print_fn)

    assert fake_bot_holder[0].offsets_seen == [None, 42]
    assert "555" in (tmp_path / ".env").read_text()


async def test_setup_llm_provider_supports_a_custom_self_hosted_endpoint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class _FakeAdapter:
        async def complete(self, request):
            return None

    monkeypatch.setattr(
        setup_wizard, "_build_adapter_for_validation", lambda *a, **k: _FakeAdapter()
    )
    io = _FakeIO(answers=["ollama", "openai", "http://localhost:11434/v1", "llama3", ""])

    await setup_wizard.setup_llm_provider(input_fn=io.input_fn, print_fn=io.print_fn)

    env_text = (tmp_path / ".env").read_text()
    assert "RTBF_LLM_PROTOCOL" in env_text
    assert "RTBF_LLM_BASE_URL" in env_text
    assert "llama3" in env_text


# --- SMTP --------------------------------------------------------------------------------------


async def test_setup_smtp_writes_config_on_successful_validation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(setup_wizard, "_validate_smtp_sync", lambda *a, **k: None)
    io = _FakeIO(answers=["smtp.example.com", "", "user@example.com", "bob", "hunter2", ""])

    await setup_wizard.setup_smtp(input_fn=io.input_fn, print_fn=io.print_fn)

    env_text = (tmp_path / ".env").read_text()
    assert "smtp.example.com" in env_text
    assert "RTBF_SMTP_USERNAME" in env_text
    assert "RTBF_SMTP_PASSWORD" in env_text
    assert any("succeeded" in line for line in io.printed)


async def test_setup_smtp_asks_before_writing_on_validation_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def _raise(*_args, **_kwargs):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(setup_wizard, "_validate_smtp_sync", _raise)
    io = _FakeIO(answers=["smtp.example.com", "", "user@example.com", "", "", "", "n"])

    await setup_wizard.setup_smtp(input_fn=io.input_fn, print_fn=io.print_fn)

    assert not (tmp_path / ".env").exists()


async def test_setup_smtp_skips_on_blank_host() -> None:
    io = _FakeIO(answers=[""])
    await setup_wizard.setup_smtp(input_fn=io.input_fn, print_fn=io.print_fn)
    assert any("skipping smtp setup" in line.lower() for line in io.printed)


def test_validate_smtp_sync_uses_smtp_ssl_for_port_465(monkeypatch: pytest.MonkeyPatch) -> None:
    used_cls: list[type] = []

    class _FakeSMTP:
        def __init__(self, host, port, timeout=None):
            used_cls.append(type(self))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def starttls(self):
            raise AssertionError("starttls should not be called for SMTP_SSL (port 465)")

        def login(self, username, password):
            pass

    monkeypatch.setattr(setup_wizard.smtplib, "SMTP_SSL", _FakeSMTP)
    monkeypatch.setattr(
        setup_wizard.smtplib,
        "SMTP",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("plain SMTP should not be used")),
    )

    setup_wizard._validate_smtp_sync("smtp.example.com", 465, None, None, use_tls=True)

    assert used_cls == [_FakeSMTP]


async def test_setup_smtp_reports_a_clean_error_for_a_non_numeric_port(tmp_path: Path) -> None:
    """OCR review finding on #90: a non-numeric port used to raise an unhandled `ValueError`
    straight out of `int(port_raw)`, propagating as a confusing message rather than being
    reported cleanly."""
    io = _FakeIO(answers=["smtp.example.com", "not-a-number"])

    await setup_wizard.setup_smtp(input_fn=io.input_fn, print_fn=io.print_fn)

    assert not (tmp_path / ".env").exists()
    assert any("invalid port" in line.lower() for line in io.printed)


# --- SearXNG -------------------------------------------------------------------------------


async def test_setup_searxng_skips_when_docker_not_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(setup_wizard.shutil, "which", lambda _cmd: None)
    io = _FakeIO(answers=[])
    await setup_wizard.setup_searxng(input_fn=io.input_fn, print_fn=io.print_fn)
    assert any("docker isn't installed" in line.lower() for line in io.printed)


async def test_setup_searxng_starts_container_after_confirmation_and_writes_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(setup_wizard.shutil, "which", lambda _cmd: "/usr/bin/docker")
    monkeypatch.setattr(setup_wizard, "_docker_container_running", lambda _name: False)
    monkeypatch.setattr(setup_wizard, "_docker_rm_if_exists", lambda _name: None)

    ran_commands: list[list[str]] = []

    class _Result:
        returncode = 0
        stderr = ""

    def _fake_run(command, **_kwargs):
        ran_commands.append(command)
        return _Result()

    monkeypatch.setattr(setup_wizard.subprocess, "run", _fake_run)

    async def _fake_wait(url, *, timeout_seconds):
        return True

    monkeypatch.setattr(setup_wizard, "_wait_for_http_ok", _fake_wait)

    io = _FakeIO(answers=["y"])  # confirm running docker run
    await setup_wizard.setup_searxng(input_fn=io.input_fn, print_fn=io.print_fn)

    assert any("docker" in cmd[0] and "run" in cmd for cmd in ran_commands)
    # OCR review finding on #90: the port publish must bind to localhost explicitly, not the
    # default 0.0.0.0 a bare "host:container" mapping would use -- this instance has no auth and
    # no rate limiting, so publishing to every interface would expose an open search proxy to the
    # whole local network.
    run_command = next(cmd for cmd in ran_commands if "run" in cmd)
    assert f"127.0.0.1:{setup_wizard.SEARXNG_PORT}:8080" in run_command
    assert f"{setup_wizard.SEARXNG_PORT}:8080" not in run_command
    assert run_command.count(f"127.0.0.1:{setup_wizard.SEARXNG_PORT}:8080") == 1
    assert "RTBF_SEARXNG_BASE_URL" in (tmp_path / ".env").read_text()
    assert setup_wizard._load_service_state()["searxng"]["container_name"] == (
        setup_wizard.SEARXNG_CONTAINER_NAME
    )


async def test_setup_searxng_reports_docker_run_failure_without_writing_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(setup_wizard.shutil, "which", lambda _cmd: "/usr/bin/docker")
    monkeypatch.setattr(setup_wizard, "_docker_container_running", lambda _name: False)
    monkeypatch.setattr(setup_wizard, "_docker_rm_if_exists", lambda _name: None)

    class _Result:
        returncode = 1
        stderr = "port is already allocated"

    monkeypatch.setattr(setup_wizard.subprocess, "run", lambda *a, **k: _Result())

    io = _FakeIO(answers=["y"])
    await setup_wizard.setup_searxng(input_fn=io.input_fn, print_fn=io.print_fn)

    assert not (tmp_path / ".env").exists()
    assert any("failed to start searxng" in line.lower() for line in io.printed)


async def test_setup_searxng_asks_before_writing_url_when_health_check_times_out(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(setup_wizard.shutil, "which", lambda _cmd: "/usr/bin/docker")
    monkeypatch.setattr(setup_wizard, "_docker_container_running", lambda _name: False)
    monkeypatch.setattr(setup_wizard, "_docker_rm_if_exists", lambda _name: None)

    class _Result:
        returncode = 0
        stderr = ""

    monkeypatch.setattr(setup_wizard.subprocess, "run", lambda *a, **k: _Result())

    async def _fake_wait(url, *, timeout_seconds):
        return False

    monkeypatch.setattr(setup_wizard, "_wait_for_http_ok", _fake_wait)

    io = _FakeIO(answers=["y", "n"])  # confirm docker run, decline writing an unverified URL
    await setup_wizard.setup_searxng(input_fn=io.input_fn, print_fn=io.print_fn)

    assert not (tmp_path / ".env").exists()
    assert any("did not become healthy" in line.lower() for line in io.printed)


async def test_setup_searxng_declines_without_confirmation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(setup_wizard.shutil, "which", lambda _cmd: "/usr/bin/docker")
    monkeypatch.setattr(setup_wizard, "_docker_container_running", lambda _name: False)

    def _must_not_run(*_args, **_kwargs):
        raise AssertionError("docker run must not execute without confirmation")

    monkeypatch.setattr(setup_wizard.subprocess, "run", _must_not_run)

    io = _FakeIO(answers=["n"])
    await setup_wizard.setup_searxng(input_fn=io.input_fn, print_fn=io.print_fn)

    assert not (tmp_path / ".env").exists()


async def test_setup_searxng_reuses_already_running_container(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(setup_wizard.shutil, "which", lambda _cmd: "/usr/bin/docker")
    monkeypatch.setattr(setup_wizard, "_docker_container_running", lambda _name: True)

    def _must_not_run(*_args, **_kwargs):
        raise AssertionError("docker run must not execute when the container already exists")

    monkeypatch.setattr(setup_wizard.subprocess, "run", _must_not_run)

    async def _fake_wait(url, *, timeout_seconds):
        return True

    monkeypatch.setattr(setup_wizard, "_wait_for_http_ok", _fake_wait)

    io = _FakeIO(answers=[])
    await setup_wizard.setup_searxng(input_fn=io.input_fn, print_fn=io.print_fn)

    assert any("already running" in line.lower() for line in io.printed)
    assert "RTBF_SEARXNG_BASE_URL" in (tmp_path / ".env").read_text()


# --- Kanban ----------------------------------------------------------------------------------


async def test_setup_kanban_launches_process_after_confirmation_and_records_pid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class _FakeProcess:
        pid = 42

    launched: list[list[str]] = []

    def _fake_popen(command, **_kwargs):
        launched.append(command)
        return _FakeProcess()

    monkeypatch.setattr(setup_wizard.subprocess, "Popen", _fake_popen)

    async def _fake_wait(url, *, timeout_seconds):
        return True

    monkeypatch.setattr(setup_wizard, "_wait_for_http_ok", _fake_wait)

    io = _FakeIO(answers=["y"])
    await setup_wizard.setup_kanban(input_fn=io.input_fn, print_fn=io.print_fn)

    assert launched
    assert setup_wizard._load_service_state()["kanban"]["pid"] == 42


async def test_setup_kanban_declines_without_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _must_not_run(*_args, **_kwargs):
        raise AssertionError("uvicorn must not launch without confirmation")

    monkeypatch.setattr(setup_wizard.subprocess, "Popen", _must_not_run)

    io = _FakeIO(answers=["n"])
    await setup_wizard.setup_kanban(input_fn=io.input_fn, print_fn=io.print_fn)

    assert setup_wizard._load_service_state() == {}


async def test_setup_kanban_reuses_already_running_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup_wizard._save_service_state({"kanban": {"pid": 999, "port": 8000}})
    monkeypatch.setattr(setup_wizard, "_pid_running", lambda _pid: True)

    def _must_not_run(*_args, **_kwargs):
        raise AssertionError("uvicorn must not launch a second time")

    monkeypatch.setattr(setup_wizard.subprocess, "Popen", _must_not_run)

    io = _FakeIO(answers=[])
    await setup_wizard.setup_kanban(input_fn=io.input_fn, print_fn=io.print_fn)

    assert any("already running" in line.lower() for line in io.printed)


# --- orchestration + stop -------------------------------------------------------------------


async def test_run_setup_wizard_skips_every_piece_when_declined() -> None:
    io = _FakeIO(answers=["n", "n", "n", "n", "n"])
    await setup_wizard.run_setup_wizard(input_fn=io.input_fn, print_fn=io.print_fn)
    assert any("Setup complete" in line for line in io.printed)


async def test_run_setup_wizard_continues_past_one_piece_failing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _raising_setup(*, input_fn, print_fn):
        raise RuntimeError("boom")

    monkeypatch.setattr(setup_wizard, "setup_llm_provider", _raising_setup)
    io = _FakeIO(answers=["y", "n", "n", "n", "n"])

    await setup_wizard.run_setup_wizard(input_fn=io.input_fn, print_fn=io.print_fn)

    assert any("setup failed unexpectedly" in line.lower() for line in io.printed)
    assert any("Setup complete" in line for line in io.printed)


async def test_stop_services_reports_nothing_to_stop_when_state_is_empty() -> None:
    io = _FakeIO(answers=[])
    await setup_wizard.stop_services(print_fn=io.print_fn)
    assert any("nothing to stop" in line.lower() for line in io.printed)


async def test_stop_services_stops_searxng_container(monkeypatch: pytest.MonkeyPatch) -> None:
    setup_wizard._save_service_state({"searxng": {"container_name": "rtbf-agent-searxng"}})

    class _Result:
        returncode = 0
        stderr = ""

    ran_commands: list[list[str]] = []

    def _fake_run(command, **_kwargs):
        ran_commands.append(command)
        return _Result()

    monkeypatch.setattr(setup_wizard.subprocess, "run", _fake_run)

    io = _FakeIO(answers=[])
    await setup_wizard.stop_services(print_fn=io.print_fn)

    assert any("rm" in cmd and "-f" in cmd for cmd in ran_commands)
    assert setup_wizard._load_service_state() == {}


async def test_stop_services_stops_kanban_process(monkeypatch: pytest.MonkeyPatch) -> None:
    setup_wizard._save_service_state({"kanban": {"pid": 4242, "port": 8000}})
    monkeypatch.setattr(setup_wizard, "_pid_running", lambda _pid: True)

    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(setup_wizard.os, "kill", lambda pid, sig: killed.append((pid, sig)))

    io = _FakeIO(answers=[])
    await setup_wizard.stop_services(print_fn=io.print_fn)

    assert killed == [(4242, setup_wizard.signal.SIGTERM)]
    assert setup_wizard._load_service_state() == {}
