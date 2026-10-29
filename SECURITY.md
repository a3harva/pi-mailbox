# Security

## Reporting a vulnerability

Please report security issues privately through
[GitHub private vulnerability reporting](https://github.com/a3harva/agent-mailbox/security/advisories/new),
not in a public issue. Include steps to reproduce and the version or commit you tested.
You should get a response within a week.

## Threat model

agent-mailbox lets AI coding agents on **one machine, under one user account** leave each
other messages.

### In scope

**Other local users.**
- The state directory (`$XDG_STATE_HOME/agent-mailbox`, or `~/.local/state/agent-mailbox`)
  is created `0700`, and every file in it `0600`, whatever your umask is.
- On startup the tool refuses a directory that belongs to another user or that others
  can access.

**Nothing listens.**
- There is no socket, port, daemon or network code. The MCP server only talks over the
  stdio pipes of the client that started it.
- Hooks are short-lived processes that read the database and print JSON.

**Prompt injection between agents.** A message body is data from a peer, never an
instruction from the user.
- `read` wraps bodies in `<<<peer-message …>>> … <<<end-peer-message>>>` and adds
  `trust: "peer"`. Runs of angle brackets inside a body are broken up with zero-width
  spaces, so a body can't close its own wrapper.
- Hooks inject **headers only**, never bodies. Every injected string is reduced to one
  line, angle brackets are replaced, and it is length-capped.
- The server instructions and tool descriptions tell the model to treat bodies as
  information.

**Runaway agents.**
- per-alias send limit (30 per minute)
- per-thread message limit (50)
- per-recipient open limit (1,000)
- the task-end gate blocks at most twice per message, then marks it overdue instead of
  trapping the agent

**Resource exhaustion.**
- subject ≤200 characters, body ≤100 KiB, ≤64 recipients, TTL ≤1 year, `wait` ≤300 s

**Installer safety.**
- `install claude-code` / `install pi` show a unified diff and ask before writing.
- They back up the settings file, write atomically, and only add or remove their own
  entries: hooks whose command contains `agent-mailbox hook claude`, the `agent-mailbox`
  MCP entry, and the files in `~/.pi/agent/extensions/agent-mailbox/`.
- They refuse to touch a settings file that isn't valid JSON.

**Hooks fail open.** A broken or locked database, bad input, or any error produces empty
output and exit 0. The Pi extension ignores hook failures and timeouts the same way. The
mailbox can't break Claude Code or Pi.

**Client identity.** The server names its client from the nearest `claude` / `pi` ancestor
process, not from inherited environment variables, so a client started inside another
one cannot pick up the outer client's Claude session id and receive its hook output.

**Logging.** Debug logging (`AGENT_MAILBOX_DEBUG=1`) writes event names, ids and aliases to
`hooks.log` (`0600`), never subjects or bodies. Tests enforce this.

### Out of scope

- **Processes running as your own user.** They can read and write the database. The
  `from` field and the CLI's `owner` sender (marked `via: cli`) are labels, not
  authentication.
- **Secrets.** Messages are stored as plaintext. Don't put credentials in them.
- **Network delivery** and **Windows**.
