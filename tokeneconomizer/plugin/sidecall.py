"""Side calls -- the proposer, the stuck judge, the nudge writer -- through the omp CLI.

The mechanisms call an optimizer as `runner(prompt, model) -> (text, effective_tokens)`
(`runner.AgentRunner.optimizer_call`). In the harness that call went to the agent's model through
the harness's own routing. Here it goes through the omp CLI the user is already signed in to, so
no extra API key is needed and the call is billed where the session is:

  * omp: `omp --print --mode json` with nothing loaded but the model -- no tools, extensions,
    skills, context files, MCP servers or saved session. With no `paid_model` it runs on the
    person's own default provider and model, under whatever login omp holds for it (a
    subscription signed in with `/login`, or an API key). `omp_cli` names the `omp` to run,
    where the first on PATH is a wrapper the calls should not go through.

Every side call runs with `TE_SIDECALL=1`, which the plugin's hook entry point treats as "do
nothing": without it the nested agent would run this plugin's hooks and recurse. A call that
returns no reply -- a CLI that is not signed in, say -- leaves one line in the plugin's
errors.log, which `te doctor` shows. One that answered it is not signed in also pauses omp's
side calls (`paused`): every later call would fail the same way. Nothing here runs unless
micro-skills is on (the proposer) or the loop guard is (the judge and the nudge writer).
"""
import json
import os
import re
import shutil
import subprocess
import tempfile

from .. import trace
from . import accounting

TIMEOUT_S = 180


def _env():
    return dict(os.environ, TE_SIDECALL="1")


# What a CLI says when it is not signed in.
_SIGNED_OUT_RE = re.compile(r"To authenticate|/login\b|\b[Nn]ot (?:logged|signed) in")


def _log(text):
    """One entry in the plugin's errors.log, which `te doctor` shows."""
    import time
    from .paths import state_root
    try:
        root = state_root()
        os.makedirs(root, exist_ok=True)
        with open(os.path.join(root, "errors.log"), "a", encoding="utf-8") as fh:
            fh.write(f"--- {time.strftime('%Y-%m-%dT%H:%M:%S')} {text}")
    except OSError:
        pass


def _noted(host, call):
    """`call`, with a side call that raises noted in errors.log before it propagates. Both
    callers catch it -- the judge records a short `runner-error`, the proposer skips its tick --
    so without this note a CLI that keeps failing would leave no trace of why. One line, and
    never the command: most hosts take the prompt on argv, a `TimeoutExpired` names its whole
    command, and `te doctor` prints this file's tail."""
    def run(prompt, model="agent"):
        try:
            return call(prompt, model)
        except subprocess.TimeoutExpired as e:
            _log(f"sidecall {host}: no reply (timed out after {e.timeout:g}s)\n")
            raise
        except subprocess.CalledProcessError as e:
            _log(f"sidecall {host}: no reply (exit {e.returncode})\n")
            raise
        except Exception as e:
            what = " ".join(str(e).split())[:200]
            _log(f"sidecall {host}: failed ({type(e).__name__}: {what})\n")
            raise
    run.__wrapped__ = call
    return run


def _no_reply(host, out):
    """Note a side call that came back empty, which nothing else would show. A CLI that said it
    is not signed in pauses the host's side calls."""
    import time
    from .paths import state_root
    stderr = getattr(out, "stderr", "") or ""
    tail = " ".join((stderr + " " + (getattr(out, "stdout", "") or "")).split())[-300:]
    _log(f"sidecall {host}: no reply (exit {getattr(out, 'returncode', '?')}) {tail}\n")
    try:
        root = state_root()
        if _SIGNED_OUT_RE.search(stderr + (getattr(out, "stdout", "") or "")):
            path = _pause_path(host, root)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"at": time.time(), "reason": tail[-200:]}, fh)
    except OSError:
        pass


def _pause_path(host, root=None):
    from .paths import state_root
    return os.path.join(root or state_root(), "hosts", host, "sidecall.json")


def paused(host, root=None):
    """The pause on this host's side calls ({"at", "reason"}) since its CLI said it is not
    signed in, or None. `te config set` of `microskills` or `loopguard` lifts it (`resume`)."""
    try:
        with open(_pause_path(host, root), encoding="utf-8") as fh:
            d = json.load(fh)
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) and d.get("at") else None


def resume(host, root=None):
    """Lift the pause; True if there was one."""
    try:
        os.remove(_pause_path(host, root))
        return True
    except OSError:
        return False


# How npm's `.cmd` shim for a CLI ends: its target and `%*`. A script runs through the program
# its shebang names -- `"%_prog%" "%dp0%\node_modules\...\cli.js" %*`, `_prog` being the
# `node.exe` beside the shim or else `node` -- and an executable directly,
# `"%dp0%\node_modules\...\cli.exe" %*`; pnpm and npm 6 spell the folder `%~dp0`.
_SHIM_TARGET_RE = re.compile(r'"%~?dp0%?\\([^"]+)"\s*%\*')
_SHIM_PROG_RE = re.compile(r'SET "_prog=([^"%]+)"')


def _shim_argv(path):
    """What the npm shim at `path` runs, as an argv ([program, script] or [executable]), or None
    when it is not one that can be read so."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return None
    targets = _SHIM_TARGET_RE.findall(text)
    if not targets:
        return None
    base = os.path.dirname(path)
    target = os.path.normpath(os.path.join(base, targets[-1].replace("\\", os.sep)))
    if not os.path.isfile(target):
        return None
    if target.lower().endswith(".exe"):
        return [target]
    m = _SHIM_PROG_RE.search(text)
    name = m.group(1) if m else "node"
    beside = os.path.join(base, name + ".exe")
    prog = beside if os.path.isfile(beside) else shutil.which(name)
    return [prog, target] if prog else None


def program(cli):
    """The argv that starts the agent CLI `cli` (a name or a path), with no shell in between.

    On Windows an npm-installed CLI is a `.cmd` shim, which `subprocess` does not find by its
    bare name (it reads no PATHEXT) and, started by its path, runs through cmd.exe -- which
    parses every argument again: a newline ends the command, `%VAR%` expands, and a quote left
    open lets `&` start another command. A side call's prompt is session content, so the shim is
    read for what it runs, and that is started directly. A batch file that cannot be read so is
    not run (FileNotFoundError, as for a CLI that is not installed)."""
    if os.name != "nt":
        return [cli]
    found = shutil.which(cli)
    if not found or os.path.splitext(found)[1].lower() not in (".cmd", ".bat"):
        return [found or cli]
    argv = _shim_argv(found)
    if argv is None:
        raise FileNotFoundError(f"{found}: a batch file that is not an npm shim, whose "
                                f"arguments cmd.exe would parse again")
    return argv


# A side call only answers, so it loads no tools, skills, extensions, context files or MCP
# servers, and runs offline from an empty directory.
OMP_FLAGS = ("--print", "--mode", "json", "--no-session", "--no-tools", "--no-extensions",
             "--no-skills", "--no-context-files", "--offline")


def omp_call(prompt, model="", cli="omp", timeout=TIMEOUT_S):
    model_args = ["--model", model] if model else []
    cmd = [*program(cli), *OMP_FLAGS, *model_args, "--", prompt]
    with tempfile.TemporaryDirectory(prefix="te-side-", ignore_cleanup_errors=True) as cwd:
        out = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace",
                             timeout=timeout, env=_env(), cwd=cwd,
                             stdin=subprocess.DEVNULL)
    text, usage = trace.omp_json_reply(out.stdout)
    if not text:
        _no_reply("omp", out)
    eff = None
    if usage:
        usage.pop("cost_usd", None)
        usage["model"] = usage.get("model") or model or "omp"
        eff, _ = accounting.effective(usage)
    return text, eff


def call_name(host_name):
    """How a notice names the side-call command: `omp -p`."""
    return "omp -p"


def runner_for(host_name, config, role=None):
    """The optimizer callable for this host. omp is the only host. The caller decides whether a
    call is allowed: micro-skills' proposer when micro-skills is on, the loop guard's judge when
    the guard is. A call that raises is noted in errors.log (`_noted`).

    `role` is accepted for the callers' sake; there is no on-device model to route to on
    Windows."""
    if host_name != "omp":
        return None
    model = getattr(config, "omp_paid_model", "") or config.paid_model
    cli = os.path.expanduser(config.omp_cli) if config.omp_cli else "omp"
    return _noted("omp", lambda prompt, _m="agent": omp_call(prompt, model=model, cli=cli))
