# Manual end-to-end checklist (interactive, tmux)

Runs the whole mailbox flow with real interactive Claude Code sessions. The headless suite
(`run_e2e.py`) covers the same rules non-interactively. This checklist adds what only a
live terminal can show: two concurrent sessions, idle wake-ups, and a crash.

## Setup (isolated: your own ~/.claude is untouched)

```sh
E=$(mktemp -d); mkdir $E/proj && git -C $E/proj init -q
EXE=$(uv run which agent-mailbox)
export AGENT_MAILBOX_DIR=$E/state AGENT_MAILBOX_DEBUG=1
$EXE install claude-code --settings-file $E/settings.json --mcp-config $E/mcp.json --command $EXE --yes
cat > $E/launch.sh <<EOF
#!/bin/zsh
export AGENT_MAILBOX_DIR=$E/state AGENT_MAILBOX_DEBUG=1
cd $E/proj
exec claude --model sonnet --setting-sources project --settings $E/settings.json \
  --mcp-config $E/mcp.json --strict-mcp-config \
  --allowedTools "mcp__agent-mailbox" "Bash(sleep:*)" "Write" "Read"
EOF
chmod +x $E/launch.sh
```

Seed an offline role: `echo "set_role refactor-agent" | claude -p …`, then send it two
normal-priority messages with `agent-mailbox send`.

## Steps and expected results

| # | Action | Expect |
|---|---|---|
| 1 | `tmux new -d -s a $E/launch.sh`; ask "what is your mailbox address and which roles could you resume?" (no tools) | a temporary alias `claude-code-proj-xxxx`; `refactor-agent (2 open)` |
| 2 | "You are the refactor agent: take that role, then run `sleep 20` three times without reading mail first" | `set_role` resumes the alias; the 2 carried items are surfaced once |
| 3 | Session B (`docs-agent`) sends A a **low**-priority message mid-task | nothing injected into A (`post-tool-use -> False` in `hooks.log`) |
| 4 | `agent-mailbox send --priority urgent …` to A mid-task | the next tool call injects URGENT; A reads and closes it |
| 5 | A defers mail to task end | `agent-mailbox status` shows the defer note |
| 6 | A tries to finish | "Stop hook feedback: before finishing, deal with N open message(s)"; A handles them; low priority never blocks |
| 7 | A idle; `agent-mailbox send` a normal message | `check your mailbox` typed into A's pane; A handles it and replies |
| 8 | A defers a message, then `tmux kill-session -t a`; start C and `set_role refactor-agent` | C's session start shows `refactor-agent (1 open)`; C inherits the deferred item and its note |
| 9 | Prompt B and C at the same time (tool calls in both) | every `hooks.log` line maps each Claude session id to its own mailbox session |

## Pi (with the agent-mailbox extension)

Same steps with Pi sessions. Isolate it the same way: a scratch git project with the MCP
entry in the project's `.pi/mcp.json` (Pi reads it with `--approve`) and the extension
loaded with `-e`, so your global Pi config is untouched:

```sh
$EXE install pi --mcp-config $E/proj/.pi/mcp.json --extension-dir $E/ext/agent-mailbox \
  --command $EXE --yes
# launch.sh: export AGENT_MAILBOX_DIR=$E/state AGENT_MAILBOX_DEBUG=1
#            unset <vars that activate your own extensions>; cd $E/proj
#            exec pi --approve --no-session -e $E/ext/agent-mailbox/index.ts
```

Session B can be started with `AGENT_MAILBOX_ROLE=docs-agent` to test the launcher hint.
Differences from Claude Code: hook lines in `hooks.log` read `pi <event>: pid <pi pid> ->
session …`; step 7 is an in-process wake (`idle-check -> True`, then `turn-start`), and
nothing is typed into the pane.
