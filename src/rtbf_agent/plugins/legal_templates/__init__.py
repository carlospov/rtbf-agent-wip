"""Concrete `LegalTemplateProvider` implementations (spec S3.1, R22/R24).

`jurisdictions/` holds legal-basis/citation content, one module per jurisdiction. `languages/`
holds phrasing/grammar/tone content, one module per drafting language. Neither subpackage imports
from or references the other -- `provider.py` is the only place the two axes meet, and it contains
no jurisdiction- or language-specific content of its own, only lookup/composition logic. This is
what makes jurisdiction and drafting language independent axes (R24): adding a second jurisdiction
never touches `languages/`, and adding a second drafting language never touches `jurisdictions/`.
"""
