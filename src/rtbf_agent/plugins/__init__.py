"""Concrete plugin implementations of the contracts in `rtbf_agent.core.interfaces`.

Every discovery source, legal template provider, notifier, and LLM adapter lives under a
subpackage here -- never in `core/` (dependencies point the other way) -- see
`docs/planning/RESTRICTIONS.md` R20/R21.
"""
