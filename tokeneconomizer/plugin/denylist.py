"""The approved tool-pruning list, as the plugin's own state, and taking it back.

Tool pruning decides WHICH tools to drop (`toolprune.ToolingStage`, run by `fold`). omp has no
settings file for a per-repository tool filter: the plugin keeps the applied list in its own
state (`untool_applied.json`), and the SessionStart answer names the tools to remove; the omp
extension applies it with `setActiveTools` and only there.

The writer adds and removes exactly the entries it recorded adding; an entry the person wrote
is never touched. `toolprune: auto` (the default) writes the list once a proposal forms and the
repository's sessions have been idle for `toolprune_quiet_minutes` (`fold.apply_pending`), and
the next session start says so; with `toolprune: propose` only the person's approval (`te
toolprune apply`) does. Either way it applies from the NEXT session: a running session keeps the
tools it started with.
"""
import json
import os
import time

# omp's applied list is recorded under this state key: it has no settings path.
OMP_LIST = "omp:session"


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".te.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


# What the writer added. Up to 0.1.0 the record held one file's entries (`{"tools", "path"}`);
# that shape is still read.
APPLIED = "untool_applied.json"


def _canon(path):
    return path if path == OMP_LIST else (os.path.realpath(path) if path else "")


def applied_by_path(doc):
    """{settings file: [entries the plugin added to it]} from an `untool_applied.json`."""
    if isinstance(doc.get("by_path"), dict):
        return {_canon(p): list(t or []) for p, t in doc["by_path"].items()}
    if doc.get("tools"):
        return {_canon(doc.get("path") or ""): list(doc["tools"])}
    return {}


def kept_by_path(doc):
    """{settings file: [entries the person gave back with `restore`]}: never applied again by
    `toolprune: auto`, which would otherwise remove them at the next fold."""
    kept = doc.get("kept")
    return {_canon(p): list(t or []) for p, t in kept.items()} if isinstance(kept, dict) else {}


def write_applied(store, by_path, kept=None):
    doc = store.read(APPLIED) or {}
    kept = kept_by_path(doc) if kept is None else kept
    store.write(APPLIED, {"by_path": {p: sorted(t) for p, t in by_path.items() if t},
                          "kept": {p: sorted(t) for p, t in kept.items() if t},
                          "updated": time.time()})


def kept_for(store):
    """What the person gave back, which `auto` must leave alone."""
    return list(kept_by_path(store.read(APPLIED) or {}).get(OMP_LIST) or [])


def set_kept(store, add=(), remove=()):
    """Mark entries as given back by the person (`restore`), or no longer (`apply` by name)."""
    doc = store.read(APPLIED) or {}
    kept = kept_by_path(doc)
    kept[OMP_LIST] = sorted((set(kept.get(OMP_LIST) or []) | set(add)) - set(remove))
    write_applied(store, applied_by_path(doc), kept)


def applied(store, path=None):
    """What the plugin added, under `path`; without one, in any checkout."""
    by = applied_by_path(store.read(APPLIED) or {})
    if path is None:
        return sorted(set().union(*by.values())) if by else []
    return list(by.get(_canon(path)) or by.get("") or [])


def applied_for(store):
    """What the plugin added for this checkout: the list omp answers each session with."""
    return applied(store, OMP_LIST)


def _record(store, items, path):
    by = applied_by_path(store.read(APPLIED) or {})
    by.pop("", None)
    by[_canon(path)] = sorted(items)
    write_applied(store, by)


def apply(store, tools):
    """Record `tools` as applied. Returns those newly added."""
    mine = set(applied(store, OMP_LIST))
    added = sorted(set(tools) - mine)
    if added:
        _record(store, mine | set(tools), OMP_LIST)
    return added


def restore(store, tools=None):
    """Remove entries this plugin added (all of them, or just `tools`). Returns removed."""
    mine = set(applied(store, OMP_LIST))
    target = mine if not tools else mine & set(tools)
    if not target:
        return []
    _record(store, mine - target, OMP_LIST)
    return sorted(target)
