/**
 * TokenEconomizer for oh-my-pi (`omp`).
 *
 * omp has no hook protocol -- extensions are functions on its event bus -- so this extension
 * speaks the one the Claude Code and Codex integrations use, on omp's behalf: every event
 * below becomes one call of the shared dispatcher (`../scripts/te-hook --host omp`, JSON in,
 * JSON out), and the answer is applied with omp's own levers. All of the mechanism logic lives
 * in the dispatcher; this file only translates.
 *
 *   session_start       -> SessionStart       the learned manual and the tools to remove; omp
 *                                             keeps only a digest of its prompt, so the manual
 *                                             is re-sent on every prompt below
 *   session_switch      -> Stop, SessionStart close the old turn, then start the new session
 *   before_agent_start  -> UserPromptSubmit   opens a prompt-turn and re-sends the manual as a
 *                                             system-prompt block
 *   tool_result         -> PostToolUse        the guard; a redirect is delivered as a steering
 *                                             message, a still-stuck notice as a UI notice
 *   agent_end           -> Stop               closes the turn
 *   session_shutdown    -> SessionEnd         hands the session to a detached fold
 *
 * Like the hooks on the other agents it fails open: a dispatcher that is missing, slow or
 * broken answers "no change". It does nothing under TE_SIDECALL=1 (the plugin's own side
 * calls) or TE_DISABLE=1. `/te <command>` runs the `te` command line for this repository.
 */
import type { ExtensionAPI, ExtensionContext } from "@oh-my-pi/pi-coding-agent";
import { spawn } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = (() => {
  try {
    return dirname(fileURLToPath(import.meta.url));
  } catch {
    return __dirname;
  }
})();
const SCRIPTS = join(HERE, "..", "scripts");
const HOOK_TIMEOUT_MS = 60_000;
const SECTION = "tokeneconomizer";

const truthy = (v: string | undefined) => ["1", "true", "yes", "on"].includes((v ?? "").trim().toLowerCase());

/** The program and arguments that run a launcher. The launchers are POSIX `sh`, which Windows
 * cannot start; there the bootstrap they run is started with Python itself (`TE_PYTHON`, else
 * `python`). */
export function launch(
  script: string,
  args: string[],
  platform: string = process.platform,
): [string, string[]] {
  if (platform !== "win32") return [join(SCRIPTS, script), args];
  const mode = script === "te-hook" ? "hook" : "cli";
  return [process.env.TE_PYTHON || "python", [join(SCRIPTS, "te_bootstrap.py"), mode, ...args]];
}

/** Run one of the plugin's launchers with `input` on stdin; resolve to its stdout. */
function run(
  script: string,
  args: string[],
  input: string,
  mode: string,
  extra: Record<string, string> = {},
): Promise<string> {
  return new Promise((resolve) => {
    let out = "";
    let child;
    try {
      const [program, argv] = launch(script, args);
      // UTF-8 mode as the launchers set it, for where they are bypassed (Windows).
      child = spawn(program, argv, {
        env: { ...process.env, PYTHONUTF8: "1", TE_OMP_MODE: mode, ...extra },
        stdio: ["pipe", "pipe", "ignore"],
      });
    } catch {
      resolve("");
      return;
    }
    const timer = setTimeout(() => {
      child.kill("SIGKILL");
      resolve("");
    }, HOOK_TIMEOUT_MS);
    // Decoded as a stream, so a character split across two chunks arrives whole.
    child.stdout.setEncoding("utf8");
    child.stdout.addListener("data", (d: string) => (out += d));
    child.addListener("error", () => {
      clearTimeout(timer);
      resolve("");
    });
    child.addListener("close", () => {
      clearTimeout(timer);
      resolve(out);
    });
    child.stdin.addListener("error", () => {});
    child.stdin.end(input);
  });
}

/** One dispatcher call; `{}` (no change) on any failure. */
async function hook(payload: Record<string, unknown>, mode: string): Promise<any> {
  if (truthy(process.env.TE_SIDECALL) || truthy(process.env.TE_DISABLE)) return {};
  const out = await run("te-hook", ["--host", "omp"], JSON.stringify(payload), mode);
  try {
    return JSON.parse(out.trim().split("\n").pop() || "{}") ?? {};
  } catch {
    return {};
  }
}

/** A notice in the UI; on stderr in print and JSON modes, which have no UI and keep stdout for
 * the reply and the event stream. A sub session has no person to notify, so it stays silent. */
function notify(ctx: ExtensionContext, text: unknown) {
  if (ctx.agent.kind !== "main") return;
  if (typeof text !== "string" || !text) return;
  if (ctx.hasUI) ctx.ui.notify(text, "info");
  else console.error(text);
}

/** omp's switch reasons in the dispatcher's words: a new session is a cleared one, and a fork
 * carries the same facts as its source. */
const SOURCE: Record<string, string> = {
  new: "clear",
  resume: "resume",
  fork: "resume",
};

export default function tokeneconomizer(api: ExtensionAPI) {
  let manual = "";
  let prompts = 0;
  let turn = "";
  let sid = "";
  let transcript = "";

  const base = (ctx: ExtensionContext) => ({
    session_id: sid,
    transcript_path: transcript,
    cwd: ctx.cwd,
  });

  /** Announce the session to the dispatcher, keep the manual it answers, and remove the tools
   * its deny list names. The deny list is applied here and only here. */
  const start = async (ctx: ExtensionContext, source: string) => {
    sid = ctx.sessionManager.getSessionId();
    transcript = ctx.sessionManager.getSessionFile() ?? "";
    const out = await hook(
      {
        ...base(ctx),
        hook_event_name: "SessionStart",
        source,
        tool_catalog: api.getAllTools().map((t) => t.name),
      },
      ctx.mode,
    );
    manual = out?.hookSpecificOutput?.additionalContext ?? "";
    const deny = out?.omp?.denyTools;
    if (Array.isArray(deny) && deny.length) {
      const drop = new Set(deny);
      await api.setActiveTools(api.getActiveTools().filter((n) => !drop.has(n))).catch(() => {});
    }
    notify(ctx, out?.systemMessage);
  };

  api.on("session_start", async (_event, ctx) => {
    prompts = 0;
    turn = "";
    // omp reports only that a session started; an empty branch is a new session, one with
    // messages is a resumed or forked one.
    const entries = (ctx.sessionManager.getBranch?.() ?? []) as any[];
    const carried = entries.some((e) => e?.type === "message");
    await start(ctx, carried ? "resume" : "startup");
  });

  api.on("session_switch", async (event, ctx) => {
    // A switch leaves the old session mid-turn; close that turn before the new one starts.
    if (turn) {
      await hook(
        { ...base(ctx), hook_event_name: "Stop", prompt_id: turn, stop_hook_active: false },
        ctx.mode,
      );
    }
    prompts = 0;
    turn = "";
    manual = "";
    await start(ctx, SOURCE[event.reason] ?? "startup");
  });

  api.on("before_agent_start", async (event, ctx) => {
    prompts += 1;
    turn = `${sid}:${prompts}`;
    const out = await hook(
      { ...base(ctx), hook_event_name: "UserPromptSubmit", prompt_id: turn, prompt: event.prompt },
      ctx.mode,
    );
    // The repository map arrives on a session's first prompt; it joins the manual for the rest
    // of the session, since omp keeps only a digest of the prompt.
    const extra = out?.hookSpecificOutput?.additionalContext;
    if (extra) {
      manual = manual ? `${manual}\n\n${extra}` : extra;
    }
    notify(ctx, out?.systemMessage);
    if (!manual) return;
    return {
      systemPrompt: [...event.systemPrompt, `<${SECTION}>\n${manual}\n</${SECTION}>`],
    };
  });

  api.on("tool_result", async (event, ctx) => {
    const content = (event.content ?? [])
      .filter((c: any) => c?.type === "text")
      .map((c: any) => c.text)
      .join("\n");
    const out = await hook(
      {
        ...base(ctx),
        hook_event_name: "PostToolUse",
        prompt_id: turn,
        tool_name: event.toolName,
        tool_use_id: event.toolCallId,
        tool_input: event.input ?? {},
        tool_response: {
          content,
          isError: event.isError,
          // omp's bash result carries the command's real exit code here.
          exit_code: (event as any).details?.exitCode,
        },
      },
      ctx.mode,
    );
    const nudge = out?.hookSpecificOutput?.additionalContext;
    if (nudge) {
      api.sendMessage({ customType: SECTION, content: nudge, display: true }, { deliverAs: "steer" });
    }
    notify(ctx, out?.systemMessage);
  });

  api.on("agent_end", async (_event, ctx) => {
    await hook(
      { ...base(ctx), hook_event_name: "Stop", prompt_id: turn, stop_hook_active: false },
      ctx.mode,
    );
    turn = "";
  });

  api.on("session_shutdown", async (event, ctx) => {
    await hook({ ...base(ctx), hook_event_name: "SessionEnd", reason: (event as any).reason }, ctx.mode);
  });

  api.registerCommand("te", {
    description: "TokenEconomizer: status | microskills | config | repos (what it learned here, what it did)",
    handler: async (args, ctx) => {
      // `te` splits the line itself (shlex), so a quoted fact id with spaces stays one argument.
      const argv = ["--argline", (args || "").trim() || "status"];
      // The session id lets `te status` say when this session is recorded under the repository
      // it started in rather than the one `ctx.cwd` is in now.
      const out = await run("te", ["--host", "omp", "--cwd", ctx.cwd, ...argv], "", ctx.mode, {
        TE_SESSION_ID: ctx.sessionManager.getSessionId(),
      });
      notify(ctx, out.trim() || "te: no output (is Python 3.10+ on PATH?)");
    },
  });
}
