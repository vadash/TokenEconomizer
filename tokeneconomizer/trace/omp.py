"""omp (oh-my-pi): a session stored as a TREE of JSONL entries.

Entries link by `id` / `parentId`; the conversation is the path from the current leaf back to
the root, and an abandoned branch stays in the file. Assistant messages carry `toolCall` blocks
({id, name, arguments}) and their own usage; tool results are `toolResult` messages with
`isError`. `omp_session_events` turns the ACTIVE branch into the event envelopes the call
readers take, so no mechanism needs a branch of its own.

Usage, per omp's own type: `input` excludes the cache fields, `output` already includes
reasoning, and `cacheWrite1h` is a subset of `cacheWrite` -- so the mapping to the ledger's four
fields is direct, and `cacheWrite1h` is their 1-hour share (`cache_creation_1h_input_tokens`).
"""
import json
import os

from .jsonl import records


def _entries(path):
    return list(records(path))


def omp_session_header(path):
    """The `session` header line ({id, cwd, version, ...}), {} if absent."""
    for ev in _entries(path)[:1]:
        if ev.get("type") == "session":
            return ev
    return {}


def omp_active_branch(entries):
    """Entries on the path from the last entry (the leaf) back to the root, in order."""
    by_id = {e.get("id"): e for e in entries if e.get("id")}
    tree = [e for e in entries if e.get("id")]
    if not tree:
        return []
    path, cur, seen = [], tree[-1], set()
    while cur is not None and cur.get("id") not in seen:
        seen.add(cur.get("id"))
        path.append(cur)
        cur = by_id.get(cur.get("parentId"))
    return list(reversed(path))


def omp_prompt_count(path):
    """How many prompts the conversation ran: `user` messages on its active branch."""
    return sum(1 for e in omp_active_branch(_entries(path))
               if e.get("type") == "message" and (e.get("message") or {}).get("role") == "user")


def _usage(u):
    if not isinstance(u, dict):
        return None
    return {"input_tokens": int(u.get("input") or 0),
            "cache_read_input_tokens": int(u.get("cacheRead") or 0),
            "cache_creation_input_tokens": int(u.get("cacheWrite") or 0),
            "output_tokens": int(u.get("output") or 0),
            "cache_creation_1h_input_tokens": int(u.get("cacheWrite1h") or 0),
            "cost_usd": float((u.get("cost") or {}).get("total") or 0)}


def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(c.get("text", "") for c in content
                         if isinstance(c, dict) and c.get("type") == "text")
    return ""


def omp_session_events(path):
    """The active branch's messages as `(assistant|user, message[, cwd])` event dicts.

    omp starts every shell command afresh in the session's directory, so each call is written
    with the header's `cwd`: an agent that types `cd /abs/repo && tox` into that directory ran
    plain `tox`, and is read that way."""
    entries = _entries(path)
    head = entries[0] if entries and entries[0].get("type") == "session" else {}
    cwd = head.get("cwd") if isinstance(head.get("cwd"), str) else None
    out = []
    for e in omp_active_branch(entries):
        if e.get("type") != "message":
            continue
        m = e.get("message") or {}
        role = m.get("role")
        if role == "user":
            out.append({"type": "user", "message": {
                "role": "user", "content": _text(m.get("content"))}})
        elif role == "assistant":
            blocks = []
            for b in m.get("content") or []:
                if isinstance(b, dict) and b.get("type") == "toolCall":
                    blocks.append({"type": "tool_use", "id": b.get("id"),
                                   "name": b.get("name"), "input": b.get("arguments") or {}})
                elif isinstance(b, dict) and b.get("type") == "text":
                    blocks.append({"type": "text", "text": b.get("text", "")})
            msg = {"id": e.get("id"), "role": "assistant", "content": blocks,
                   "model": m.get("model")}
            usage = _usage(m.get("usage"))
            if usage:
                msg["usage"] = {k: v for k, v in usage.items() if k != "cost_usd"}
            out.append(dict({"type": "assistant", "message": msg},
                            **({"cwd": cwd} if cwd else {})))
        elif role == "toolResult":
            out.append({"type": "user", "message": {"role": "user", "content": [{
                "type": "tool_result", "tool_use_id": m.get("toolCallId"),
                "content": _text(m.get("content")),
                "is_error": bool(m.get("isError"))}]}})
    return out


def omp_write_session(path, out_path):
    """Write `omp_session_events(path)` as JSONL. Returns the number of tool calls written, or
    None when the file is missing."""
    if not path or not os.path.exists(path):
        return None
    events = omp_session_events(path)
    with open(out_path, "w", encoding="utf-8") as out:
        for ev in events:
            out.write(json.dumps(ev) + "\n")
    return sum(1 for ev in events if ev.get("type") == "assistant"
               for b in (ev.get("message") or {}).get("content") or []
               if isinstance(b, dict) and b.get("type") == "tool_use")


def omp_json_reply(stdout):
    """(text, usage) of an `omp --print --mode json` run: the last assistant message's text, and
    the usage of every assistant message in ledger fields, with omp's model id and its own cost
    estimate (`cost_usd`). `usage` is None when the run produced no assistant message."""
    text, usage = "", None
    for line in (stdout or "").splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        m = ev.get("message") if isinstance(ev, dict) and ev.get("type") == "message_end" else None
        if not isinstance(m, dict) or m.get("role") != "assistant":
            continue
        text = _text(m.get("content")) or text
        u = _usage(m.get("usage"))
        if u:
            usage = usage or dict.fromkeys(u, 0)
            for k, v in u.items():
                usage[k] += v
            usage["model"] = m.get("model") or usage.get("model")
    return text, usage


def omp_session_usage(path):
    """Token breakdown of an omp session's active branch, plus omp's own cost estimate."""
    totals = {"input_tokens": 0, "cache_read_input_tokens": 0,
              "cache_creation_input_tokens": 0, "output_tokens": 0,
              "cache_creation_1h_input_tokens": 0}
    cost, requests, models = 0.0, 0, {}
    for e in omp_active_branch(_entries(path)):
        u = None
        if e.get("type") == "message" and (e.get("message") or {}).get("role") == "assistant":
            m = e["message"]
            u = _usage(m.get("usage"))
            if m.get("model"):
                models[m["model"]] = models.get(m["model"], 0) + 1
        elif e.get("type") in ("usage", "compaction", "branch_summary"):
            u = _usage(e.get("usage"))
        if u:
            requests += 1
            cost += u.pop("cost_usd")
            for k in totals:
                totals[k] += u[k]
    totals.update(requests=requests, cost_usd_vendor=round(cost, 6),
                  model=max(models, key=models.get) if models else None)
    return totals
