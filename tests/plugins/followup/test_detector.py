"""Tests for `ResponseClassifier` (issue #68): compose an `LLMProvider` into a reply-intent
*classification*, never an assignment.

No real LLM vendor anywhere -- the collaborator is a fake injected the same way every other
capability in this repo is tested (see `tests/plugins/jurisdiction_detection/test_detector.py`).
"""

from __future__ import annotations

import json

import pytest

from rtbf_agent.core.interfaces import LLMProvider, LLMRequest, LLMResponse
from rtbf_agent.plugins.followup.detector import (
    ReplyIntent,
    ResponseClassification,
    ResponseClassifier,
)


class _FakeLLMProvider(LLMProvider):
    """Returns a fixed, injected canned response -- never touches a real LLM vendor."""

    def __init__(self, content: str) -> None:
        self._content = content
        self.requests: list[LLMRequest] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        return LLMResponse(content=self._content, model=request.model)


class _RaisingLLMProvider(LLMProvider):
    """Simulates an LLM transport failure."""

    async def complete(self, request: LLMRequest) -> LLMResponse:
        raise RuntimeError("LLM provider unreachable")


async def _classify(content: str) -> ResponseClassification | None:
    classifier = ResponseClassifier(_FakeLLMProvider(content), model="mock-model")
    return await classifier.classify(
        site="Example Corp",
        domain="example.com",
        reply_subject="Re: Your data deletion request",
        reply_body="Hello, we have now deleted your account and all associated data. Regards.",
    )


async def test_classify_returns_confirmation_intent_and_reasoning() -> None:
    classification = await _classify(
        json.dumps(
            {"intent": "confirmation", "reasoning": "They state the account has been deleted."}
        )
    )
    assert classification is not None
    assert classification.intent is ReplyIntent.CONFIRMATION
    assert "deleted" in classification.reasoning


async def test_classify_returns_rejection_intent() -> None:
    classification = await _classify(
        json.dumps({"intent": "rejection", "reasoning": "They refuse the request."})
    )
    assert classification is not None
    assert classification.intent is ReplyIntent.REJECTION


async def test_classify_returns_additional_info_request_intent() -> None:
    classification = await _classify(
        json.dumps(
            {"intent": "additional_info_request", "reasoning": "They ask for ID verification."}
        )
    )
    assert classification is not None
    assert classification.intent is ReplyIntent.ADDITIONAL_INFO_REQUEST


async def test_classify_tolerates_json_wrapped_in_surrounding_prose() -> None:
    classification = await _classify(
        "Sure:\n```json\n"
        + json.dumps({"intent": "confirmation", "reasoning": "Deleted."})
        + "\n```\nAnything else?"
    )
    assert classification is not None
    assert classification.intent is ReplyIntent.CONFIRMATION


@pytest.mark.parametrize(
    "content",
    [
        json.dumps({"intent": None, "reasoning": "auto-acknowledgement"}),
        json.dumps({"intent": "UNCLEAR", "reasoning": "just an autoresponder"}),
        json.dumps({"intent": "  ", "reasoning": "blank"}),
        # An intent outside the fixed three-way vocabulary must never become a transition.
        json.dumps({"intent": "maybe_later", "reasoning": "model invented a class"}),
        json.dumps({"intent": "deletion_confirmed", "reasoning": "close but not a member"}),
        "not even json",
        json.dumps({"no_intent_key": "oops"}),
    ],
)
async def test_classify_returns_none_for_unclear_or_out_of_vocabulary_or_malformed(
    content: str,
) -> None:
    assert await _classify(content) is None


async def test_classify_propagates_an_llm_failure_rather_than_swallowing_it() -> None:
    classifier = ResponseClassifier(_RaisingLLMProvider(), model="m")
    with pytest.raises(RuntimeError, match="unreachable"):
        await classifier.classify(
            site="Example", domain="example.com", reply_subject="Re:", reply_body="hi"
        )


async def test_classify_sends_the_reply_body_and_subject_to_the_llm() -> None:
    llm = _FakeLLMProvider(json.dumps({"intent": "confirmation", "reasoning": "x"}))
    classifier = ResponseClassifier(llm, model="mock-model")
    await classifier.classify(
        site="Example Corp",
        domain="example.com",
        reply_subject="Re: deletion",
        reply_body="ACCOUNT REMOVED sentinel-body",
    )
    user_message = llm.requests[0].messages[-1].content
    assert "sentinel-body" in user_message
    assert "Re: deletion" in user_message


def test_response_classification_is_frozen() -> None:
    classification = ResponseClassification(intent=ReplyIntent.CONFIRMATION, reasoning="x")
    with pytest.raises(AttributeError):
        classification.intent = ReplyIntent.REJECTION  # type: ignore[misc]
