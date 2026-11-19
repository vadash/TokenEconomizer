"""The omp host adapter, and the contract its hook calls share.

omp has no hook protocol of its own: the TypeScript extension in `plugin/omp/` speaks the
dispatcher's on its behalf. Each event becomes one JSON payload on stdin (`session_id`,
`transcript_path`, `cwd`, `hook_event_name`, `prompt_id`, and `tool_name` / `tool_input` /
`tool_response` per tool call), and the answer comes back as one JSON object on stdout
(`hookSpecificOutput.additionalContext`, `systemMessage`, and this host's own `omp.denyTools`).

The differences that matter live on the adapter rather than in a branch of a mechanism:

  * Turn identity. omp does not stamp a turn, so the extension numbers each prompt and sends it
    as `prompt_id`.
  * Stopping. The plugin never stops a turn: the guard's last rung is a notice telling the
    person how to. omp's terminal works as Pi's does: Esc aborts the turn and its command;
    Ctrl+C ends `omp -p`, but a command it started runs on until it finishes.
  * Tool results. `tool_response` is `{content, isError, exit_code}`: the extension carries the
    bash result's real exit code, which the guard reads.
  * The session log. omp's session file is a tree of JSONL entries; `trace.omp_write_session`
    writes its active branch as the event transcript the mechanisms read.
"""
import json
import os
import time

from .. import trace

_RESULT_KEYS = ("stdout", "stderr", "output", "aggregated_output", "formatted_output",
                "content", "result", "error", "message", "text_result_for_llm")
RESULT_CAP = 20000      # characters of a tool result the guard folds (errors live early)


class FoldLater(Exception):
    """A finished session's log cannot be had now but may be later: it stays unfolded."""


def response_text(resp, cap=RESULT_CAP):
    """Best-effort text of a hook's `tool_response`, whatever shape the tool returned."""
    if resp is None:
        return ""
    if isinstance(resp, str):
        return resp[:cap]
    if isinstance(resp, list):
        return "\n".join(x.get("text", "") if isinstance(x, dict) else str(x)
                         for x in resp)[:cap]
    if isinstance(resp, dict):
        if "structuredPatch" in resp:
            return ""
        parts = []
        for k in _RESULT_KEYS:
            v = resp.get(k)
            if isinstance(v, str) and v:
                parts.append(v)
            elif isinstance(v, list):
                parts.append("\n".join(x.get("text", "") for x in v if isinstance(x, dict)))
        return "\n".join(parts)[:cap]
    return str(resp)[:cap]


def merge_outputs(*outputs):
    """Combine hook outputs: contexts and notices concatenate, the last stop decision wins."""
    out = {}
    for o in outputs:
        if not o:
            continue
        for k, v in o.items():
            if k == "systemMessage" and out.get(k):
                out[k] = out[k] + "\n" + v
            elif k in ("additionalContext", "additional_context") and out.get(k):
                out[k] = out[k] + "\n\n" + v
            elif k == "hookSpecificOutput" and out.get(k):
                a = out[k].get("additionalContext") or ""
                b = v.get("additionalContext") or ""
                out[k] = dict(out[k], **v)
                if a and b:
                    out[k]["additionalContext"] = a + "\n\n" + b
            else:
                out[k] = v
    return out


def _exit_code(resp):
    if isinstance(resp, dict):
        for k in ("exit_code", "exitCode", "returncode", "status_code"):
            v = resp.get(k)
            if isinstance(v, int) and not isinstance(v, bool):
                return v
        meta = resp.get("metadata")
        if isinstance(meta, dict) and isinstance(meta.get("exit_code"), int):
            return meta["exit_code"]
    return None


class Host:
    name = "?"

    def session_id(self, p):
        return str(p.get("session_id") or "")

    def transcript(self, p):
        return p.get("transcript_path") or ""

    def cwd(self, p, env):
        return p.get("cwd") or os.getcwd()

    def env_session_id(self, env):
        """The session a `te` command runs inside, from its shell's environment ("" if none is
        named). The omp extension's `/te` sets `TE_SESSION_ID`."""
        return str(env.get("TE_SESSION_ID") or "")

    def turn_key(self, p):
        return ""

    def is_subagent(self, p):
        return bool(p.get("agent_id"))

    def context(self, event, text):
        return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}

    def notice(self, text):
        return {"systemMessage": text}

    def stop_hint(self, headless):
        return self.end_run if headless else self.interrupt

    def stopped_normally(self, p):
        return True

    def completed_at_end(self, p):
        """Whether a turn still open at SessionEnd finished normally (its Stop never came)."""
        return False

    # Whether the loop guard's still-stuck notice must also go through the model, because the
    # agent does not put hook messages where the person looks.
    relays_notices = False

    # Whether a resumed conversation still carries the context its first SessionStart added, so
    # the plugin need not add it again. omp keeps only a digest of its system prompt.
    keeps_start_context = False

    def relay(self, text):
        """What the model is asked, so that it passes `text` on to the person, word for word.
        The notice is the person's to act on: it does not change the model's task."""
        return ("TokenEconomizer, a plugin the user runs, has a notice for them, and their main "
                "view does not show plugin messages. Put this line, word for word, in your next "
                f"message to the user, and again in your final reply:\n\n{text}\n\nIt is for "
                "the user to act on; it does not change your task.")

    def headless(self, env, transcript=None):
        return False

    def tool_call(self, p):
        """(tool_name, args, result_text, exit_code_or_None) for a PostToolUse payload."""
        args = p.get("tool_input") if isinstance(p.get("tool_input"), dict) else {}
        resp = p.get("tool_response")
        return str(p.get("tool_name") or ""), args, response_text(resp), _exit_code(resp)

    # -- the finished session, in the shapes the mechanisms read --------------------------
    def mechanism_trace(self, transcript_path, workdir):
        """Path of a trace `trace.load_trajectory` reads, for the finished session."""
        return transcript_path if transcript_path and os.path.exists(transcript_path) else None

    def session_usage(self, transcript_path):
        raise NotImplementedError

    def prompt_count(self, transcript_path):
        """How many prompts the finished session ran; None where the log does not say."""
        return None

    def settle(self, transcript_path):
        """Wait, if this host needs to, until the finished session's log is complete."""

    def complete(self, transcript_path, session_id, cwd=None, session=None):
        """The log to fold the finished session from."""
        return transcript_path

    def last_active(self, transcript_path, session):
        """When a session that never said it ended last did anything (`session`: its state)."""
        return os.path.getmtime(transcript_path)

    def session_state(self, env):
        """What a session's state keeps from the environment of its first hook."""
        return {}

    def tool_usage(self, trace_path):
        return trace.tool_usage(trace_path)

    # Whether the agent keeps its sessions' logs where the plugin can read the ones from before
    # it was installed (`history`). omp's state records the sessions the plugin saw only.
    keeps_history = False

    def history(self, env, extra_dirs=()):
        return []


class Omp(Host):
    """oh-my-pi (`omp`), through the TypeScript extension in `plugin/omp/`.

    The extension speaks the dispatcher's contract on omp's behalf and applies the answer with
    omp's own levers (a system-prompt block, a steer message, the session's tool deny list).
    What it sends:

      * `prompt_id`, which it numbers itself, for the turn identity omp does not stamp;
      * `tool_response = {content, isError, exit_code}`, its bash result's real exit code;
      * its mode in `TE_OMP_MODE` on the hook's environment: print / json / rpc are headless.

    A resumed conversation does not carry what its first start added. Its subagents are full
    sessions of their own, not sidechains marked in the parent, so no session is marked a
    subagent and the loop guard covers every one at every depth.
    """
    name = "omp"
    prompt_context = True
    # omp's terminal works as Pi's does: Esc aborts the turn and its command; Ctrl+C ends
    # `omp -p`, but a command it started runs on (detached) until it finishes.
    interrupt = "Press Esc to abort it."
    end_run = "Press Ctrl+C to end the run; a command it started runs on until it finishes."

    def turn_key(self, p):
        return str(p.get("prompt_id") or "")

    def prepare(self, payload, env):
        """Keep the tool catalog this SessionStart reports, merged with what earlier sessions
        reported: a fold needs every tool a session could call, not only the ones it used."""
        p = dict(payload or {})
        catalog = p.get("tool_catalog")
        if not isinstance(catalog, list):
            return p
        try:
            from . import adapter as live_adapter
            from . import denylist
            from .paths import state_root
            path = live_adapter.catalog_path(state_root(env), "omp")
            try:
                with open(path, encoding="utf-8") as fh:
                    old = json.load(fh)
                known = [t for t in old.get("tools") or [] if isinstance(t, str)]
            except (OSError, ValueError, AttributeError):
                known = []
            tools = sorted(set(known) | {t for t in catalog if isinstance(t, str)})
            denylist.write_json(path, {"tools": tools, "at": time.time()})
        except Exception:              # a hook fails open: a catalog is an optimization
            pass
        return p

    def render(self, payload, out, ctx):
        """The extension applies the deny list at session start, and only there: the answer
        names the tools to remove, from the plugin's state, never from an omp settings file."""
        from . import denylist
        if (payload or {}).get("hook_event_name") == "SessionStart" and not ctx.config.shadow:
            out["omp"] = {"denyTools": sorted(
                denylist.applied_for(ctx.store, ctx.repo.root, "omp"))}
        return out

    def is_subagent(self, p):
        return False

    def headless(self, env, transcript=None):
        return str(env.get("TE_OMP_MODE", "")).strip() in ("print", "json", "rpc")

    def mechanism_trace(self, transcript_path, workdir):
        out = os.path.join(workdir, "omp_trace.jsonl")
        n = trace.omp_write_session(transcript_path, out)
        return out if n is not None else None

    def prompt_count(self, transcript_path):
        return trace.omp_prompt_count(transcript_path)

    def session_usage(self, transcript_path):
        return trace.omp_session_usage(transcript_path)


HOSTS = {"omp": Omp}


def default_host(env=None, recent=None):
    """omp is the only host. `TE_HOST` is still read, so an existing shell keeps working."""
    env = os.environ if env is None else env
    return env["TE_HOST"] if env.get("TE_HOST") in HOSTS else "omp"


def detect(payload, env=None, hint=None):
    """The adapter for this hook call. The extension passes `--host omp` explicitly; a payload
    or environment is no longer enough to pick another agent."""
    return HOSTS[hint]() if hint in HOSTS else Omp()
