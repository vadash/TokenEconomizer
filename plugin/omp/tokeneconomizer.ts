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
 * calls) or TE_DISABLE=1. `/te <command>` runs the `te` command line for this repository and
 * shows its output in a user-only overlay panel.
 */
import type { ExtensionAPI, ExtensionContext } from "@oh-my-pi/pi-coding-agent";
import { getMarkdownTheme, Markdown } from "@oh-my-pi/pi-coding-agent";
import { ScrollView } from "@oh-my-pi/pi-tui";
import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync, readFileSync, realpathSync, statSync } from "node:fs";
import { homedir } from "node:os";
import { basename, delimiter, dirname, isAbsolute, join, normalize, resolve } from "node:path";
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

/** The nearest directory at or above `start` that is a git checkout. Honours
 * `GIT_CEILING_DIRECTORIES`, and treats the home directory as one more ceiling, as the CLI does. */
function findRepoRoot(start: string): string | null {
  const ceilings = new Set(
    (process.env.GIT_CEILING_DIRECTORIES || "")
      .split(delimiter)
      .filter(Boolean)
      .map((path) => resolve(path)),
  );
  const home = homeDir();
  let cur = resolve(start || process.cwd());
  for (;;) {
    if (isGitEntry(join(cur, ".git"))) return cur;
    const parent = dirname(cur);
    if (parent === cur || ceilings.has(parent)) return null;
    if (home) {
      try {
        if (realpathSync(parent) === home) return null;
      } catch {
        // An unresolvable parent is not the home ceiling; keep climbing as git would.
      }
    }
    cur = parent;
  }
}

/** The git directory that holds `config`, following worktree indirection. */
function gitCommonDir(root: string): string | null {
  const dot = join(root, ".git");
  if (isDir(dot)) return dot;
  const gitdir = gitdirOfFile(dot);
  if (!gitdir) return null;
  try {
    return normalize(join(gitdir, readFileSync(join(gitdir, "commondir"), "utf8").trim()));
  } catch {
    return gitdir;
  }
}

function originUrl(root: string): string {
  const gitdir = gitCommonDir(root);
  if (!gitdir) return "";
  let config: string;
  try {
    config = readFileSync(join(gitdir, "config"), "utf8");
  } catch {
    return "";
  }
  const remotes = new Map<string, string>();
  let current = "";
  for (const line of config.split(/\r?\n/)) {
    const header = /^\s*\[remote\s+"([^"]+)"\]\s*$/.exec(line);
    if (header) {
      current = header[1];
      continue;
    }
    if (line.trimStart().startsWith("[")) {
      current = "";
      continue;
    }
    const url = /^\s*url\s*=\s*(\S+)\s*$/.exec(line);
    if (current && url && !remotes.has(current)) remotes.set(current, url[1]);
  }
  return remotes.get("origin") || remotes.values().next().value || "";
}

/** `git@github.com:Owner/Repo.git` and `https://github.com/owner/repo` become one spelling. */
function normalizeRemote(url: string): string {
  let u = (url || "").trim();
  const scheme = /^[a-z+]+:\/\//.exec(u);
  if (scheme) u = u.slice(scheme[0].length);
  u = u.replace(/^[^@/]+@/, "");
  u = u.replace(/\\/g, "/").replace(/\/+/g, "/");
  const scp = scheme ? /^[^/]+:[^/\d]/ : /^[^/]+:[^/]/;
  if (!/^[a-zA-Z]:\//.test(u) && scp.test(u)) u = u.replace(":", "/");
  return u.replace(/\/+$/, "").replace(/\.git$/, "").toLowerCase();
}

function slug(text: string): string {
  const s = (text || "repo")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 40);
  return s || "repo";
}

/** The directory holding a checkout's shared `.git`, following a linked worktree back to its
 * clone; null for a git directory that is not a `.git` (a bare repository, `--separate-git-dir`). */
function mainCheckout(root: string): string | null {
  const common = gitCommonDir(root);
  return common && basename(normalize(common)) === ".git" ? dirname(normalize(common)) : null;
}

/** The repository's state key, by the CLI's rule: the normalized `origin` remote, else the real
 * path of its main checkout, else the directory itself; the readable part is the repository name.
 * `$TOKENECONOMIZER_REPO` stands in for a remote where git names none. */
function repoKey(cwd: string): string {
  const found = findRepoRoot(cwd);
  const root = found ?? resolve(cwd || process.cwd());
  let remote = found ? normalizeRemote(originUrl(root)) : "";
  if (!remote) remote = normalizeRemote(process.env.TOKENECONOMIZER_REPO || "");
  if (remote) {
    const name = remote.replace(/\/+$/, "").split("/").pop() || "repo";
    return `${slug(name)}-${createHash("sha1").update(remote, "utf8").digest("hex").slice(0, 10)}`;
  }
  const main = found ? (mainCheckout(root) ?? gitCommonDir(root) ?? root) : root;
  let canon: string;
  try {
    canon = realpathSync(main);
  } catch {
    canon = resolve(main);
  }
  const name = basename(main.replace(/\/+$/, "")) || "repo";
  return `${slug(name)}-${createHash("sha1").update(canon, "utf8").digest("hex").slice(0, 10)}`;
}

/** The `config.json` a `config` command writes: the repository's when it exists, else the
 * global one in effect. A `config set` writes the repository's file, or the global one with
 * `--global`; the command resolves the repository from `--cwd`, not from a session pin. */
function configTarget(cwd: string, global: boolean): { path: string; exists: boolean; repoExists: boolean } {
  const root = stateRoot();
  const repo = join(root, "repos", repoKey(cwd), "config.json");
  const repoExists = existsSync(repo);
  if (!global && repoExists) return { path: repo, exists: true, repoExists };
  const path = join(root, "config.json");
  return { path, exists: existsSync(path), repoExists };
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
 * this session's files. */
function indexPanel(help: string, cwd: string): string {
  const lines = help
    .split(/\r?\n/)
    .map((line) => line.trimEnd())
    .filter(Boolean);
  const bullets = lines.length
    ? lines.map((line) => `- ${line}`).join("\n")
    : "- te help printed nothing (is Python 3.10+ on PATH?)";
  return `${bullets}\n\nstate \`${join(stateRoot(), "repos", repoKey(cwd), "omp")}\``;
}

/** The config file as an explicit Markdown link (which the renderer turns into a terminal
 * hyperlink) plus the same full path as plain text, since a bare path is not linked. */
function configLink(cwd: string, global: boolean): string {
  const target = configTarget(cwd, global);
  const note = target.exists && target.repoExists ? "" : " (not created yet — config set writes it)";
  return `[\`config.json\`](${pathToFileURL(target.path).href})\n\n\`${target.path}\`${note}`;
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
      // index; a subcommand's output is shown as it printed.
      const argline = (args || "").trim();
      const sessionId = ctx.sessionManager.getSessionId();
      // The session id lets `te status` say when this session is recorded under the repository
      // it started in rather than the one `ctx.cwd` is in now.
      const out = await run(
        "te",
        ["--host", "omp", "--cwd", ctx.cwd, "--argline", argline || "help"],
        "",
        ctx.mode,
        { TE_SESSION_ID: sessionId },
      );
      let markdown = argline ? resultPanel(out) : indexPanel(out, ctx.cwd);
      if (argline.split(/\s+/)[0] === "config") {
        const global = /(?:^|\s)--global(?:ly)?(?:\s|$)/.test(argline);
        markdown += `\n\n${configLink(ctx.cwd, global)}`;
      }
      showPanel(ctx, markdown);
    },
  });
}
