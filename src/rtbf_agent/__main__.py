"""`python -m rtbf_agent` entrypoint. See `rtbf_agent.cli` for the real orchestration (#36).

`load_dotenv()` runs unconditionally at import time, not inside the `if __name__ == "__main__"`
guard below (issue #84): the `rtbf-agent`/`forget` console scripts (`[project.scripts]` in
`pyproject.toml`) import this module and call `main` directly rather than executing it as a
script, so that guard never runs for them -- only top-level module code does. This is the one
place both invocation styles (`python -m rtbf_agent` and the installed console scripts) share, so
it's the one place `.env` loading has to live for the CLI to see it either way. Never overrides an
already-exported real environment variable (`load_dotenv()`'s own default, `override=False`).
"""

from dotenv import load_dotenv

load_dotenv()

from rtbf_agent.cli import main  # noqa: E402 -- must load .env before cli.py's config resolvers run

if __name__ == "__main__":
    main()
