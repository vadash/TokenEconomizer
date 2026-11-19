"""`te history`: what an import of earlier sessions did in this repository.

omp keeps no session log the plugin can read from before it was installed, so there is nothing
to import: `run` refuses, and the CLI shows why. A `history.json` written by an earlier version
is still read, so `te status` and `te history` keep reporting what it did.
"""
import os

STATE = "history.json"


def read_state(ctx):
    """`history.json` as a dict; None when there is none, or it is not a JSON object."""
    doc = ctx.store.read(STATE)
    return doc if isinstance(doc, dict) else None


def _num(v, default=0):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else default


def run(ctx, auto=False, now=None, budget_s=None, log=None):
    """An import is refused: omp's earlier sessions are not readable."""
    return {"state": "refused",
            "reason": f"{ctx.host.name} keeps no session history the plugin reads"}


def maybe_start(ctx, now=None):
    """No import starts: omp's earlier sessions are not readable."""
    return None


def summary(ctx):
    """`history.json` without its plan, for `te status` and `te history`."""
    doc = read_state(ctx)
    if doc is None:
        return None
    out = {k: v for k, v in doc.items() if k != "plan"}
    if isinstance(doc.get("plan"), list):
        out["left"] = max(0, len(doc["plan"]) - int(_num(doc.get("next"))))
    return out


def describe(doc, host="omp"):
    """One line for `te status`: what the import did here."""
    if not doc:
        return None
    state = doc.get("state")
    n = int(_num(doc.get("imported")))
    who = "omp"
    if state == "done":
        return f"read {n} earlier session(s) from {who}'s history"
    if state in ("running", "partial"):
        return (f"reading {who}'s history: {n} session(s) so far, {doc.get('left', '?')} "
                f"to go; sessions ending meanwhile are folded after it")
    if state == "skipped":
        return f"not read ({doc.get('reason') or 'skipped'})"
    if state == "stopped":
        return f"stopped ({doc.get('reason') or 'stopped'})"
    if state == "refused":
        return f"not imported ({doc.get('reason') or 'refused'})"
    if state == "error":
        return f"failed ({doc.get('reason') or 'error'})"
    return None
