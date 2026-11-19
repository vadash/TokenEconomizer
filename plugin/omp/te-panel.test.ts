// Drives the real /te command handler with a stubbed omp UI and asserts the panel
// markdown. Catches branch-suppression bugs no Python test can reach (the handler
// decides between the links panel and the CLI stdout dump).
// Run: bun test plugin/omp/  (or npm run test:omp)
import { beforeAll, describe, expect, test } from "bun:test";
import { plugin } from "bun";
import { mkdirSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const seen: string[] = [];
let handler: (args: string, ctx: unknown) => Promise<void> = async () => {};

// What the stubbed `te repo-key` call prints (with its trailing newline), or null to
// fail that call. Every other launcher call runs for real. Tests take the key as a
// given literal and never compute the key rule.
let repoKeyStub: string | null = "panelrepo-0123456789\n";
let stubRunner: {
  run: (
    script: string,
    args: string[],
    input: string,
    mode: string,
    extra?: Record<string, string>,
  ) => Promise<string>;
} | null = null;

async function panelFor(argline: string, work: string): Promise<string> {
  seen.length = 0;
  const factories: Array<
    (t: unknown, th: unknown, k: unknown, d: (v: unknown) => void) => unknown
  > = [];
  const ctx = {
    cwd: work,
    hasUI: true,
    mode: "default",
    sessionManager: { getSessionId: () => "panel-test", getSessionFile: () => "" },
    ui: {
      custom: (
        f: (t: unknown, th: unknown, k: unknown, d: (v: unknown) => void) => unknown,
      ) => {
        factories.push(f);
        return Promise.resolve();
      },
    },
  };
  await handler(argline, ctx);
  const factory = factories.pop();
  if (factory) factory({ terminal: { rows: 40 }, requestRender() {} }, {}, {}, () => {});
  return seen.join("\n");
}

let work = "";

beforeAll(async () => {
  // Stubs must register before the extension module loads, hence the dynamic import:
  // a static import would resolve the bare specifiers before plugin() runs.
  plugin({
    name: "omp-stubs",
    setup(build) {
      build.module("@oh-my-pi/pi-coding-agent", () => ({
        loader: "object",
        exports: {
          getMarkdownTheme: () => ({}),
          Markdown: class {
            constructor(text: string, ..._rest: unknown[]) {
              seen.push(text);
              void _rest;
            }
            render(_width: number) {
              return [seen[seen.length - 1] ?? ""];
            }
          },
        },
      }));
      build.module("@oh-my-pi/pi-tui", () => ({
        loader: "object",
        exports: {
          ScrollView: class {
            constructor(..._args: unknown[]) {
              void _args;
            }
            render(_width: number) {
              return [];
            }
            handleScrollKey(_data: string) {
              return false;
            }
            invalidate() {}
            dispose() {}
          },
        },
      }));
    },
  });
  const mod = (await import("./tokeneconomizer.ts")) as {
    default: (api: unknown) => void;
    runner: NonNullable<typeof stubRunner>;
  };
  const ext = mod.default as (api: unknown) => void;
  // The handler runs launchers through this seam; repo-key answers come from the stub
  // above while every other call runs for real.
  stubRunner = mod.runner;
  const realRun = stubRunner.run.bind(stubRunner);
  stubRunner.run = async (script, args, input, mode, extra) => {
    if (script === "te" && args.includes("repo-key")) {
      if (repoKeyStub === null) return "";
      return repoKeyStub;
    }
    return realRun(script, args, input, mode, extra);
  };
  const commands: Record<string, { handler: (a: string, c: unknown) => Promise<void> }> = {};
  ext({
    on() {},
    registerCommand(
      name: string,
      def: { handler: (a: string, c: unknown) => Promise<void> },
    ) {
      commands[name] = def;
    },
    getAllTools: () => [],
    getActiveTools: () => [],
    setActiveTools: async () => {},
  });
  handler = commands.te.handler;
  const base = mkdtempSync(join(tmpdir(), "te-panel-"));
  const home = join(base, "home");
  work = join(base, "work");
  mkdirSync(work, { recursive: true });
  process.env.TOKENECONOMIZER_HOME = home;
});

describe("/te panel", () => {
  test("bare config shows the two links and no JSON dump", async () => {
    const panel = await panelFor("config", work);
    expect(panel).toContain("config.json (this repo)");
    expect(panel).toContain("config.json (global)");
    expect(panel).not.toContain("```text");
  });

  test("config --global still shows the two links", async () => {
    const panel = await panelFor("config --global", work);
    expect(panel).toContain("config.json (global)");
    expect(panel).not.toContain("```text");
  });

  test("config set shows the fenced CLI confirmation", async () => {
    const panel = await panelFor("config set microskills=on", work);
    expect(panel).toContain("config.json: {");
    expect(panel).toContain("```text");
    expect(panel).not.toContain("config.json (this repo)");
  });

  test("config unset shows CLI output", async () => {
    const panel = await panelFor("config unset microskills", work);
    expect(panel).toContain("config.json: {}");
  });

  test("bare /te shows the state path with the stubbed repo key", async () => {
    repoKeyStub = "panelrepo-0123456789\n";
    const panel = await panelFor("", work);
    expect(panel).toContain(join("repos", "panelrepo-0123456789", "omp"));
  });

  test("bare /te omits the state path when repo-key fails", async () => {
    repoKeyStub = null;
    const panel = await panelFor("", work);
    expect(panel).not.toContain("state `");
    expect(panel).toContain("- ");
  });

  test("config omits the repo entry but keeps the global one when repo-key fails", async () => {
    repoKeyStub = "  \n";
    const panel = await panelFor("config", work);
    expect(panel).not.toContain("config.json (this repo)");
    expect(panel).toContain("config.json (global)");
  });

  test("config shows the stubbed repo key in the repo entry", async () => {
    repoKeyStub = "panelrepo-0123456789\n";
    const panel = await panelFor("config", work);
    expect(panel).toContain(join("repos", "panelrepo-0123456789", "config.json"));
  });
});