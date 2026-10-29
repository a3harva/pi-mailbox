/**
 * agent-mailbox for Pi: turn-start digests, mid-task notices, the task-end gate and
 * in-process wake-ups.
 *
 * The extension holds no mailbox logic. Each event runs `agent-mailbox hook pi <event>`
 * with Pi's pid, and the shared hook core answers with text to inject. Every failure is
 * ignored: a broken mailbox never breaks Pi.
 *
 * Installed by `agent-mailbox install pi`, which writes config.json next to this file.
 */
import { spawn } from "node:child_process";
import { readFileSync, statSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";

const PROTOCOL = 1;
const HOOK_TIMEOUT_MS = 3_000;
const START_TIMEOUT_MS = 8_000;
const POLL_MS = 2_000;
const MESSAGE_TYPE = "agent-mailbox";
const WAKE_TYPE = "agent-mailbox-wake";

type HookReply = { text?: string; continue?: boolean; wake?: boolean };

function extensionDir(): string {
	try {
		return dirname(fileURLToPath(import.meta.url));
	} catch {
		return join(homedir(), ".pi", "agent", "extensions", "agent-mailbox");
	}
}

function loadCommand(): string[] | undefined {
	try {
		const config = JSON.parse(readFileSync(join(extensionDir(), "config.json"), "utf8"));
		if (config.protocol !== PROTOCOL) return undefined;
		const command = config.command;
		return Array.isArray(command) && command.length > 0 && command.every((c) => typeof c === "string")
			? command
			: undefined;
	} catch {
		return undefined;
	}
}

/** Same rule as agent_mailbox.paths.state_dir. */
function stateDir(): string {
	const explicit = process.env.AGENT_MAILBOX_DIR;
	if (explicit) return explicit.replace(/^~(?=\/|$)/, homedir());
	const xdg = process.env.XDG_STATE_HOME;
	return join(xdg || join(homedir(), ".local", "state"), "agent-mailbox");
}

/** Changes whenever anything writes to the database, so idle polls and tool results
 * can skip the hook process when nothing happened. */
function dbSignature(dir: string): string {
	const parts: string[] = [];
	for (const name of ["mailbox.db", "mailbox.db-wal"]) {
		try {
			const s = statSync(join(dir, name));
			parts.push(`${s.mtimeMs}:${s.size}`);
		} catch {
			parts.push("-");
		}
	}
	return parts.join("|");
}

function runHook(command: string[], event: string, data: Record<string, unknown>, timeoutMs = HOOK_TIMEOUT_MS) {
	return new Promise<HookReply | undefined>((resolve) => {
		let out = "";
		let done = false;
		const finish = (value: HookReply | undefined) => {
			if (!done) {
				done = true;
				clearTimeout(timer);
				resolve(value);
			}
		};
		let child;
		try {
			child = spawn(command[0], [...command.slice(1), "hook", "pi", event], {
				stdio: ["pipe", "pipe", "ignore"],
			});
		} catch {
			return resolve(undefined);
		}
		const timer = setTimeout(() => {
			child.kill("SIGKILL");
			finish(undefined);
		}, timeoutMs);
		child.on("error", () => finish(undefined));
		child.stdout.on("data", (chunk: Buffer) => {
			out += chunk.toString("utf8");
		});
		child.on("close", () => {
			try {
				finish(out.trim() ? (JSON.parse(out) as HookReply) : undefined);
			} catch {
				finish(undefined);
			}
		});
		child.stdin.on("error", () => {});
		child.stdin.end(JSON.stringify({ client_pid: process.pid, ...data }));
	});
}

export default function agentMailbox(pi: ExtensionAPI) {
	const command = loadCommand();
	if (!command) return;
	const dir = stateDir();

	let ctx: ExtensionContext | undefined;
	let firstTurn = true;
	let seen = "";
	let timer: ReturnType<typeof setInterval> | undefined;
	let polling = false;

	const hook = async (event: string, data: Record<string, unknown> = {}, timeoutMs?: number) => {
		const reply = await runHook(command, event, data, timeoutMs);
		seen = dbSignature(dir); // our own writes are not news
		return reply;
	};

	const stopPolling = () => {
		if (timer) clearInterval(timer);
		timer = undefined;
	};

	// While idle, wake the agent in-process when urgent or normal mail arrives.
	const poll = async () => {
		if (polling || !ctx || !ctx.isIdle() || ctx.hasPendingMessages()) return;
		const now = dbSignature(dir);
		if (now === seen) return;
		polling = true;
		try {
			const reply = await hook("idle-check");
			if (reply?.wake && reply.text && ctx.isIdle()) {
				pi.sendMessage({ customType: WAKE_TYPE, content: reply.text, display: true }, { triggerTurn: true });
			}
		} finally {
			polling = false;
		}
	};

	pi.on("session_start", async (_event, context) => {
		ctx = context;
		firstTurn = true;
		stopPolling();
		// Not awaited: Pi connects MCP servers in the background, and the hook waits for ours.
		void hook("session-start", {}, START_TIMEOUT_MS);
		timer = setInterval(() => void poll(), POLL_MS);
		timer.unref?.();
	});

	pi.on("session_shutdown", async () => {
		stopPolling();
		ctx = undefined;
	});

	pi.on("before_agent_start", async (_event, context) => {
		ctx = context;
		const first = firstTurn;
		firstTurn = false;
		const reply = await hook("turn-start", { first }, first ? START_TIMEOUT_MS : HOOK_TIMEOUT_MS);
		if (!reply?.text) return;
		return { message: { customType: MESSAGE_TYPE, content: reply.text, display: true } };
	});

	pi.on("tool_result", async (event) => {
		if (event.parentToolCallId) return; // nested call: its result goes to a tool, not the model
		if (dbSignature(dir) === seen) return;
		const reply = await hook("tool-result", { tool: event.toolName });
		if (!reply?.text) return;
		return {
			content: [...event.content, { type: "text" as const, text: reply.text }],
			structuredContent: event.structuredContent,
		};
	});

	pi.on("agent_before_settle", async (event) => {
		// Not after Escape or an error. The context usually ends on the assistant message,
		// so `canContinue` is false here; the entry we append is what makes it valid.
		if (event.outcome !== "completed") return;
		const reply = await hook("settle");
		if (!reply?.continue || !reply.text) return;
		return {
			continue: true,
			entries: [
				...event.entries,
				{ type: "custom_message" as const, customType: MESSAGE_TYPE, content: reply.text, display: true },
			],
		};
	});

	pi.on("agent_settled", async () => {
		await hook("settled");
	});
}
