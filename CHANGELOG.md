# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - unreleased

A from-scratch rewrite of `pi-mailbox` as `agent-mailbox`. It shares no code or data
format with pi-mailbox 0.1.0.

### Added

- **Storage:** SQLite (WAL mode) in a private state directory (`0700`/`0600`, checked at
  startup).
- **Identity with no environment variables:**
  - a UUIDv7 session id per run, and a temporary alias such as `claude-code-repo-7f3a`;
  - `set_role` mid-run gives a permanent `<client>-<project>-<role>` alias; mail sent to
    the temporary alias is forwarded to it;
  - a later session that sets the same role resumes the alias and inherits its open
    mail;
  - one live session per alias (flock);
  - `retire`, and owner-side `set-role` and `retire`.
- **Obligations:**
  - priorities (`urgent` / `normal` / `low`);
  - `defer` until task end or until a set time, with a note the sender can see through
    `status`;
  - `close` with a resolution;
  - replying closes the original (`keep_open` to opt out);
  - `wait` for new mail.
- **Claude Code hooks:**
  - a digest of open mail at session start and at each turn start;
  - urgent and normal mail injected once mid-task (never into subagents; low never);
  - a task-end gate that keeps deferred work from being forgotten, respects background
    tasks, and blocks at most twice per message before marking it overdue.
- **Pi extension** (`install pi` / `uninstall pi` / `doctor pi`): the same digest,
  mid-task notices and task-end gate as Claude Code, plus in-process wake-ups for idle
  Pi sessions. Shipped inside the wheel.
- **tmux wake-ups** for idle sessions, debounced.
- Optional `AGENT_MAILBOX_ROLE` (apply a role at server start) and `AGENT_MAILBOX_CLIENT`
  (override the detected client). The client is otherwise detected from the nearest
  `claude` / `pi` ancestor process, so a client started inside another one is named
  correctly.
- **CLI** for the human owner: `send`, `inbox`, `read`, `defer`, `close`, `status`
  (dashboard), `agents`, `set-role`, `retire`.
- **`install` / `uninstall` / `doctor claude-code`**, with a diff shown and confirmation
  asked before any change.
- Message ids can be given as any unique 8+ character fragment.
- **Limits:** rate, thread length, open-per-recipient, sizes.
- Headless end-to-end suite against real Claude Code and Pi (`e2e/run_e2e.py`).

[Unreleased]: https://github.com/a3harva/agent-mailbox/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/a3harva/agent-mailbox/releases/tag/v0.1.0
