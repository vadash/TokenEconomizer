"""Tiny synthetic fixtures for the offline test suite (no Docker, no datasets)."""
import json


def _tree(path, header_cwd, calls):
    """Write an omp session tree: a header, then per call an assistant `toolCall` and its
    `toolResult`, chained so the active branch holds every entry."""
    lines = [{"type": "session", "version": 3, "id": "fixture",
              "timestamp": "2026-09-30T00:00:00Z"}]
    if header_cwd:
        lines[0]["cwd"] = header_cwd
    parent, n = None, 0

    def add(message):
        nonlocal parent, n
        n += 1
        eid = "e%d" % n
        lines.append({"type": "message", "id": eid, "parentId": parent,
                      "timestamp": "2026-09-30T00:00:01Z", "message": message})
        parent = eid
        return eid

    for name, args, text, is_error in calls:
        tid = "tc%d" % n
        add({"role": "assistant", "model": "fixture", "usage": {
            "input": 1, "output": 1, "cost": {"total": 0.0}},
            "content": [{"type": "toolCall", "id": tid, "name": name,
                         "arguments": args if isinstance(args, dict) else {}}]})
        add({"role": "toolResult", "toolCallId": tid, "toolName": name,
             "isError": bool(is_error),
             "content": [{"type": "text", "text": text if isinstance(text, str) else ""}]})
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(json.dumps(x) for x in lines) + "\n")
    return path


def _call_text(res):
    if isinstance(res, list):
        return "\n".join(b.get("text", "") for b in res
                         if isinstance(b, dict) and isinstance(b.get("text"), str))
    return res if isinstance(res, str) else ""


def write_omp_session(path, entries):
    """Write a transcript `load_trajectory` reads: an omp session tree, the only session
    shape the plugin reads. `entries` is either (tool_name, input_dict, result_text[,
    cwd]) call tuples -- the outcome is read from the result text, as the old transcript
    schemas carried no status flag -- or `command_execution`/`file_change` item dicts (a
    command becomes a `bash` call, failed when its exit code is not zero; a file change
    becomes an `edit` call per changed path)."""
    entries = list(entries)
    if entries and isinstance(entries[0], dict):
        flat = []
        for item in entries:
            if item.get("type") == "command_execution":
                flat.append(("bash", {"command": item.get("command", "")},
                             item.get("aggregated_output", "") or "",
                             item.get("exit_code", 0) != 0))
            elif item.get("type") == "file_change":
                for change in item.get("changes", []) or []:
                    if isinstance(change, dict) and change.get("path"):
                        flat.append(("edit", {"path": change["path"]}, "ok", False))
        return _tree(path, None, flat)
    flat = []
    for name, inp, res, *cwd in entries:
        flat.append((name, inp, _call_text(res), False, *(cwd or [None])))
    header = next((c for _, _, _, _, c in flat if c), None)
    return _tree(path, header, [(name, inp, text, False) for name, inp, text, _, _ in flat])
