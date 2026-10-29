# Contributing

Thanks for your interest in agent-mailbox.

## Ground rules

- **Keep it small and local.** Changes that add a socket, port, daemon or network access
  are out of scope.
- **No new runtime dependencies** (only `mcp`) without discussing it in an issue first.
- **Keep layers separate.** Mailbox logic lives in `mail.py` and `identity.py`.
  `server.py` (MCP), `hooks.py` (Claude Code) and `cli.py` stay thin.
- **The hook path must stay fast and must never import `mcp`.** A test checks this.

## Setup

Needs [uv](https://docs.astral.sh/uv/). Python 3.11 or newer.

```sh
uv sync
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

The unit and integration suite includes:
- real multi-process concurrency tests;
- stdio MCP servers driven by the MCP client;
- a real tmux wake-up test (skipped when tmux is missing);
- property-based fuzzing of the injection surfaces.

## End-to-end tests with real Claude Code and Pi

These need the `claude` CLI (or `pi`), logged in. They use isolated settings and state, so
your own `~/.claude`, Pi config and mailbox are never touched. They cost a little model
usage (Sonnet by default for Claude Code; Pi's configured default model for Pi).

```sh
uv run python e2e/run_e2e.py              # six headless scenarios
uv run python e2e/run_e2e.py --repeat 3   # required before a release
uv run python e2e/run_e2e.py --client pi  # the same scenarios on Pi
```

[`e2e/MANUAL.md`](e2e/MANUAL.md) is the interactive tmux checklist for both clients.

## Pull requests

- One change per pull request, with tests.
- Update `CHANGELOG.md` under "Unreleased".
- Describe user-visible behaviour changes in `README.md`.

## Security issues

Please don't open public issues for vulnerabilities; see [SECURITY.md](SECURITY.md).

## License

By contributing, you agree that your contributions are licensed under the Apache License
2.0 (see [LICENSE](LICENSE)).
