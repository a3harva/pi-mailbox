# Contributing

Thanks for your interest in pi-mailbox.

## Ground rules

- Keep it small and boring. The value of this project is that it is easy to read in one
  sitting and has nothing listening on the machine. Changes that add a socket, port,
  daemon or network access are out of scope.
- No new runtime dependencies without discussion in an issue first.
- The MCP server and the CLI share one implementation (`src/pi_mailbox/core.py`). New
  behaviour goes there, not into one front end.
- Message files must stay plain, human-readable JSON.

## Setup

Requires Python 3.14+ and [uv](https://docs.astral.sh/uv/).

```sh
uv sync
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

The tests include real multi-process concurrency checks and an end-to-end run of two
`pi-mailbox serve` processes over stdio. The tmux nudge test is skipped when tmux is not
installed.

## Pull requests

- One change per pull request, with tests.
- Update `CHANGELOG.md` under "Unreleased".
- Describe user-visible behaviour changes in `README.md`.

## Security issues

Please do not open public issues for vulnerabilities; see [SECURITY.md](SECURITY.md).

## License

By contributing, you agree that your contributions are licensed under the Apache License
2.0 (see [LICENSE](LICENSE)).
