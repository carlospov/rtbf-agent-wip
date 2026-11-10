"""FastAPI kanban UI package (issue #53). See `rtbf_agent.api.app` for the full design rationale.

`app` is re-exported here so `uv run uvicorn rtbf_agent.api:app --reload` works without callers
needing to know it actually lives in `rtbf_agent.api.app`.
"""

from __future__ import annotations

from rtbf_agent.api.app import app

__all__ = ["app"]
