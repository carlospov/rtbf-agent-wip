"""English drafting-language content -- phrasing/grammar/tone only, no legal basis (R24).

Provides the body-template grammar/structure for English-language deletion requests. Contains a
`{legal_basis}` placeholder that a composing `LegalTemplateProvider` fills in from whichever
jurisdiction module was requested -- this file never names a specific jurisdiction, regulation, or
article, so a second jurisdiction added later needs zero changes here. It in turn never imports or
references anything from `jurisdictions/`.
"""

from __future__ import annotations

LANGUAGE_CODE = "en"
"""Registry key this language is looked up under -- see `languages/__init__.py`."""

BODY_TEMPLATE = (
    "To whom it may concern,\n"
    "\n"
    "I am writing to request the deletion of all personal data you hold about me, "
    "associated with the email address {target_email}, in connection with my account on "
    "{site} ({domain}).\n"
    "\n"
    "This request is made pursuant to {legal_basis}.\n"
    "\n"
    "Please confirm in writing once this deletion has been completed, and let me know if you "
    "require any further information to process this request.\n"
    "\n"
    "Sincerely,\n"
    "{requester_name}"
)
"""Format-string body template. Placeholders are all generic/jurisdiction-agnostic: `{site}`,
`{domain}`, and `{target_email}` mirror `DiscoveredAccount` fields (`core/interfaces.py`);
`{legal_basis}` and `{requester_name}` are filled in by the caller/composing provider at draft
time -- never by this module itself, which supplies only phrasing."""
