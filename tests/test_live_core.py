"""The live layer's plumbing: repository identity, state, config, session logs, host I/O.

Offline and synthetic. The transcript fixtures reproduce the two properties of real session logs
that the harness's own transcripts never had, and that the live readers exist to handle: Claude
Code writes one entry PER CONTENT BLOCK, each repeating its message's usage, and Codex's rollout
wraps exec-stream item fields in PascalCase `event_msg/item_completed` records.
"""
import json
import os
import subprocess

import pytest

from tokeneconomizer import trace
from tokeneconomizer.plugin import config as live_config
from tokeneconomizer.plugin import hosts, paths
from tokeneconomizer.plugin.store import Store



# ---- repository identity ---------------------------------------------------------------
def _git_repo(root, remote=None):
    os.makedirs(os.path.join(root, ".git"), exist_ok=True)
    with open(os.path.join(root, ".git", "HEAD"), "w") as fh:
        fh.write("ref: refs/heads/main\n")
    cfg = "[core]\n\trepositoryformatversion = 0\n"
    if remote:
        cfg += f'[remote "origin"]\n\turl = {remote}\n\tfetch = +refs/heads/*:refs/remotes/origin/*\n'
    with open(os.path.join(root, ".git", "config"), "w") as fh:
        fh.write(cfg)
    return root


def test_repo_identity_follows_the_remote_not_the_spelling(tmp_path):
    a = _git_repo(str(tmp_path / "a"), "git@github.com:Owner/Repo.git")
    b = _git_repo(str(tmp_path / "b" / "repo"), "https://github.com/owner/repo")
    ra, rb = paths.repo_identity(a), paths.repo_identity(b)
    assert ra.remote == rb.remote == "github.com/owner/repo"
    assert ra.label == "owner/repo"
    # the key carries the checkout's name for readability, and the remote for identity
    assert ra.key.endswith(rb.key.split("-")[-1])


def test_a_subdirectory_and_a_worktree_share_their_repository(tmp_path):
    main = _git_repo(str(tmp_path / "main"), "https://github.com/o/r.git")
    os.makedirs(os.path.join(main, "src", "pkg"))
    wt_git = os.path.join(main, ".git", "worktrees", "feature")
    os.makedirs(wt_git)
    with open(os.path.join(wt_git, "commondir"), "w") as fh:
        fh.write("../..")
    wt = str(tmp_path / "feature")
    os.makedirs(wt)
    with open(os.path.join(wt, ".git"), "w") as fh:
        fh.write(f"gitdir: {wt_git}\n")
    sub = paths.repo_identity(os.path.join(main, "src", "pkg"))
    assert sub.root == main
    assert paths.repo_identity(wt).remote == sub.remote == "github.com/o/r"


def test_a_directory_without_git_is_keyed_by_its_path(tmp_path, monkeypatch):
    d = str(tmp_path / "scratch")
    os.makedirs(d)
    # Fence the walk at tmp_path: a machine may well have a stray .git in /tmp itself.
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    r = paths.repo_identity(d)
    assert r.remote == "" and r.label == "scratch" and r.root == d


def test_state_root_honours_the_environment(tmp_path):
    assert paths.state_root({"TOKENECONOMIZER_HOME": str(tmp_path)}) == str(tmp_path)


# ---- store and config ----------------------------------------------------------------------
def test_store_writes_atomically_and_logs(tmp_path):
    s = Store(str(tmp_path / "st"))
    s.write("a/b.json", {"x": 1})
    assert s.read("a/b.json") == {"x": 1}
    assert s.read("missing.json", default={}) == {}
    s.append("log.jsonl", {"n": 1})
    s.append("log.jsonl", {"n": 2})
    assert [r["n"] for r in s.read_lines("log.jsonl")] == [1, 2]
    assert [r["n"] for r in s.read_lines("log.jsonl", limit=1)] == [2]
    with s.locked("x") as got_x:
        with s.locked("y") as got_y:              # distinct locks never deadlock
            s.write("c.json", [1])
        with s.locked("x", timeout=0.1) as again:  # held: a hook's caller still proceeds,
            assert again is False                  # and is told it is unlocked
    assert got_x and got_y
    assert not [f for f in os.listdir(s.base) if f.endswith(".tmp")]


def test_config_layers_and_never_raises_on_bad_values(tmp_path):
    root = str(tmp_path)
    repo_dir = os.path.join(root, "repos", "r")
    os.makedirs(repo_dir)
    with open(os.path.join(root, "config.json"), "w") as fh:
        json.dump({"loopguard": "guard", "toolprune_evidence": 7, "bogus": 1}, fh)
    with open(os.path.join(repo_dir, "config.json"), "w") as fh:
        json.dump({"loopguard": "nope", "microskills": "envfacts+distiller"}, fh)
    cfg = live_config.load_config(root, repo_dir=repo_dir, env={"TE_TOOLPRUNE": "auto"})
    assert cfg.loopguard == "nudge"                # invalid values are ignored
    assert cfg.toolprune_evidence == 7 and cfg.toolprune == "auto"
    assert cfg.microskills == "envfacts+distiller"
    joined = " ".join(cfg.warnings)
    assert "bogus" in joined and "nope" in joined and "guard" in joined
    assert live_config.load_config(root, env={"TE_DISABLE": "1"}).enabled is False


def test_write_config_validates_before_writing(tmp_path):
    path = str(tmp_path / "config.json")
    live_config.write_config(path, {"loopguard": "off", "paid_model": "haiku"})
    assert json.load(open(path)) == {"loopguard": "off", "paid_model": "haiku"}
    with pytest.raises(ValueError, match="unknown key 'paid'"):
        live_config.write_config(path, {"paid": "true"})          # gone: refused, and why
    assert "paid" not in json.load(open(path))
    with open(path, "w") as fh:
        json.dump({"paid": True, "loopguard": "off"}, fh)          # an old file warns
    cfg = live_config.load_config(str(tmp_path), repo_dir=None, env={})
    assert cfg.loopguard == "off" and any("paid" in w for w in cfg.warnings)
    with pytest.raises(ValueError):
        live_config.write_config(path, {"loopguard": "sometimes"})
    assert json.load(open(path))["loopguard"] == "off"


# ---- hosts ---------------------------------------------------------------------------------
def test_merge_outputs_concatenates_contexts_and_notices():
    h = hosts.Omp()
    m = hosts.merge_outputs(h.context("SessionStart", "a"), h.notice("n1"),
                            h.context("SessionStart", "b"), h.notice("n2"), None, {})
    assert m["hookSpecificOutput"]["additionalContext"] == "a\n\nb"
    assert m["systemMessage"] == "n1\nn2"


def test_there_is_no_host_registry_left_to_select():
    assert not hasattr(hosts, "HOSTS")
    assert not hasattr(hosts, "detect") and not hasattr(hosts, "default_host")
    assert not hasattr(hosts, "Host") and hosts.Omp.__bases__ == (object,)
    assert hosts.Omp().name == "omp"


# ---- omp sessions -------------------------------------------------------------------------
def _omp_session(path, cwd="/w/repo", sid="omp-1", abandoned=False):
    """An omp session tree: header, a user prompt, a bash call that fails, an edit, a bash call
    that passes -- and, optionally, an abandoned branch hanging off the first prompt."""
    lines = [{"type": "session", "version": 3, "id": sid, "timestamp": "2026-09-30T00:00:00Z",
              "cwd": cwd}]
    state = {"parent": None, "n": 0}

    def add(entry, parent="__chain__"):
        state["n"] += 1
        eid = f"e{state['n']:03d}"
        entry.update(id=eid, timestamp="2026-09-30T00:00:01Z",
                     parentId=state["parent"] if parent == "__chain__" else parent)
        lines.append(entry)
        if parent == "__chain__":
            state["parent"] = eid
        return eid

    usage = {"input": 10, "output": 50, "cacheRead": 1000, "cacheWrite": 100, "cacheWrite1h": 5,
             "reasoning": 5, "totalTokens": 1160, "cost": {"total": 0.002}}

    def call(name, args, text, err):
        tc = f"call{state['n']}"
        add({"type": "message", "message": {"role": "assistant", "provider": "openai",
             "model": "gpt-5.6-sol", "usage": usage,
             "content": [{"type": "toolCall", "id": tc, "name": name, "arguments": args}]}})
        add({"type": "message", "message": {"role": "toolResult", "toolCallId": tc,
             "toolName": name, "isError": err, "content": [{"type": "text", "text": text}]}})

    first = add({"type": "message", "message": {"role": "user", "content": "fix the bug"}})
    call("bash", {"command": "pytest -q"}, "bash: pytest: command not found", True)
    call("edit", {"path": "pkg/a.py", "oldText": "x", "newText": "y"}, "ok", False)
    call("bash", {"command": "python -m pytest -q"}, "1 passed", False)
    if abandoned:                        # a branch the user walked away from, earlier in the file
        lines.insert(3, {"type": "message", "id": "zzz", "parentId": first,
                         "timestamp": "2026-09-30T00:00:02Z", "message": {
                             "role": "assistant", "model": "gpt-5.6-sol", "usage": usage,
                             "content": [{"type": "toolCall", "id": "dead", "name": "bash",
                                          "arguments": {"command": "rm -rf build"}}]}})
    with open(path, "w") as fh:
        fh.write("\n".join(json.dumps(x) for x in lines) + "\n")
    return path


def test_an_omp_session_reads_through_the_trace_seam_on_its_active_branch(tmp_path):
    src = _omp_session(str(tmp_path / "s.jsonl"), abandoned=True)
    out = str(tmp_path / "trace.jsonl")
    assert trace.omp_write_session(src, out) == 3
    traj = trace.load_trajectory(out)
    assert [(n, a.get("command") or a.get("path"), ok) for n, a, _, ok in traj] == [
        ("bash", "pytest -q", False), ("edit", "pkg/a.py", True), ("bash", "python -m pytest -q", True)]
    assert "rm -rf build" not in open(out).read()          # the abandoned branch is not read
    # the raw session file reads through the same seam, without a conversion first
    assert trace.tool_usage(src) == {"bash": 2, "edit": 1}


def test_an_omp_call_into_the_session_directory_is_the_plain_form(tmp_path):
    """omp starts every command afresh in the session's directory, so `cd <that dir> && tox`
    is the same form as `tox`. Without a cwd per call it was learned as a second form, with the
    absolute path in the claim, and the two split one fact's evidence."""
    from tokeneconomizer.microskills.observe import PYTHON_GIT, observe_task
    src = str(tmp_path / "s.jsonl")
    _omp_session(src, cwd="/w/repo")
    lines = [json.loads(l) for l in open(src)]
    for e in lines:
        for b in (e.get("message") or {}).get("content") or []:
            if isinstance(b, dict) and b.get("arguments", {}).get("command") == "pytest -q":
                b["arguments"]["command"] = "cd /w/repo && pytest -q"
    open(src, "w").write("\n".join(json.dumps(e) for e in lines) + "\n")
    out = str(tmp_path / "trace.jsonl")
    trace.omp_write_session(src, out)
    assert trace.call_cwds(out) == ["/w/repo"] * 3
    forms = [o.form for o in observe_task(out, PYTHON_GIT)["obs"] if o.intent == "run_tests"]
    assert forms == ["pytest", "python -m pytest"]


def test_omp_usage_maps_directly_and_keeps_its_own_cost(tmp_path):
    u = trace.omp_session_usage(_omp_session(str(tmp_path / "s.jsonl"), abandoned=True))
    # three assistant messages on the active branch; output already includes reasoning
    assert u["requests"] == 3
    assert (u["input_tokens"], u["cache_read_input_tokens"], u["cache_creation_input_tokens"],
            u["output_tokens"]) == (30, 3000, 300, 150)
    assert u["cost_usd_vendor"] == pytest.approx(0.006) and u["model"] == "gpt-5.6-sol"
    assert trace.omp_session_header(str(tmp_path / "s.jsonl"))["cwd"] == "/w/repo"


def test_the_omp_host_reads_what_the_extension_sends():
    h = hosts.Omp()
    name, args, text, code = h.tool_call({"tool_name": "bash", "tool_input": {"command": "go test"},
                                          "tool_response": {"content": "FAIL",
                                                            "isError": True, "exit_code": 1}})
    assert (name, args["command"], code) == ("bash", "go test", 1) and "FAIL" in text
    assert h.headless({"TE_OMP_MODE": "print"}) and not h.headless({"TE_OMP_MODE": "tui"})


# ---- the guard sees edits made through the shell ---------------------------------------------
from tokeneconomizer import loopguard as guard  # noqa: E402
from tokeneconomizer.loopguard import shell_edits, turn  # noqa: E402


@pytest.mark.parametrize("cmd,want", [
    ("sed -i 's/a/b/' pkg/mod.py", ["mod.py"]),
    ("cd /t && sed -i.bak -e 's/a/b/' -e 's/c/d/' x.py y.py && pytest -q", ["x.py", "y.py"]),
    ("sed -i '' 's/a/b/' x.py", ["x.py"]),                         # BSD sed's empty suffix
    ("perl -pi -e 's/foo/bar/' src/a.rs", ["a.rs"]),
    # the script inside the option word, not in the next word
    ("sed -i --expression='s/a/b/' pkg/mod.py", ["mod.py"]),
    ("sed --in-place --expression=s/a/b/ a.py b.py", ["a.py", "b.py"]),
    ("sed -i -e's/a/b/' x.py", ["x.py"]),
    ("sed -i --file=fix.sed x.py", ["x.py"]),
    ("sed -ne 's/a/b/p' -i x.py", ["x.py"]),
    ("perl -i -pe's/foo/bar/' src/a.rs", ["a.rs"]),
    ("perl -i.orig -pe 's/foo/bar/' src/a.rs", ["a.rs"]),
    ("perl -Mre=eval -i -pe 's/foo/bar/' src/a.rs", ["a.rs"]),
    ("cat > repro.py <<'EOF'\nprint(1 > 0)\nEOF\npython repro.py", ["repro.py"]),
    ("echo 'x' | tee -a setup.cfg", ["setup.cfg"]),
    # how Sonnet 5.5 edits: an inline script that reads the file and writes it back
    ("python - <<'E'\np='sympy/printing/str.py'; s=open(p).read()\nopen(p,'w').write(s)\nE",
     ["str.py"]),
    ("python - <<'E'\nfor p in ['x/a.py', 'x/b.py']:\n    open(p, 'w').write('')\nE",
     ["a.py", "b.py"]),
    ("python - <<'E'\nfrom pathlib import Path\nPath('m/n.py').write_text('x')\nE", ["n.py"]),
    ("git apply /tmp/fix.diff", ["(patch)"]),
    # Codex 0.163.0 edits like this, then tests, in ONE command (seen live)
    ("apply_patch <<'PATCH'\n*** Begin Patch\n*** Update File: pkg/__init__.py\n@@\n-    x\n"
     "+    y\n*** End Patch\nPATCH\npython3 -m unittest", ["__init__.py"]),
    ("apply_patch <<'P'\n*** Begin Patch\n*** Add File: a/new.py\n+x\n*** Update File: b/old.py\n"
     "*** End Patch\nP", ["new.py", "old.py"]),
    ("git checkout -- a.py b/c.py", ["a.py", "c.py"]),
    # git's own options before the subcommand (`-C <dir>`, `-c <name>=<value>`, `--no-pager`)
    ("git -C /w/repo apply /tmp/fix.diff", ["(patch)"]),
    ("git -c core.autocrlf=false --no-pager apply fix.diff", ["(patch)"]),
    ("git -C repo checkout HEAD -- pkg/mod.py", ["mod.py"]),
    ("git --git-dir=.git --work-tree . restore pkg/mod.py", ["mod.py"]),
    # a wrapper's own options and values before the command (`sudo -u bob`, `env -u VAR`)
    ("env -u CURSOR_AGENT sed -i 's/a/b/' pkg/mod.py", ["mod.py"]),
    ("sudo -u bob sed -i 's/a/b/' pkg/mod.py", ["mod.py"]),
    ("sudo -Eu bob -- perl -pi -e 's/a/b/' src/a.rs", ["a.rs"]),
    ("timeout 60 sed -i 's/a/b/' pkg/mod.py", ["mod.py"]),
    ("timeout -k 5 --signal=KILL 1m git apply fix.diff", ["(patch)"]),
    ("nice -n 10 sed -i 's/a/b/' x.py", ["x.py"]),
    ("nice -5 tee -a setup.cfg", ["setup.cfg"]),
    ("time -p sed -i 's/a/b/' x.py", ["x.py"]),
    ("FOO=1 env -C sub -u X BAR=2 nice -n5 sed -i 's/a/b/' x.py", ["x.py"]),
    ("sudo -E git checkout -- pkg/mod.py", ["mod.py"]),
    ("env -u X sed -n '1,20p' pkg/mod.py", []),
    ("timeout 60 python -m pytest -q > /tmp/out.txt", []),
    # not edits: output captures, scratch, reads, and operators inside quotes
    ("python -m pytest -q > /tmp/out.txt 2>&1; tail -5 /tmp/out.txt", []),
    ("grep -n 'x' a.py > hits.txt", []),
    ("grep -n '>' x.py", []),
    ("grep -nE 'a|b' x.py | head", []),
    ("python -c \"print(1>2)\"", []),
    ("git stash -q; python bin/test x; git stash pop -q", []),
    ("sed -n '1,20p' pkg/mod.py", []),
    ("git -C repo diff -- pkg/mod.py", []),
    ("git -C repo log -- pkg/mod.py", []),
    # a quoted `|` in a sed script must not split the command (seen live: sympy-13091)
    ("sed -i -E 's/x (<|<=|>|>=) y/z/' numbers.py expr.py", ["numbers.py", "expr.py"]),
    ("sed -i 's/a/b/' x.py 2>&1 | tail -2", ["x.py"]),
    # a here-string (`<<<`) is not a heredoc: the lines after it are still commands
    ("grep -q ok <<< \"ok\"\nsed -i 's/a/b/' pkg/mod.py", ["mod.py"]),
    ("read -r v <<< done\ncat > fix.py <<'EOF'\nx = 1\nEOF", ["fix.py"]),
])
def test_edits_made_through_the_shell_are_recognised(cmd, want):
    assert guard.shell_edit_targets(cmd) == want


def test_a_git_command_run_with_dash_c_still_counts_its_edit_and_its_revert():
    """`git -C <dir> apply` and `git -C <dir> checkout -- <path>` name the subcommand after
    git's own options. Reading the word after `git` as the subcommand missed both, so the
    patch was no edit and putting back a file the turn edited was not counted either."""
    revert = "git -C /w/repo checkout -- pkg/mod.py"
    assert shell_edits.shell_revert_targets(revert) == ["mod.py"]
    g = guard.TurnGuard(turn_key="t")
    g.fold("Bash", {"command": "git -C /w/repo apply /tmp/fix.diff"}, "", 0)
    g.fold("Bash", {"command": "sed -i 's/a/b/' pkg/mod.py"}, "", 0)
    g.fold("Bash", {"command": revert}, "", 0)
    assert g.edits == 3
    assert g.whole.edit_counts == {"(patch)": 1, "mod.py": 2}


def test_an_edit_run_through_a_wrapper_with_options_still_counts():
    """`env -u VAR`, `sudo -u bob`, `timeout 60` and `nice -n 10` run the command after their
    own options. Dropping only the wrapper's name left `-u` or `60` as the command, so the
    `sed -i` was no edit and the `git checkout --` that put the file back was no revert."""
    revert = "sudo -u bob git checkout -- pkg/mod.py"
    assert shell_edits.shell_revert_targets(revert) == ["mod.py"]
    g = guard.TurnGuard(turn_key="t")
    g.fold("Bash", {"command": "env -u CURSOR_AGENT sed -i 's/a/b/' pkg/mod.py"}, "", 0)
    g.fold("Bash", {"command": "timeout 60 sed -i 's/b/c/' pkg/mod.py"}, "", 0)
    g.fold("Bash", {"command": revert}, "", 0)
    assert g.edits == 3
    assert g.whole.edit_counts == {"mod.py": 3}


def test_an_edit_after_a_here_string_is_counted():
    """`<<<` feeds one word to a command; reading it as a heredoc tag made every later line of
    the command a heredoc body, so a `sed -i` after it was not counted as an edit."""
    g = guard.TurnGuard(turn_key="t")
    g.fold("Bash", {"command": "grep -q ok <<< ok\nsed -i 's/a/b/' pkg/mod.py"}, "", 0)
    assert g.edits == 1 and g.whole.edit_counts == {"mod.py": 1}


def test_shell_edits_reach_the_state_the_judge_reads_and_the_ended_green_proxy():
    g = guard.TurnGuard(turn_key="t")
    edit = "python - <<'E'\np='pkg/mod.py'; s=open(p).read()\nopen(p,'w').write(s[::-1])\nE"
    for _ in range(2):
        g.fold("Bash", {"command": edit}, "")
    assert g.st.edit_churn == 2 and g.whole.distinct_edit_files == 1 and g.edits == 2
    digest = g.st.recent_digest()
    assert "edit_churn=2" in digest and "[edits mod.py]" in digest
    # one command that edits and then fails its tests: the turn did not end green
    g.fold("Bash", {"command": "sed -i 's/b/c/' pkg/mod.py && pytest -q"},
           "FAILED tests/test_m.py::t - AssertionError")
    assert g.edits == 3 and g.last_verify > g.last_edit and not g.resolved_proxy()


def test_a_sed_script_given_inside_its_option_word_still_counts_the_edit():
    """`sed -i --expression=...` and `perl -i -pe'...'` carry the script in the option word.
    The file after it is the edited file, not the script, so the turn records an edit."""
    g = guard.TurnGuard(turn_key="t")
    g.fold("Bash", {"command": "sed -i --expression='s/a/b/' pkg/mod.py"}, "", 0)
    g.fold("Bash", {"command": "perl -i -pe's/b/c/' pkg/mod.py"}, "", 0)
    assert g.edits == 2 and g.whole.edit_counts == {"mod.py": 2}
    assert "[edits mod.py]" in g.st.recent_digest()


def test_an_edit_named_by_a_windows_path_counts_by_its_basename():
    """Claude Code on Windows names the file an edit tool changed with backslashes. The guard
    counts that edit under the file's basename, as it counts a shell edit or a `git checkout --`
    of the same file, and the judge's digest names the basename only."""
    g = guard.TurnGuard(turn_key="t")
    g.fold("Edit", {"file_path": r"C:\w\repo\pkg\mod.py", "old_string": "a", "new_string": "b"},
           "ok")
    g.fold("Bash", {"command": "git checkout -- pkg/mod.py"}, "", 0)
    g.fold("Bash", {"command": "sed -i 's/a/b/' pkg/mod.py"}, "", 0)
    assert g.whole.edit_counts == {"mod.py": 3}
    assert g.whole.distinct_edit_files == 1
    assert "C:" not in g.st.recent_digest()


def test_a_patch_that_changes_several_files_counts_each_of_them():
    """One `apply_patch` shell command can change several files; each counts as an edit."""
    patch = ("*** Begin Patch\n*** Update File: pkg/a.py\n@@\n-x\n+y\n"
             "*** Update File: pkg/b.py\n@@\n-x\n+y\n*** Add File: tests/test_c.py\n+z\n"
             "*** End Patch")
    shell = guard.TurnGuard(turn_key="t")
    shell.fold("bash", {"command": f"apply_patch <<'PATCH'\n{patch}\nPATCH"}, "ok", 0)
    assert shell.whole.edit_counts == {"a.py": 1, "b.py": 1, "test_c.py": 1}
    assert shell.whole.distinct_edit_files == 3
    assert "[edits a.py, b.py, test_c.py]" in shell.st.recent_digest()


def test_reading_a_file_full_of_asserts_is_not_a_failure():
    """A test file read back is full of `assert` and `Error`: reading it, three times over, is
    no failure and no repeated failure, and opens no gate to the judge."""
    test_file = "def test_total():\n    assert total([1, 2]) == 3\n    raise ValueError('no')\n"
    g = guard.TurnGuard(turn_key="t")
    for _ in range(3):
        g.fold("read", {"path": "tests/test_total.py"}, test_file)
        g.fold("grep", {"pattern": "assert"}, "tests/test_total.py:2:    assert total")
    g.fold("bash", {"command": "cat tests/test_total.py"}, test_file, 0)
    assert (g.whole.error_outputs, g.whole.repeated_fail_sigs) == (0, 0)
    assert not guard.gate_for_judge(g.whole, dict(guard.DEFAULT_THRESHOLDS, gate_min_turns=1))
    # what did fail still counts: a non-zero exit, or a read the tool itself reports as failed
    g.fold("bash", {"command": "pytest -q"}, "FAILED tests/test_total.py - AssertionError", 1)
    g.fold("bash", {"command": "pytest -q"}, "FAILED tests/test_total.py - AssertionError", None)
    assert (g.whole.error_outputs, g.whole.repeated_fail_sigs) == (2, 1)
    s = guard.SpiralState()
    s.update("Read", {"file_path": "x.py"}, "Error: File does not exist.", failed=True)
    assert s.error_outputs == 1


def test_the_judge_is_told_how_the_last_verification_went_not_that_tests_are_red():
    g = guard.TurnGuard(turn_key="t")
    vendored = "turns=3\nrepo_context: ... = 1.2x that typical length. Tests are still NOT green. A"
    assert "No test or verification run has been seen" in g.live_digest(vendored)
    g.fold("Bash", {"command": "python bin/test sympy/core"}, "tests finished: 10 passed")
    told = g.live_digest(vendored)
    assert "The last verification run passed." in told and "NOT green" not in told
    g.fold("Edit", {"file_path": "/r/a.py", "old_string": "a", "new_string": "b"}, "ok")
    assert g.live_digest(vendored).count("There have been edits since.") == 1


class _BackgroundJudge:
    """`plugin.judge.AsyncJudge`'s interface, with the verdict handed in by the test."""

    def __init__(self):
        self.started, self.verdict = [], None

    def start(self, g, digest):
        self.started.append(digest)
        return f"job{len(self.started)}"

    def poll(self, g):
        pending = g.judge_pending or {}
        if self.verdict is None or pending.get("id") != f"job{len(self.started)}":
            return False, None
        v, self.verdict = self.verdict, None
        return True, v


def _spin(g, judge, n, envelope=6.0):
    """An edit and an identical failing test run, alternately: a loop the gate opens on."""
    out = []
    for k in range(n):
        call = (("Edit", {"file_path": "/r/a.py", "old_string": "x", "new_string": "y"}, "ok")
                if k % 2 == 0 else
                ("Bash", {"command": "pytest -q"}, "FAILED tests/test_a.py::t - AssertionError"))
        out.append(g.fold(*call, envelope=envelope, judge=judge))
    return out


def test_a_background_judge_starts_once_and_its_verdict_nudges_a_later_call():
    g, j = guard.TurnGuard(turn_key="t"), _BackgroundJudge()
    assert _spin(g, j, 10) == [None] * 10            # nothing is decided while it is out
    assert len(j.started) == 1                       # and no second job is started
    assert "NOT green" not in j.started[0]
    j.verdict = {"stuck": True, "confidence": 0.9, "reason": "loop",
                 "_nudge": "Read the failing assertion before editing again."}
    assert _spin(g, j, 1)[0] == ("nudge", "judge-stuck@0.90:loop")
    assert g.nudge_body(6.0) == "Read the failing assertion before editing again."
    assert g.nudge_body(6.0) != "Read the failing assertion before editing again."  # used once


def test_a_productive_verdict_backs_the_judge_off():
    g, j = guard.TurnGuard(turn_key="t"), _BackgroundJudge()
    _spin(g, j, 7)
    j.verdict = {"stuck": False, "confidence": 0.8, "reason": "progress"}
    assert _spin(g, j, 1) == [None]
    assert g.judge_gap == g.JUDGE_BASE_GAP * 2 and g.judge_pending is None


def test_a_job_that_never_answers_stops_blocking_the_next_one():
    g, j = guard.TurnGuard(turn_key="t"), _BackgroundJudge()
    _spin(g, j, 7)
    assert len(j.started) == 1 and g.judge_pending
    g.judge_pending["since"] = 0                     # its process died
    _spin(g, j, g.judge_gap)                         # well before the backstop at 2.5x
    assert len(j.started) == 2


# npm's `.cmd` shims on Windows, as its cmd-shim writes them: a script run through node, and an
# executable run directly; npm 6 (and pnpm) spell the folder `%~dp0`.
_SHIM_HEAD = ('@ECHO off\r\nGOTO start\r\n:find_dp0\r\nSET dp0=%~dp0\r\nEXIT /b\r\n:start\r\n'
              'SETLOCAL\r\nCALL :find_dp0\r\n')
NPM_SCRIPT_SHIM = (_SHIM_HEAD + '\r\nIF EXIST "%dp0%\\node.exe" (\r\n'
                   '  SET "_prog=%dp0%\\node.exe"\r\n) ELSE (\r\n  SET "_prog=node"\r\n'
                   '  SET PATHEXT=%PATHEXT:;.JS;=;%\r\n)\r\n\r\n'
                   'endLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & "%_prog%"  '
                   '"%dp0%\\node_modules\\@openai\\codex\\bin\\codex.js" %*\r\n')
NPM_EXE_SHIM = _SHIM_HEAD + '"%dp0%\\node_modules\\@opencode\\cli\\bin\\opencode.exe"   %*\r\n'
NPM6_SHIM = ('@IF EXIST "%~dp0\\node.exe" (\r\n'
             '  "%~dp0\\node.exe"  "%~dp0\\..\\lib\\pi\\cli.js" %*\r\n) ELSE (\r\n'
             '  @SETLOCAL\r\n  @SET PATHEXT=%PATHEXT:;.JS;=;%\r\n'
             '  node  "%~dp0\\..\\lib\\pi\\cli.js" %*\r\n)\r\n')


def _shim(folder, text, *files):
    """`folder/cli.cmd` holding `text`, with `files` (relative to `folder`) beside it."""
    for rel in files:
        path = folder / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / "cli.cmd", "w", newline="") as fh:
        fh.write(text)
    return str(folder / "cli.cmd")


def test_an_npm_shim_is_read_for_what_it_runs(tmp_path, monkeypatch):
    from tokeneconomizer.plugin import sidecall
    monkeypatch.setattr(sidecall.shutil, "which",
                        lambda name: "/usr/bin/node" if name == "node" else None)
    script = os.path.join("node_modules", "@openai", "codex", "bin", "codex.js")
    a = _shim(tmp_path / "a", NPM_SCRIPT_SHIM, script, "node.exe")
    assert sidecall._shim_argv(a) == [str(tmp_path / "a" / "node.exe"),
                                      str(tmp_path / "a" / script)]
    b = _shim(tmp_path / "b", NPM_SCRIPT_SHIM, script)            # no node.exe beside it
    assert sidecall._shim_argv(b) == ["/usr/bin/node", str(tmp_path / "b" / script)]
    exe = os.path.join("node_modules", "@opencode", "cli", "bin", "opencode.exe")
    c = _shim(tmp_path / "c", NPM_EXE_SHIM, exe)
    assert sidecall._shim_argv(c) == [str(tmp_path / "c" / exe)]
    d = _shim(tmp_path / "d" / "bin", NPM6_SHIM, os.path.join("..", "lib", "pi", "cli.js"))
    assert sidecall._shim_argv(d) == ["/usr/bin/node",
                                      str(tmp_path / "d" / "lib" / "pi" / "cli.js")]
    # its target gone, or a batch file of some other shape: nothing it would run is known
    assert sidecall._shim_argv(_shim(tmp_path / "e", NPM_SCRIPT_SHIM)) is None
    assert sidecall._shim_argv(_shim(tmp_path / "f", "@echo off\r\ncodex-real.exe %*\r\n")) is None


def test_a_cli_on_windows_is_started_without_cmd_exe(tmp_path, monkeypatch):
    """A side call's prompt is session content: through cmd.exe a newline would end it, `%VAR%`
    expand and `"...&` start another command. A batch file that is not an npm shim is not run."""
    from tokeneconomizer.plugin import sidecall
    script = os.path.join("node_modules", "@openai", "codex", "bin", "codex.js")
    shim = _shim(tmp_path / "npm", NPM_SCRIPT_SHIM, script, "node.exe")
    other = _shim(tmp_path / "other", "@echo off\r\ntool.exe %*\r\n")
    found = {"codex": shim, "tool": other, "pi": str(tmp_path / "pi.exe")}
    monkeypatch.setattr(sidecall.shutil, "which", found.get)
    monkeypatch.setattr(sidecall.os, "name", "nt")
    assert sidecall.program("codex") == [str(tmp_path / "npm" / "node.exe"),
                                         str(tmp_path / "npm" / script)]
    assert sidecall.program("pi") == [str(tmp_path / "pi.exe")]
    assert sidecall.program("opencode") == ["opencode"]       # not installed: that error, as before
    with pytest.raises(FileNotFoundError):
        sidecall.program("tool")
def test_a_small_envelopes_backstop_never_preempts_the_nudge():
    """Interactive repositories learn envelopes of ~4-10 calls, so the hard backstop
    (1.8-2.5x the envelope) lies inside the stuck rule's own floor: before this fix, the first
    stuck call tripped the backstop and the turn was stopped without ever being redirected."""
    from tokeneconomizer import loopguard as guard
    thr = guard.scaled_thresholds("aggressive")
    fail = ("FAIL: test_floats (tests.test_calc.TestAdd)\nTraceback (most recent call last):\n"
            "AssertionError: 0.30000000000000004 != 0.3\n\nFAILED (failures=1)\n")
    g = guard.TurnGuard(turn_key="t")
    calls = [("Bash", {"command": "python -m unittest"}, fail, 1)]
    for k in range(30):
        calls += [("Edit", {"file_path": "src/calc/__init__.py", "old_string": "x",
                            "new_string": f"y{k}"}, "ok", None),
                  ("Bash", {"command": "python -m unittest"}, fail, 1)]
    fired = []
    for i, (n, a, r, c) in enumerate(calls, 1):
        d = g.fold(n, a, r, c, envelope=4.0, thr=thr, nudge_budget=1)
        if d:
            fired.append((i, d[0]))
            if d[0] == "nudge":
                g.after_nudge()
            else:
                break
    assert fired == [(14, "nudge"), (28, "abort")]


@pytest.mark.parametrize("cmd", ["python tests/runtests.py model_inheritance -v 2",
                                 "./runtests.py basic", ".venv/bin/pytest -q tests/test_a.py",
                                 "cd web && ./node_modules/.bin/jest src", "/usr/bin/python3 -m pytest",
                                 "/w/venv/bin/pytest -q", "~/.local/bin/pytest -q",
                                 "python3 /w/repo/tests/runtests.py basic", "node --test test/"])
def test_a_runner_named_by_path_counts_as_a_verification(cmd):
    """A failing run after the last edit means the turn did not end green, whatever path the
    runner was called by -- otherwise long failing turns feed the envelope and the guard
    stops seeing them as long."""
    g = guard.TurnGuard(turn_key="t")
    g.fold("Edit", {"file_path": "/r/a.py", "old_string": "x", "new_string": "y"}, "ok")
    g.fold("Bash", {"command": cmd}, "FAILED (failures=1)", 1)
    assert g.verifications == 1 and not g.resolved_proxy()


@pytest.mark.parametrize("cmd", ["uv run pytest -q", "uv run --frozen python -m pytest x.py",
                                 "poetry run pytest", "pipenv run pytest", "npx jest src",
                                 "npx -y vitest run", "pnpm exec jest", "bundle exec rspec",
                                 "xvfb-run -a pytest", "cd web && npx jest"])
def test_a_runner_started_by_a_launcher_counts_as_a_verification(cmd):
    """`uv run`, `npx`, `bundle exec` are passed over like `timeout 60`: the run is the runner's,
    so its status is the tests' own."""
    assert turn.status_of_tests(cmd) == "own"
    g = guard.TurnGuard(turn_key="t")
    g.fold("Edit", {"file_path": "/r/a.py", "old_string": "x", "new_string": "y"}, "ok")
    g.fold("Bash", {"command": cmd}, "FAILED (failures=1)", 1)
    assert g.verifications == 1 and not g.resolved_proxy()


@pytest.mark.parametrize("cmd", ["env -u CURSOR_AGENT python -m pytest tests",
                                 "env -i PATH=/usr/bin go test ./...", "env --unset=CI npm test",
                                 "env -C web npx vitest run", "nice -n 10 pytest -q",
                                 "nice -n10 cargo test", "nice -5 go test ./...",
                                 "FOO=1 env -u X timeout 60 npm test"])
def test_a_runner_behind_env_or_nice_options_counts_as_a_verification(cmd):
    """`env -u VAR` and `nice -n 10` take options before the command they run. Only the bare
    word was passed over, so the run was no verification and a failing one after the last edit
    left the turn green."""
    assert turn.status_of_tests(cmd) == "own"
    g = guard.TurnGuard(turn_key="t")
    g.fold("Edit", {"file_path": "/r/a.py", "old_string": "x", "new_string": "y"}, "ok")
    g.fold("Bash", {"command": cmd}, "FAILED (failures=1)", 1)
    assert g.verifications == 1 and not g.resolved_proxy()


@pytest.mark.parametrize("cmd,status", [("env -u CI python -m pytest --collect-only", None),
                                        ("env -u CI echo pytest", None),
                                        ("nice -n 10 make", None), ("env pytest", "own"),
                                        ("nice pytest -q", "own")])
def test_env_and_nice_leave_what_they_run_to_decide(cmd, status):
    assert turn.status_of_tests(cmd) == status


@pytest.mark.parametrize("cmd", ["python -W error -m pytest -q", "python3 -Werror -m pytest",
                                 "python -X dev -m pytest tests",
                                 "python3.12 -Xfaulthandler -m pytest",
                                 "python -u -m unittest discover", "python -bb -B -m pytest",
                                 "python -I -m unittest -v", ".venv/bin/python -W error -m pytest",
                                 "python -W error::DeprecationWarning manage.py test",
                                 "python -u tests/runtests.py"])
def test_a_runner_behind_pythons_own_options_counts_as_a_verification(cmd):
    """`python -W error -m pytest` and `python -X dev -m pytest` put the interpreter's options
    before `-m`. Only `python -m` was read, so the run was no verification and a failing one after
    the last edit left the turn green."""
    assert turn.status_of_tests(cmd) == "own"
    g = guard.TurnGuard(turn_key="t")
    g.fold("Edit", {"file_path": "/r/a.py", "old_string": "x", "new_string": "y"}, "ok")
    g.fold("Bash", {"command": cmd}, "FAILED (failures=1)", 1)
    assert g.verifications == 1 and not g.resolved_proxy()


@pytest.mark.parametrize("cmd", ["python -W error repro.py", "python -X dev -c 'import a'",
                                 "python -u -m pip install -e .", "python -m pytest --version"])
def test_pythons_own_options_leave_what_it_runs_to_decide(cmd):
    assert turn.status_of_tests(cmd) is None


@pytest.mark.parametrize("cmd", ["ruby test/ledger_test.rb", "ruby -w -Ilib test/slugify_test.rb",
                                 "ruby -I lib test/x_test.rb", "ruby /w/repo/test/x_test.rb",
                                 "ruby -Itest test/models/user_test.rb", "ruby spec/foo_spec.rb",
                                 "ruby -W0 --disable-gems test/test_parser.rb",
                                 "ruby -W:no-deprecated test/a_test.rb", "ruby -wv test/a_test.rb",
                                 "ruby -C /w -Itest test/a_test.rb",
                                 "bundle exec ruby -Itest test/a_test.rb -n test_x",
                                 "rake test", "bundle exec rake test", "rake test:models",
                                 "bin/rake test TEST=test/a_test.rb", "rake spec", "bin/rails test",
                                 "rails test test/models/user_test.rb"])
def test_a_ruby_test_run_counts_as_a_verification(cmd):
    """minitest and test-unit are run as `ruby` given a test file, or through rake or rails, not
    by a runner of their own as `rspec` is."""
    assert turn.status_of_tests(cmd) == "own"
    g = guard.TurnGuard(turn_key="t")
    g.fold("Edit", {"file_path": "/r/lib/a.rb", "old_string": "x", "new_string": "y"}, "ok")
    g.fold("Bash", {"command": cmd}, "8 runs, 8 assertions, 1 failures, 0 errors, 0 skips", 1)
    assert g.verifications == 1 and not g.resolved_proxy()


@pytest.mark.parametrize("cmd", ["ruby -Ilib -e 'require \"a\"; p A.new'", "ruby -wc lib/retry.rb",
                                 "ruby -c test/a_test.rb", "ruby -wc test/a_test.rb",
                                 "ruby -Wc test/a_test.rb", "ruby -cw test/a_test.rb",
                                 "ruby -we 'load \"test/a_test.rb\"'", "ruby repro.rb", "ruby -v",
                                 "ruby -e 'load \"test/a_test.rb\"'", "ruby lib/contest.rb",
                                 "ruby -Icore lib/x.rb", "rake", "rake db:migrate",
                                 "rails server", "cat test/a_test.rb"])
def test_ruby_that_runs_no_test_file_is_not_a_verification(cmd):
    """A one-off `ruby -e`, a syntax check and a script are not the project's checks."""
    assert turn.status_of_tests(cmd) is None


def test_a_piped_minitest_run_is_judged_by_its_summary():
    """`ruby test/x_test.rb | tail -5` exits with `tail`'s status, so minitest's summary line
    decides whether the turn ended green."""
    g = guard.TurnGuard(turn_key="t")
    g.fold("Edit", {"file_path": "/r/lib/a.rb", "old_string": "x", "new_string": "y"}, "ok")
    g.fold("Bash", {"command": "ruby test/a_test.rb 2>&1 | tail -5"},
           "8 runs, 8 assertions, 1 failures, 0 errors, 0 skips", 0)
    assert g.verifications == 1 and not g.resolved_proxy()
    g.fold("Bash", {"command": "ruby test/a_test.rb 2>&1 | tail -5"},
           "8 runs, 8 assertions, 0 failures, 0 errors, 0 skips", 0)
    assert g.verifications == 2 and g.resolved_proxy()


@pytest.mark.parametrize("cmd,status", [("uv run ruff check", None),
                                        ("npx prettier --check .", None),
                                        ("pnpm exec jest --help", None),
                                        ("yarn test", "own"), ("npm test", "own")])
def test_a_launcher_leaves_what_it_runs_to_decide(cmd, status):
    """A launcher alone is no test run, and `yarn test`, which names the test script, still is."""
    assert turn.status_of_tests(cmd) == status


@pytest.mark.parametrize("cmd", ["cat /w/pytest.ini", "cat pytest.ini", "ls /w/tests",
                                 "vim tests/runtests.py.bak"])
def test_a_file_named_like_a_runner_is_not_a_verification(cmd):
    assert not guard.VERIFY_RE.search(cmd)


@pytest.mark.parametrize("cmd", ["grep -rn pytest src | head",
                                 "python -m pytest x.py --collect-only -q | rg test_",
                                 "bin/test --help | head", 'git commit -m "fix pytest"',
                                 'echo "run pytest next"'])
def test_a_non_test_command_mentioning_a_runner_is_not_a_verification(cmd):
    g = guard.TurnGuard(turn_key="t")
    g.fold("Edit", {"file_path": "/r/a.py", "old_string": "x", "new_string": "y"}, "ok")
    g.fold("Bash", {"command": "pytest -q"}, "FAILED (failures=1)", 1)
    g.fold("Bash", {"command": cmd}, "ok", 0)
    assert g.verifications == 1 and not g.resolved_proxy()


def test_a_heredoc_writing_a_script_runs_no_tests():
    assert turn.status_of_tests("cat <<EOF > ci.sh\nnpm test\nEOF") is None


@pytest.mark.parametrize("cmd", ["gh pr create --body-file - <<'EOF'\npytest -q\nEOF",
                                 "gh issue comment 7 --body-file - <<EOF\nnpm test\nEOF"])
def test_a_heredoc_line_naming_a_runner_is_not_a_verification(cmd):
    """A heredoc's lines are the input of the command that reads them, as in
    `exit_status_covers`: a body written with one runs no tests, so the failing run before it
    is still the turn's last verification."""
    assert turn.status_of_tests(cmd) is None
    g = guard.TurnGuard(turn_key="t")
    g.fold("Edit", {"file_path": "/r/a.py", "old_string": "x", "new_string": "y"}, "ok")
    g.fold("Bash", {"command": "pytest -q"}, "FAILED (failures=1)", 1)
    g.fold("Bash", {"command": cmd}, "ok", 0)
    assert g.verifications == 1 and not g.resolved_proxy()


@pytest.mark.parametrize("cmd", ["pytest -q <<EOF\ny\nEOF",
                                 "cat <<EOF > ci.sh\nnpm test\nEOF\npytest -q"])
def test_a_runner_beside_a_heredoc_is_still_judged_by_its_status(cmd):
    """A runner that reads a heredoc, or runs after one, is the last command: its status is the
    tests' own."""
    assert turn.status_of_tests(cmd) == "own"


@pytest.mark.parametrize("cmd, want", [
    ("bash -lc 'pytest -q <<EOF\ny\nEOF'", "own"),
    ("bash -lc \"pytest -q && cat <<'EOF'\nsummary\nEOF\"", "own"),
    ("bash -lc \"gh pr create --body-file - <<'EOF'\npytest -q\nEOF\"", None),
])
def test_a_heredoc_that_ends_a_script_is_read_inside_the_script(cmd, want):
    """Codex records `bash -lc '<script>'`. A heredoc that ends the script closes on `EOF'` there,
    not on a line of its own, so its body is left out after the unwrap: the run before it still
    counts, and a runner named in its body still does not."""
    assert turn.status_of_tests(cmd) == want


TAP_FAILING = "# tests 9\n# suites 0\n# pass 8\n# fail 1\n# cancelled 0"


def test_a_test_run_whose_status_was_replaced_is_judged_by_its_output():
    """A pipe, `||` or a report of the status gives the command line another command's status:
    `npm test 2>&1 | tail -n 8` exits with `tail`'s 0 whatever the tests did."""
    g = guard.TurnGuard(turn_key="t")
    for _ in range(3):
        g.fold("bash", {"command": "npm test 2>&1 | tail -n 8"}, TAP_FAILING, 0)
    assert (g.whole.error_outputs, g.whole.repeated_fail_sigs) == (3, 2)
    assert not g.resolved_proxy()
    # Codex records a call as `bash -lc '<script>'`
    g = guard.TurnGuard(turn_key="t")
    g.fold("bash", {"command": "/bin/bash -lc 'cd /w && timeout 60 go test ./... 2>&1 | tail -3'"},
           "--- FAIL: TestAdd (0.00s)\nFAIL\tacme/x\t0.01s", 0)
    assert g.whole.error_outputs == 1 and not g.resolved_proxy()
    for cmd in ('python3 -m unittest tests.test_x; echo "exit=$?"', "npm test || true",
                "python tests/runtests.py app 2>&1 | tail -5"):
        g = guard.TurnGuard(turn_key="t")
        g.fold("bash", {"command": cmd}, "FAILED (failures=1)\nexit=1", 0)
        assert g.whole.error_outputs == 1 and not g.resolved_proxy()
    # under pipefail the status is the tests' own; `| grep` finding nothing is no failure
    g = guard.TurnGuard(turn_key="t")
    g.fold("bash", {"command": "set -o pipefail; npm test | tail -n 8"}, TAP_FAILING, 0)
    g.fold("bash", {"command": "pytest -q | grep FAILED"}, "", 1)
    assert g.whole.error_outputs == 0 and g.resolved_proxy()


def test_a_test_run_followed_by_other_commands_is_judged_by_its_runners_summary():
    """`pytest -q; git diff` exits with `git`'s status, and its output holds the diff too: the
    runner's own summary line says how the tests went."""
    diff = "diff --git a/m.py b/m.py\n+    raise ValueError('no')\n"
    g = guard.TurnGuard(turn_key="t")
    g.fold("bash", {"command": "pytest -q; git diff"},
           "F.\n1 failed, 1 passed in 0.12s\n" + diff, 0)
    assert g.whole.error_outputs == 1 and not g.resolved_proxy()
    # passing tests, whatever the later command's status or the words in its output
    for code in (1, 0, None):
        g = guard.TurnGuard(turn_key="t")
        g.fold("bash", {"command": "pytest -q\ngit diff --exit-code"}, "2 passed in 0.10s\n" + diff,
               code)
        assert g.whole.error_outputs == 0 and g.resolved_proxy()


@pytest.mark.parametrize("out, failed", [
    ("1 failed, 4 passed, 2 warnings in 1.23s", True), ("== 1 error in 0.12s ==", True),
    ("2 passed, 1 warning in 0.30s", False), ("no tests ran in 0.01s", None),
    ("Ran 3 tests in 0.1s\n\nFAILED (failures=1)", True), ("Ran 3 tests in 0.1s\n\nOK", False),
    ("tests finished: 10 passed, 1 failed, in 3.20 seconds", True), (TAP_FAILING, True),
    ("# pass 9\n# fail 0", False), ("Tests:       1 failed, 3 passed, 4 total", True),
    ("  3 passing (20ms)\n  1 failing", True), ("--- FAIL: TestAdd (0.00s)", True),
    ("ok  \tacme/x\t0.012s", False), ("test result: FAILED. 1 passed; 1 failed", True),
    ("test result: ok. 3 passed; 0 failed", False), ("5 examples, 1 failure", True),
    ("Tests run: 4, Failures: 0, Errors: 1", True), ("[INFO] BUILD SUCCESS", False),
    ("8 runs, 8 assertions, 1 failures, 0 errors, 0 skips", True),
    ("8 runs, 8 assertions, 0 failures, 2 errors, 0 skips", True),
    ("Finished in 0.01s\n5 runs, 9 assertions, 0 failures, 0 errors, 1 skips", False),
    ("1 tests, 1 assertions, 0 failures, 0 errors, 0 pendings, 0 omissions", False),
    ("+    raise ValueError('no')", None), ("ok 1 - add", None)])
def test_a_runners_summary_gives_its_verdict(out, failed):
    assert turn.runner_verdict(out) is failed


@pytest.mark.parametrize("cmd", ["tar -xOf pkg.tgz pkg/x.py | sed -n 1,80p",
                                 "cat tests/test_x.py | head -40", "grep -rn pytest src | head",
                                 "python -m pytest x.py --collect-only -q | rg test_",
                                 "bin/test --help | head", "python repro.py 2>&1 | tail",
                                 "pytest -q; git diff --stat", "pytest -q\npython - <<'PY'\nx\nPY"])
def test_any_other_command_keeps_its_exit_status(cmd):
    """Piped reads of source, and a test run among other commands with no runner's summary in
    the result, keep the status the agent reported: `assert` and `Error` in such output are no
    verdict."""
    g = guard.TurnGuard(turn_key="t")
    g.fold("bash", {"command": cmd}, "def f():\n    assert x\n    raise ValueError('no')\n", 0)
    assert g.whole.error_outputs == 0


@pytest.mark.parametrize("out, failing", [
    (TAP_FAILING, True), ("# pass 9\n# fail 0", False), ("not ok 3 - rates", True),
    ("  2 failing", True), ("5 examples, 0 failures", False), ("5 examples, 2 failures", True),
    ("Tests run: 4, Failures: 1, Errors: 0", True), ("Tests run: 4, Failures: 0, Errors: 0", False),
    ("--- FAIL: TestAdd (0.00s)", True), ("ok  \tacme/x\t0.01s", False)])
def test_other_ecosystems_failing_test_summaries_count_as_failures(out, failing):
    g = guard.TurnGuard(turn_key="t")
    g.fold("bash", {"command": "make test"}, out, None)
    assert (g.whole.error_outputs == 1) is failing


def test_a_unittest_failure_is_known_by_its_traceback():
    """unittest's `FAIL: test_x (...)` is not Go's `FAIL`: a Django failure is known by its
    traceback, not its test name, so the same failure in another test still repeats."""
    from tokeneconomizer.loopguard.state import _failure_sig
    out = "FAIL: test_x (validators.tests.T)\nTraceback (most recent call last):\n  ..."
    assert _failure_sig(out) == _failure_sig("Traceback (most recent call last):")
