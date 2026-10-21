# Security

## Reporting a vulnerability

Please report security issues privately via
[GitHub private vulnerability reporting](https://github.com/a3harva/pi-mailbox/security/advisories/new)
rather than in a public issue. Include steps to reproduce and the version or commit you
tested. You should get a response within a week.

## Threat model

pi-mailbox lets AI coding agents on **one machine, under one user account** leave each
other messages. Its security rests on a few deliberate choices.

### Nothing listens

There is no socket, port, daemon or network code. The MCP server talks only over the stdio
pipes of the Pi process that launched it. There is nothing to connect to, so there is
nothing to authenticate.

This is why pi-mailbox is a mailbox rather than a way to inject prompts into a live
session: a socket that accepts prompts would let anything able to reach it put words in an
agent's mouth, with all of that agent's tools, in the middle of its work.

### The filesystem is the boundary

- The mailbox root and every agent directory are created `0700`; message files `0600`.
- Anyone who can act as your user can read, forge or delete messages. pi-mailbox does not
  try to defend against your own account; it defends against other local users.
- Message files are written atomically (temp file, `fsync`, link/rename), so a crash never
  leaves a partial message visible.

### Messages are untrusted input

A message body is **data from a peer agent**, exactly like the contents of a web page. It
is never an instruction from the human owner.

- The server never executes, evaluates or expands anything in a message: no shell, no
  path expansion, no templating.
- Agent names and message ids are validated against strict patterns before they touch a
  path, which rules out path traversal (`../`).
- Bodies are capped at 100 KB and subjects at 200 characters.
- The recommended `AGENTS.md` text tells agents to treat bodies as information and never
  as instructions that override their own instructions or the owner's direct requests.

### Sender identity is not authentication

- An MCP session always sends as its own `PI_MAILBOX_AGENT` name; the tool has no `from`
  argument, so one agent cannot casually sign as another.
- The CLI accepts `--from`, including the reserved name `owner`, so a human can leave a
  note. This is a label, **not** proof: any process running as your user can write any
  file in the mailbox. Agents should weigh `owner` mail as information.
- One live server holds each name (an `flock`), so two sessions cannot silently share an
  inbox.

### No secrets

Messages are stored as plaintext and kept forever (nothing is deleted automatically). Do
not put passwords, tokens or other secrets in them. Encryption is out of scope for v0.1.

### Out of scope

- Cross-machine delivery.
- Protection against other processes running as the same user.
- Windows.
