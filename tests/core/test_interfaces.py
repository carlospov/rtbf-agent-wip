"""Tests for `rtbf_agent.core.interfaces` — the plugin-family contract layer.

Covers:
- Each ABC cannot be instantiated directly (it's a contract, not an implementation).
- Each ABC's docstring names at least one `R#` restriction it enforces.
- A minimal stub can implement each contract without needing anything else from `core/`.
- The two static/AST checks demanded by issue #6 (R16, R20): no core file imports a concrete LLM
  SDK, and no core file contains a hardcoded jurisdiction/language literal.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from rtbf_agent.core.interfaces import (
    DiscoveredAccount,
    DiscoverySource,
    EmailMessage,
    I18nProvider,
    LegalTemplate,
    LegalTemplateProvider,
    LLMMessage,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    NotificationMessage,
    Notifier,
    SearchResult,
    Sender,
    WebSearchProvider,
)

CORE_DIR = Path(__file__).resolve().parents[2] / "src" / "rtbf_agent" / "core"

# --------------------------------------------------------------------------------------------- #
# Each ABC is a contract, not an implementation — it must reject direct instantiation.
# --------------------------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "abc_cls",
    [
        DiscoverySource,
        LegalTemplateProvider,
        Notifier,
        Sender,
        LLMProvider,
        I18nProvider,
        WebSearchProvider,
    ],
)
def test_abc_cannot_be_instantiated_directly(abc_cls: type) -> None:
    with pytest.raises(TypeError):
        abc_cls()  # type: ignore[abstract]


# --------------------------------------------------------------------------------------------- #
# Every ABC's docstring must name the R# restriction(s) it exists to enforce (CLAUDE.md
# convention, called out explicitly in issue #6's verification checklist).
# --------------------------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "abc_cls",
    [
        DiscoverySource,
        LegalTemplateProvider,
        Notifier,
        Sender,
        LLMProvider,
        I18nProvider,
        WebSearchProvider,
    ],
)
def test_abc_docstring_names_a_restriction(abc_cls: type) -> None:
    docstring = abc_cls.__doc__ or ""
    assert re.search(r"\bR\d+\b", docstring), (
        f"{abc_cls.__name__}'s docstring does not name an R# restriction: {docstring!r}"
    )


def test_discovery_source_docstring_names_r20_and_r21() -> None:
    assert "R20" in (DiscoverySource.__doc__ or "")
    assert "R21" in (DiscoverySource.__doc__ or "")


def test_legal_template_provider_docstring_names_r20_r21_r24() -> None:
    doc = LegalTemplateProvider.__doc__ or ""
    for restriction in ("R20", "R21", "R24"):
        assert restriction in doc


def test_notifier_docstring_names_r15_r20_r21() -> None:
    doc = Notifier.__doc__ or ""
    for restriction in ("R15", "R20", "R21"):
        assert restriction in doc


def test_sender_docstring_names_r1_r12_r13_r14_r21() -> None:
    doc = Sender.__doc__ or ""
    for restriction in ("R1", "R12", "R13", "R14", "R21"):
        assert restriction in doc


def test_sender_has_exactly_one_abstract_method() -> None:
    assert Sender.__abstractmethods__ == frozenset({"send"})


def test_notifier_has_exactly_one_abstract_method() -> None:
    """One-way `notify()` only (automated-case-loop rework) -- `request_approval()`/
    `receive_document()`/`receive_text_response()`/`download_document_bytes()` are gone; every
    decision is recorded via `case_actions.py` instead of a blocking wait on this interface."""
    assert Notifier.__abstractmethods__ == frozenset({"notify"})


def test_llm_provider_docstring_names_r16_r17_r18() -> None:
    doc = LLMProvider.__doc__ or ""
    for restriction in ("R16", "R17", "R18"):
        assert restriction in doc


def test_i18nprovider_docstring_names_r23_and_r21() -> None:
    doc = I18nProvider.__doc__ or ""
    assert "R23" in doc
    assert "R21" in doc


def test_websearchprovider_docstring_names_r16_r20_r21() -> None:
    doc = WebSearchProvider.__doc__ or ""
    for restriction in ("R16", "R20", "R21"):
        assert restriction in doc


# --------------------------------------------------------------------------------------------- #
# LLMProvider must stay a single narrow method, not vendor-shaped methods (per
# docs/architecture/llm-provider-patterns.md).
# --------------------------------------------------------------------------------------------- #


def test_llm_provider_has_exactly_one_abstract_method() -> None:
    assert LLMProvider.__abstractmethods__ == frozenset({"complete"})


def test_websearchprovider_has_exactly_one_abstract_method() -> None:
    assert WebSearchProvider.__abstractmethods__ == frozenset({"search"})


# --------------------------------------------------------------------------------------------- #
# A throwaway stub can implement each contract with nothing beyond the interface itself — proof
# that a real plugin never needs to touch this file again (R21).
# --------------------------------------------------------------------------------------------- #


class _StubDiscoverySource(DiscoverySource):
    async def discover(self, target_email: str) -> list[DiscoveredAccount]:
        return [
            DiscoveredAccount(
                site="Example",
                domain="example.invalid",
                target_email=target_email,
                evidence={"matched_pattern": "welcome-email"},
            )
        ]


class _StubLegalTemplateProvider(LegalTemplateProvider):
    def get_template(self, jurisdiction: str, drafting_language: str) -> LegalTemplate:
        return LegalTemplate(
            jurisdiction=jurisdiction,
            drafting_language=drafting_language,
            legal_basis="stub-basis",
            body_template="stub template body",
        )

    def supported_jurisdictions(self) -> list[str]:
        return ["stub-jurisdiction"]

    def supported_languages(self, jurisdiction: str) -> list[str]:
        return ["stub-language"]


class _StubNotifier(Notifier):
    async def notify(self, message: NotificationMessage) -> None:
        return None


class _StubSender(Sender):
    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []

    async def send(self, message: EmailMessage) -> None:
        self.sent.append(message)


class _StubLLMProvider(LLMProvider):
    async def complete(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(content="stub response", model=request.model)


class _StubI18nProvider(I18nProvider):
    def translate(self, key: str, lang: str, **params: object) -> str:
        if key == "missing":
            raise LookupError(f"no translation for {key!r} in {lang!r}")
        return f"[{lang}] {key}" if not params else f"[{lang}] {key} {params}"


class _StubWebSearchProvider(WebSearchProvider):
    async def search(self, query: str) -> list[SearchResult]:
        return [
            SearchResult(
                title=f"Result for {query}",
                url="https://example.invalid/result",
                snippet="stub snippet",
            )
        ]


async def test_stub_discovery_source_implements_contract() -> None:
    source = _StubDiscoverySource()
    accounts = await source.discover("user@example.invalid")
    assert accounts and isinstance(accounts[0], DiscoveredAccount)


def test_stub_legal_template_provider_implements_contract() -> None:
    provider = _StubLegalTemplateProvider()
    template = provider.get_template("stub-jurisdiction", "stub-language")
    assert isinstance(template, LegalTemplate)
    assert provider.supported_jurisdictions() == ["stub-jurisdiction"]
    assert provider.supported_languages("stub-jurisdiction") == ["stub-language"]


async def test_stub_notifier_implements_contract() -> None:
    notifier = _StubNotifier()
    await notifier.notify(NotificationMessage(case_id="c1", text="hello"))


async def test_stub_sender_implements_contract() -> None:
    sender = _StubSender()
    await sender.send(
        EmailMessage(case_id="c1", to_address="controller@example.invalid", subject="s", body="b")
    )
    assert len(sender.sent) == 1
    assert sender.sent[0].to_address == "controller@example.invalid"


async def test_stub_llm_provider_implements_contract() -> None:
    provider = _StubLLMProvider()
    response = await provider.complete(
        LLMRequest(messages=[LLMMessage(role="user", content="hi")], model="stub-model")
    )
    assert isinstance(response, LLMResponse)
    assert response.model == "stub-model"


def test_stub_i18nprovider_implements_contract() -> None:
    """A minimal stub can implement `I18nProvider` with nothing beyond the interface (R21)."""
    provider = _StubI18nProvider()
    assert isinstance(provider, I18nProvider)
    assert provider.translate("greeting", "some-lang") == "[some-lang] greeting"


def test_missing_key_raises_rather_than_returning_the_key_itself() -> None:
    provider = _StubI18nProvider()
    with pytest.raises(LookupError):
        provider.translate("missing", "some-lang")


async def test_websearchprovider_implements_contract() -> None:
    """A minimal stub can implement `WebSearchProvider` with nothing beyond the interface (R21)."""
    provider = _StubWebSearchProvider()
    assert isinstance(provider, WebSearchProvider)
    results = await provider.search("example query")
    assert results and isinstance(results[0], SearchResult)
    assert results[0].title == "Result for example query"


def test_llm_response_raw_is_immutable_at_runtime() -> None:
    """`raw` is typed `Mapping`; it must also *behave* like one, not just a `dict` in disguise."""
    response = LLMResponse(content="hi", model="stub-model", raw={"usage": {"tokens": 3}})
    with pytest.raises(TypeError):
        response.raw["usage"] = "mutated"  # type: ignore[index]


def test_llm_request_messages_is_immutable_at_runtime() -> None:
    """`messages` is typed `Sequence`; a caller-supplied `list` must not stay mutable in place."""
    shared_messages = [LLMMessage(role="user", content="hi")]
    request = LLMRequest(messages=shared_messages, model="stub-model")
    shared_messages.append(LLMMessage(role="user", content="sneaky append"))
    assert len(request.messages) == 1
    assert isinstance(request.messages, tuple)


# --------------------------------------------------------------------------------------------- #
# R16 — no file under core/ imports a concrete LLM SDK directly.
# --------------------------------------------------------------------------------------------- #

KNOWN_LLM_SDK_PACKAGES = {
    "anthropic",
    "openai",
    "cohere",
    "mistralai",
    "google.generativeai",
    "ollama",
    "groq",
    "together",
    "replicate",
    "boto3",  # bedrock access commonly goes through boto3; still a concrete-SDK path
}
_KNOWN_LLM_SDK_TOP_LEVEL_NAMES = {pkg.split(".")[0] for pkg in KNOWN_LLM_SDK_PACKAGES}


def _iter_core_py_files() -> list[Path]:
    return sorted(CORE_DIR.rglob("*.py"))


def test_core_has_no_direct_llm_sdk_import() -> None:
    offenders: list[str] = []
    for path in _iter_core_py_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    if top in _KNOWN_LLM_SDK_TOP_LEVEL_NAMES:
                        offenders.append(f"{path}:{node.lineno}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom) and node.module:
                top = node.module.split(".")[0]
                if top in _KNOWN_LLM_SDK_TOP_LEVEL_NAMES:
                    offenders.append(f"{path}:{node.lineno}: from {node.module} import ...")
    assert not offenders, "core/ imports a concrete LLM SDK directly:\n" + "\n".join(offenders)


# --------------------------------------------------------------------------------------------- #
# R20 — no hardcoded jurisdiction code or language literal lives in core/ (outside type
# annotations/docstrings, which are exempt since they only document the *shape* of a plugin's
# data, not a specific site/language/jurisdiction's logic).
# --------------------------------------------------------------------------------------------- #

# ISO 639-1 language codes (a representative maintained subset, not exhaustive — enough to catch
# a hardcoded literal if one ever appears in core/).
ISO_LANGUAGE_CODES = {
    "en",
    "es",
    "fr",
    "de",
    "it",
    "pt",
    "nl",
    "ja",
    "zh",
    "ko",
    "ru",
    "ar",
    "pl",
    "sv",
    "tr",
    "he",
    "hi",
    "da",
    "fi",
    "no",
    "cs",
    "el",
    "ro",
    "hu",
}

# Common jurisdiction/legal-regime codes a "no hardcoded jurisdiction" check should catch.
JURISDICTION_CODES = {
    "GDPR",
    "CCPA",
    "CPRA",
    "LGPD",
    "PIPEDA",
    "POPIA",
    "APPI",
    "PDPA",
    "EU",
    "US",
    "UK",
    "BR",
    "CA",
    "IN",
    "AU",
    "JP",
    "KR",
    "ZA",
}


def _is_docstring_like_expr(node: ast.AST) -> bool:
    """True for any bare string-literal expression statement.

    Covers module/class/function docstrings (PEP 257) *and* the attribute-docstring convention
    used in this module (a bare string literal immediately following a dataclass field), since
    both are documentation, not data a plugin would depend on.
    """
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


def test_no_hardcoded_jurisdiction_or_language_literals_in_core() -> None:
    docstring_like_nodes: set[int] = set()
    offenders: list[str] = []

    for path in _iter_core_py_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

        # First pass: mark every docstring-like expression statement's Constant node so the
        # second pass can skip it (docstrings/attribute-docstrings are exempt).
        for node in ast.walk(tree):
            if _is_docstring_like_expr(node):
                docstring_like_nodes.add(id(node.value))  # type: ignore[attr-defined]

        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if id(node) in docstring_like_nodes:
                continue
            value = node.value
            if value in ISO_LANGUAGE_CODES or value.upper() in JURISDICTION_CODES:
                offenders.append(f"{path}:{node.lineno}: literal {value!r}")

    assert not offenders, (
        "core/ contains a hardcoded jurisdiction/language literal outside a docstring:\n"
        + "\n".join(offenders)
    )
