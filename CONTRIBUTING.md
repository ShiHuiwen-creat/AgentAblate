# Contributing to AgentAblate

Thank you for helping make coding-agent experiments easier to reproduce.

## Development setup

AgentAblate supports Python 3.11 or newer. With [uv](https://docs.astral.sh/uv/):

```bash
uv venv
uv pip install -e '.[dev]'
```

Or use a standard virtual environment and pip:

```bash
python -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
```

On Windows, use `.venv\\Scripts\\python` in place of `.venv/bin/python`.

## Test-driven changes

Use a RED-GREEN cycle for behavior changes: write one focused test, run it and
confirm the expected failure, then make the smallest implementation that passes.
Before opening a pull request, run:

```bash
pytest --cov=agentablate --cov-report=term-missing --cov-fail-under=90
ruff check .
```

## Adapter contract

Adapters implement `doctor()` and asynchronous `run(trial, cwd, *, on_event=None)`.
Return an `AdapterResult`, emit structured `AgentEvent` records through the optional
`EventSink`, and preserve partial results during timeout or cancellation. New
adapters must terminate the whole child process tree and include cancellation,
timeout, event-stream, and redaction tests.

Credentials are opt-in. Pass only explicitly allowlisted environment variables to
agent processes; never inherit every secret from the parent environment, log secret
values, or commit credentials and local absolute paths.

`codex-exec` adapter tests must replace the Codex executable with a local fake
script. Automated tests and CI must not run live Codex, consume quota, require
network access, or depend on a logged-in account. Keep fixtures, logs, reports, and
recorded events free of credentials, tokens, private account details, and local
absolute paths.

## Commits and pull requests

Keep commits focused, use an imperative subject, and avoid unrelated formatting.
Pull requests should explain the motivation, document the RED and GREEN commands,
list verification results, and update examples or documentation when behavior is
user-visible. A draft PR is welcome for early feedback.
