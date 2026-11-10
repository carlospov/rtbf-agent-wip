"""Core package: plugin contracts, orchestration, and data models.

Per `CLAUDE.md`'s non-negotiables, nothing under this package may contain site-, language-, or
jurisdiction-specific logic (R20), and nothing here may import a concrete LLM SDK directly (R16).
See `docs/planning/RESTRICTIONS.md` for the full list of constraints this package must satisfy.
"""

from rtbf_agent.core.interfaces import (
    ApprovalDecision,
    DiscoveredAccount,
    DiscoverySource,
    LegalTemplate,
    LegalTemplateProvider,
    LLMMessage,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    NotificationMessage,
    Notifier,
)
from rtbf_agent.core.models import (
    CASE_STATES_IN_ORDER,
    DOCUMENT_EVENT_KINDS,
    Case,
    CaseState,
    Event,
    create_event,
    validate_document_event_detail,
)

__all__ = [
    "ApprovalDecision",
    "CASE_STATES_IN_ORDER",
    "Case",
    "CaseState",
    "DOCUMENT_EVENT_KINDS",
    "DiscoveredAccount",
    "DiscoverySource",
    "Event",
    "LLMMessage",
    "LLMProvider",
    "LLMRequest",
    "LLMResponse",
    "LegalTemplate",
    "LegalTemplateProvider",
    "NotificationMessage",
    "Notifier",
    "create_event",
    "validate_document_event_detail",
]
