"""An omp session's tool calls as one ordered list of `(name, args, result_text, success)`.

`load_trajectory` reads a finished omp session, or the event transcript `omp_write_session`
wrote from one, and returns the same tuples, so a mechanism never needs to know which shape it
was handed.
"""
import json
import os
import re

from . import omp

# rtk changes the COMMAND LINE rather than hooking it: the agent is told to type `rtk pytest`,
# `rtk git status`, `rtk proxy <cmd>`. Every classifier that reads a command's first token --
# the micro-skill vocabulary, a test-run detector -- would otherwise stop seeing a test run, a
# fetch or a repository query. One definition, so the classifiers cannot disagree about what
# counts as the wrapper. `rtk proxy` is rtk's own passthrough (run unfiltered, still tracked); a
# bare `rtk` with no command is left alone.
RTK_PREFIX_RE = re.compile(r"^rtk\s+(?:proxy\s+)?(?=\S)")


def strip_rtk_prefix(command):
    """`rtk pytest -q` -> `pytest -q`; anything else unchanged."""
    return RTK_PREFIX_RE.sub("", (command or "").lstrip(), count=1)


def _events(path):
    """The session's canonical events: `omp_session_events` for a raw session file, else the
    transcript's own lines."""
    if not os.path.exists(path):
        return []
    recs = []
    for line in open(path, encoding="utf-8", errors="replace"):
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev, dict):
            recs.append(ev)
    if recs and recs[0].get("type") == "session":
        return omp.omp_session_events(path)
    return recs


def _calls(path):
    """[(name, args, result_text, success, cwd)] over the session's events: each `tool_use`
    block paired with its `tool_result` by id, with the directory the shell was in."""
    out, order, seen, results = [], [], {}, {}
    for ev in _events(path):
        if ev.get("type") not in ("assistant", "user"):
            continue
        msg = ev.get("message") or {}
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use":
                tid = b.get("id")
                if not isinstance(tid, str) or tid in seen:
                    continue
                name = b.get("name")
                args = b.get("input", {})
                seen[tid] = {"name": name if isinstance(name, str) else "",
                             "args": args if isinstance(args, dict) else {},
                             "cwd": ev.get("cwd") if isinstance(ev.get("cwd"), str) else None}
                order.append(tid)
            elif b.get("type") == "tool_result":
                tid = b.get("tool_use_id")
                if not isinstance(tid, str):
                    continue
                c = b.get("content")
                if isinstance(c, list):
                    c = "\n".join(x.get("text") for x in c
                                  if isinstance(x, dict) and isinstance(x.get("text"), str))
                results[tid] = (c if isinstance(c, str) else "",
                                not b.get("is_error", False))
    for tid in order:
        info = seen[tid]
        res_text, ok = results.get(tid, ("", True))
        out.append((info["name"], info["args"], res_text, ok, info["cwd"]))
    return out


def load_trajectory(path):
    """Ordered [(tool_name, args, result_text, success)] over a saved omp session."""
    return [c[:4] for c in _calls(path)]


def call_cwds(path):
    """The directory each call of `load_trajectory(path)` ran in, aligned with it; None where
    the log does not say. omp runs each command afresh in the session's directory, and the
    session header records it on every event."""
    return [c[4] for c in _calls(path)]


def tool_usage(path):
    """{tool_name: calls} over a saved session."""
    counts = {}
    for name, _args, _res, _ok in load_trajectory(path):
        if name:
            counts[name] = counts.get(name, 0) + 1
    return counts
