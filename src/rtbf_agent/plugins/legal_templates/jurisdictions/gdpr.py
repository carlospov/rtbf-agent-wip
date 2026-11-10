"""GDPR (EU) jurisdiction content -- legal basis only, no phrasing/grammar (R24).

Holds exactly one thing: the citation/legal-basis text a deletion request needs to invoke the GDPR
jurisdiction's "right to erasure". This module is never touched when a new drafting language is
added under `plugins/legal_templates/languages/` -- that independence is this issue's whole point
(R22, R24). It in turn never imports or references anything from `languages/`.
"""

from __future__ import annotations

JURISDICTION_CODE = "GDPR"
"""Registry key this jurisdiction is looked up under -- see `jurisdictions/__init__.py`."""

LEGAL_BASIS = (
    "Regulation (EU) 2016/679 (General Data Protection Regulation), Article 17 "
    "(\"Right to erasure ('right to be forgotten')\")"
)
"""Citation text for the GDPR right to erasure. Kept as a single language-invariant citation
string (the regulation name and article number are conventionally cited as-is regardless of the
drafting language) rather than translated per-language content -- translating a jurisdiction's own
citation is explicitly out of scope for this issue (see the GitHub issue's "Not in scope" section);
a real second-language rollout would revisit this if a jurisdiction's citation convention actually
needs translation, but that is a jurisdiction-content decision, made here, never in `languages/`.
"""
