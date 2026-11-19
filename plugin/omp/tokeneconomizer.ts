/**
 * TokenEconomizer for oh-my-pi (`omp`).
 *
 * omp has no hook protocol -- extensions are functions on its event bus -- so this extension
 * speaks the dispatcher's hook protocol on omp's behalf: every event
 * below becomes one call of the shared dispatcher (`../scripts/te-hook.cmd --host omp`, JSON in,
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
 * The extension fails open like any hook: a dispatcher that is missing, slow or
 * broken answers "no change". It does nothing under TE_SIDECALL=1 (the plugin's own side
 * calls) or TE_DISABLE=1. `/te <command>` runs the `te` command line for this repository and
 * shows its output in a user-only overlay panel; `/te config` shows links to the two config files
 * instead of that output.
 */
import type { ExtensionAPI, ExtensionContext } from "@oh-my-pi/pi-coding-agent";
import { getMarkdownTheme, Markdown } from "@oh-my-pi/pi-coding-agent";
import { ScrollView } from "@oh-my-pi/pi-tui";
import { spawn } from "node:child_process";
import { existsSync, readFileSync, realpathSync, statSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, isAbsolute, join, normalize, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

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
// The user-facing subcommands, in the order `te help` prints them. Bare `/te` shows that list
// as the overlay's index (the CLI's `_HELP`, which owns the one-line descriptions); the picker
// offers only the names, so no description is repeated here.
const SUBCOMMANDS = [
  "status",
  "microskills",
  "toolprune",
  "config",
  "repos",
  "history",
  "repomap",
  "doctor",
];

export const truthy = (v: string | undefined) => ["1", "true", "yes", "on"].includes((v ?? "").trim().toLowerCase());

/** The program and arguments that run a launcher. On Windows the `.cmd` launchers run the
 * bootstrap with Python itself (`TE_PYTHON`, else `python`); elsewhere the script runs
 * directly. */
export function launch(
  script: string,
  args: string[],
  platform: string = process.platform,
): [string, string[]] {
  if (platform !== "win32") return [join(SCRIPTS, script), args];
  const mode = script === "te-hook" ? "hook" : "cli";
  return [process.env.TE_PYTHON || "python", [join(SCRIPTS, "te_bootstrap.py"), mode, ...args]];
}

/** The launcher call the handler goes through; tests replace `run` to stub `te` calls. */
export const runner = {
  /**
   * Run one of the plugin's launchers with `input` on stdin; resolve to its stdout.
   * Never rejects: a missing or slow launcher answers empty, and the caller fails open.
   */
  run(
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
  },
};

/** One dispatcher call; `{}` (no change) on any failure. */
async function hook(payload: Record<string, unknown>, mode: string): Promise<any> {
  if (truthy(process.env.TE_SIDECALL) || truthy(process.env.TE_DISABLE)) return {};
  const out = await runner.run("te-hook", ["--host", "omp"], JSON.stringify(payload), mode);
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

/** The state root the CLI uses: `$TOKENECONOMIZER_HOME`, else `~/.tokeneconomizer`. */
function stateRoot(): string {
  return resolve(process.env.TOKENECONOMIZER_HOME || join(homedir(), ".tokeneconomizer"));
}

function isDir(path: string): boolean {
  try {
    return statSync(path).isDirectory();
  } catch {
    return false;
  }
}

/** The git directory a `.git` file points at (`gitdir: <path>`), or null. */
function gitdirOfFile(dot: string): string | null {
  try {
    const line = readFileSync(dot, "utf8").slice(0, 4096).trim();
    if (!line.startsWith("gitdir:")) return null;
    const gitdir = line.slice(line.indexOf(":") + 1).trim();
    return isAbsolute(gitdir) ? gitdir : normalize(join(dirname(dot), gitdir));
  } catch {
    return null;
  }
}

/** Whether the `.git` entry makes its directory a checkout, as git itself takes it: a directory
 * that holds `HEAD`, or a file whose `gitdir:` target exists. */
function isGitEntry(dot: string): boolean {
  if (isDir(dot)) return existsSync(join(dot, "HEAD"));
  const gitdir = gitdirOfFile(dot);
  return !!gitdir && isDir(gitdir);
}

function homeDir(): string {
  const home = process.env.HOME || homedir();
  if (!home || home === "~") return "";
  try {
    return realpathSync(home);
  } catch {
    return "";
  }
}

/** The CLI's stdout as one fenced block, so no line of it is re-read as Markdown. The fence is
 * longer than any backtick run in the output, which a learned fact can contain. */
function resultPanel(out: string): string {
  const text = out.trimEnd() || "te: no output (is Python 3.10+ on PATH?)";
  const longest = (text.match(/`+/g) ?? []).reduce((n, run) => Math.max(n, run.length), 0);
  const fence = "`".repeat(Math.max(3, longest + 1));
  return `${fence}text\n${text}\n${fence}`;
}

/** Bare `/te`: one bullet per subcommand, from `te help`, then the state directory that holds
 * this session's files. The key comes from `te repo-key`; when that call fails the state
 * line is omitted, never guessed. */
function indexPanel(help: string, key: string): string {
  const lines = help
    .split(/\r?\n/)
    .map((line) => line.trimEnd())
    .filter(Boolean);
  const bullets = lines.length
    ? lines.map((line) => `- ${line}`).join("\n")
    : "- te help printed nothing (is Python 3.10+ on PATH?)";
  const id = key.trim();
  return id ? `${bullets}\n\nstate \`${join(stateRoot(), "repos", id, "omp")}\`` : bullets;
}

/** The `config.json` files `/te config` points at, each as an explicit Markdown link (which
 * the renderer turns into a terminal hyperlink) plus the same full path as plain text, since a
 * bare path is not linked. A `config set` writes the repository's file, or the global one with
 * `--global`; the repository comes from `--cwd`, not from a session pin. The repository key
 * comes from `te repo-key`; when that call fails the repository entry is omitted. */
function configPanel(key: string, global: boolean): string {
  const root = stateRoot();
  const entry = (label: string, path: string) => {
    const note = existsSync(path) ? "" : " (not created yet — config set writes it)";
    return `[\`config.json (${label})\`](${pathToFileURL(path).href})\n\n\`${path}\`${note}`;
  };
  const target = global
    ? "`config set` writes the global `config.json` (`--global`)."
    : "`config set` writes this repository's `config.json`; add `--global` to write the global one.";
  const id = key.trim();
  const repo = id ? `${entry("this repo", join(root, "repos", id, "config.json"))}\n\n` : "";
  return `${repo}${entry("global", join(root, "config.json"))}\n\n${target}`;
}

/** Render `markdown` in a user-only overlay. The panel is never a session entry, so it costs no
 * context tokens. It is as tall as its content up to the terminal, scrolls when longer, and any
 * key closes it. */
function showPanel(ctx: ExtensionContext, markdown: string) {
  if (!ctx.hasUI) return;
  void ctx.ui
    .custom(
      (tui, _theme, _keybindings, done) => {
        const body = new Markdown(markdown, 1, 1, getMarkdownTheme());
        let view: ScrollView | undefined;
        return {
          render: (width: number) => {
            if (!view) {
              const rows = body.render(width).length;
              const height = Math.min(Math.max(5, rows), Math.max(5, tui.terminal.rows - 4));
              view = new ScrollView(body, { height, scrollbar: "auto" });
            }
            return view.render(width);
          },
          invalidate: () => view?.invalidate(),
          handleInput: (data: string) => {
            if (view?.handleScrollKey(data)) tui.requestRender();
            else done(undefined);
          },
          dispose: () => view?.dispose(),
        };
      },
      { overlay: true },
    )
    .catch(() => {});
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
    description: "TokenEconomizer: help, status, and repo learning (what it learned here, what it did)",
    // `te <prefix>` opens the subcommand list, as omp's own commands do; past the first word
    // there is nothing to offer.
    getArgumentCompletions: (prefix) => {
      if (prefix.includes(" ")) return null;
      const p = prefix.toLowerCase();
      const matches = SUBCOMMANDS.filter((name) => name.startsWith(p)).map((name) => ({
        value: `${name} `,
        label: name,
      }));
      return matches.length ? matches : null;
    },
    handler: async (args, ctx) => {
      // `te` splits the line itself (shlex), so a quoted fact id with spaces stays one argument.
      // Bare `/te` asks for the CLI's one-line subcommand list, which the panel turns into the
      // index; a subcommand's output is shown as it printed, except `config` and `config show`,
      // which show the two config files. `config set` and `config unset` printed a confirmation
      // that names the file written, so that output is shown instead.
      const argline = (args || "").trim();
      const sessionId = ctx.sessionManager.getSessionId();
      // The session id lets `te status` say when this session is recorded under the repository
      // it started in rather than the one `ctx.cwd` is in now. The repository key belongs to
      // the dispatcher, so it is fetched beside the main call; a failed key call leaves an
      // empty key, and the panels omit the state paths instead of guessing them.
      const [out, key] = await Promise.all([
        runner.run(
          "te",
          ["--host", "omp", "--cwd", ctx.cwd, "--argline", argline || "help"],
          "",
          ctx.mode,
          { TE_SESSION_ID: sessionId },
        ),
        runner.run("te", ["--host", "omp", "--cwd", ctx.cwd, "repo-key"], "", ctx.mode),
      ]);
      let markdown: string;
      if (!argline) markdown = indexPanel(out, key);
      else {
        const words = argline.split(/\s+/).filter((t) => !/^--global(?:ly)?$/.test(t));
        if (words[0] === "config" && (words.length === 1 || words[1] === "show")) {
          const global = /(?:^|\s)--global(?:ly)?(?:\s|$)/.test(argline);
          markdown = configPanel(key, global);
        } else markdown = resultPanel(out);
      }
      showPanel(ctx, markdown);
    },
  });
}
