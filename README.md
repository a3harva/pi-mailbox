# agent-mailbox

A durable local mailbox so coding agents on one machine can message each other.
**Claude Code** (through its hooks) and **Pi** (through the bundled Pi extension) are
supported fully. Any other MCP client gets the same tools at a best-effort level.

The important part is that mail can't be forgotten. Every delivered message stays an open
obligation until the agent closes it. Claude Code hooks keep open mail in front of the
agent at each turn, however long its context gets, and stop it from finishing a task
while important mail is still unhandled. The Pi extension does the same in Pi.

```
owner / another agent ── send(priority) ──▶  SQLite (~/.local/state/agent-mailbox)
                                                   │
 Claude Code session ◀── hooks: digest at turn start · urgent mail mid-task · gate at task end
        │
        └── read → reply (closes) │ defer("task_end", note) │ close(resolution)
```

## Quick start

macOS and Linux. Needs [uv](https://docs.astral.sh/uv/).

```sh
uvx agent-mailbox install claude-code   # shows a diff of your settings, asks first
uvx agent-mailbox doctor claude-code
```

That registers the MCP server (`claude mcp add --scope user`) and adds four hooks to
`~/.claude/settings.json`. `uninstall claude-code` removes exactly those entries.

For **Pi**:

```sh
uvx agent-mailbox install pi    # adds the server to ~/.pi/agent/mcp.json (diff, asks first)
uvx agent-mailbox doctor pi     # and the extension in ~/.pi/agent/extensions/agent-mailbox/
```

Restart Pi sessions (or `/reload`) to load it. `uninstall pi` removes both.

Start Claude Code anywhere. Then, from another session or your shell:

```sh
agent-mailbox agents                                   # who is out there
agent-mailbox send --to claude-code-myrepo-refactor-agent \
  --subject "Rename db helpers" --body "…" --priority normal
agent-mailbox status                                   # what is waiting on whom
```

## Identity: no setup per session

- Each session gets a fresh **session id** (UUIDv7) and, straight away, a temporary
  **alias** such as `claude-code-myrepo-7f3a` (`<client>-<project>-<4 hex>`).
- When the agent knows its purpose, or you tell it ("you're the refactor agent"), it calls
  `set_role("refactor-agent")`. It then becomes `claude-code-myrepo-refactor-agent`, and
  any mail sent to the temporary name follows it.
- **Aliases are permanent; sessions are not.** A later session that sets the same role
  takes the alias back, along with every open message on it, including ones a crashed
  session had deferred. Only one live session can hold an alias at a time.
- You can also rename a session from outside:
  `agent-mailbox set-role <alias-or-session> reviewer`.

No environment variables are needed. `AGENT_MAILBOX_DIR` only moves the state directory.
Two optional ones exist for launchers: `AGENT_MAILBOX_ROLE=<role>` applies `set_role` when
the server starts (handy for scripts that start named agents in tmux), and
`AGENT_MAILBOX_CLIENT` overrides the detected client name (normally the nearest `claude`
or `pi` process).

## Priorities, deferral and the task-end gate

| Priority | Mid-task (after a tool call) | Turn-start digest | Blocks finishing? |
|---|---|---|---|
| `urgent` | injected once, marked URGENT | yes | yes, until closed |
| `normal` | injected once | yes | yes, until closed or deferred to a set time |
| `low` | never | yes | **never** |

- A busy agent can `defer(id, until="task_end", note="mid-refactor")`. The sender sees the
  note through `status`. When the task really ends, the gate asks the agent to deal with
  the item.
- While background work (e.g. a long shell command) is still running, the task hasn't
  ended, so deferred items wait.
- **Loop guard:** each message can block finishing at most twice. After that it is
  marked *overdue* for you (`agent-mailbox status`), and the agent is allowed to stop.
- Replying with `reply_to` closes the original; pass `keep_open` to keep it open.
- Hooks only ever show headers (priority, sender, subject, age, id), never message
  bodies.

## Tools (MCP)

`whoami` · `agents` · `set_role` · `retire` · `send` · `inbox` · `read` · `defer` ·
`close` · `status` · `wait` (blocks until mail arrives, up to 300 s).

- Every result ends with a summary line such as
  `open: 1 urgent · 2 deferred (oldest 40m) · 0 new · 3 total`.
- `read`, `defer`, `close`, `status` and `reply_to` accept a full message id or any
  unique 8+ character piece of one.

## Waking idle sessions

**Pi with the extension** wakes in-process: the extension notices new urgent or normal
mail while Pi is idle and starts a run with the digest. No tmux needed.

Everything else uses **tmux**. If a session runs inside tmux, its pane is recorded.
Sending it mail while it is idle types `check your mailbox` into the pane, which starts a
turn and shows the digest:

- idle Claude Code sessions wake for urgent and normal mail;
- sessions without hooks or the extension wake only for urgent mail;
- busy sessions and low-priority mail never trigger a wake-up;
- at most one wake-up per session per minute.

## Support levels

| Level | Client | What you get |
|---|---|---|
| Full | Claude Code with hooks (`install claude-code`), Pi with the extension (`install pi`) | everything above |
| Best effort | Pi or Claude Code without them, other MCP clients | tools, summary line, `wait`, tmux wake-ups for urgent mail |

Claude Code and Pi sessions share one mailbox: they can mail each other directly.

## Limits

| What | Limit |
|---|---|
| Subject | 200 characters |
| Body | 100 KiB |
| Recipients | 64 |
| TTL | 1 year |
| Sends per alias | 30 per minute |
| Messages per thread | 50 (stops two agents ping-ponging forever) |
| Open messages per recipient | 1,000 |

## Security

Everything is local:
- stdio only, no listener;
- the state directory is `0700` and its files `0600`, checked at startup;
- message bodies are wrapped in delimiters and labelled `trust: "peer"`.

Processes running as your own Unix user can write the database, so `from` is a label,
not proof of who sent a message. See [SECURITY.md](SECURITY.md).

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md).
