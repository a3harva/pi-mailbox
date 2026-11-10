# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- **Stalled deliveries:**
  - an idle session is woken again for mail that arrives within a minute of its last
    wake-up, once it has finished the turn that wake-up started;
  - the task-end gate and going idle happen in one transaction, so a message can no
    longer land in between and sit unseen;
  - `wait` returns mail that arrived before it was called or whose sender's clock or
    commit ran late, and accepts any message id of the thread as `thread_id`;
  - a turn that ends on an API error (new `StopFailure` hook) or that the user interrupts
    no longer leaves the session marked busy for good;
  - compaction in the middle of a turn no longer marks the session idle.
- **Sessions and aliases:**
  - a server starting at the same moment as another could be marked ended and then never
    be found by its hooks; a live session now also repairs that on its next tool call;
  - `set_role` could fail with "in use" while another process was only probing the lock;
  - a resumed Claude Code conversation takes its previous role back;
  - a failed owner `set-role` request no longer fails the agent's unrelated tool call;
  - a client started inside another client no longer records the outer client's tmux
    pane, so wake-ups are not typed into the wrong session.
- **Sending:**
  - a bare role name resolves to the one agent that has it instead of queueing for an
    alias nobody can ever claim;
  - mail to the temporary address of an ended session is refused; the result lists
    recipients that are `offline`, and `status` shows whether each one is `live`;
  - a broadcast skips recipients whose inbox is full instead of failing for everyone.
- **Hardening:**
  - subjects are reduced to one printable line; reminders and the CLI's text output drop
    control and invisible characters (terminal escape sequences, bidi overrides);
  - the roles `owner` and `all` are reserved; `--as` validates the alias;
  - the installer recognises its hooks when the command path needs quoting, and keeps a
    symlinked settings file a symlink;
  - hooks give up on a locked database after 2 s instead of being killed by the client.

Run `agent-mailbox install claude-code` again to add the `StopFailure` hook.

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
