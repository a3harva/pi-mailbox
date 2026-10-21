# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-10-21

### Added

- Stdio MCP server (`pi-mailbox serve`) with four tools: `send`, `inbox`, `read`, `ack`.
- Durable, atomic, per-message JSON storage under `~/.pi-mailbox` (or `PI_MAILBOX_DIR`).
- Identity from `PI_MAILBOX_AGENT` only, with an `flock` so one name has one live server.
- Broadcast (`"all"` or a list), threads via `reply_to`, and per-message TTL expiry;
  `send` echoes `ttl_seconds` and `expires_at` when a TTL is set.
- CLI with `send`, `inbox`, `read`, `ack` and `nudge` (tmux) subcommands, printing JSON.
- `owner` sender for notes from a human via the CLI.

[Unreleased]: https://github.com/a3harva/pi-mailbox/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/a3harva/pi-mailbox/releases/tag/v0.1.0
