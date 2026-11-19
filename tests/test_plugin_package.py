"""The installable omp-only plugin: launchers, the self-contained bundle, and the layering
the README promises.
"""
import json
import os
import re
import shutil
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN = os.path.join(REPO, "plugin")


def _json(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as fh:
        return json.load(fh)


def test_the_plugin_ships_only_the_omp_surface():
    """Only the omp extension, the Windows launchers and the core link ship; omp needs no
    hook files (its extension calls `te-hook --host omp` itself)."""
    assert set(os.listdir(PLUGIN)) == {"README.md", "hooks", "lib", "omp", "package.json",
                                       "scripts"}
    assert not [n for n in os.listdir(os.path.join(PLUGIN, "hooks")) if n.endswith(".json")]
    assert os.path.isfile(os.path.join(PLUGIN, "omp", "tokeneconomizer.ts"))
    for name in ("te_bootstrap.py", "te.cmd", "te-hook.cmd"):
        assert os.path.isfile(os.path.join(PLUGIN, "scripts", name)), name
    for gone in ("te", "te-hook"):
        assert not os.path.exists(os.path.join(PLUGIN, "scripts", gone)), gone
    assert os.path.exists(os.path.join(PLUGIN, "lib", "tokeneconomizer"))
    assert not os.path.exists(os.path.join(PLUGIN, "slash-commands"))
def _run_hook(launcher, payload, env, cwd=None):
    p = subprocess.run(["cmd.exe", "/c", launcher], input=json.dumps(payload),
                       capture_output=True, text=True, env=env, timeout=60, cwd=cwd or os.getcwd())
    return p.returncode, p.stdout, p.stderr


# What every Windows process inherits, whatever runs it: the launchers know Windows by `OS`, and
# Python and Git need the rest to start.
WINDOWS_ENV = ("OS", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP")


def _hook_env(tmp_path, **extra):
    env = {k: os.environ[k] for k in WINDOWS_ENV if os.name == "nt" and k in os.environ}
    env.update({"PATH": os.environ.get("PATH", ""), "TOKENECONOMIZER_HOME": str(tmp_path / "home"),
                "GIT_CEILING_DIRECTORIES": str(tmp_path), "TE_FOLD_SYNC": "1"})
    env.update(extra)
    return env


def test_the_launcher_runs_the_dispatcher_from_the_checkout(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    code, out, _ = _run_hook(os.path.join(PLUGIN, "scripts", "te-hook.cmd"),
                             {"hook_event_name": "SessionStart", "session_id": "s",
                              "cwd": str(work), "source": "startup"}, _hook_env(tmp_path))
    assert code == 0 and json.loads(out) == {"omp": {"denyTools": []}}
    assert os.path.isdir(tmp_path / "home" / "repos")      # it ran, and recorded the session


@pytest.mark.skipif(os.name != "nt", reason="the Windows .cmd launcher")
def test_the_cmd_hook_launcher_fails_open_without_python(tmp_path):
    """`te-hook.cmd` with no Python anywhere answers no change: a hook never fails the agent."""
    env = {k: os.environ[k] for k in ("SYSTEMROOT", "COMSPEC", "PATHEXT", "TEMP", "TMP", "OS")
           if k in os.environ}
    env["PATH"] = ""
    work = tmp_path / "work"
    work.mkdir()
    p = subprocess.run(["cmd.exe", "/c", os.path.join(PLUGIN, "scripts", "te-hook.cmd")],
                       input="{}", capture_output=True, text=True, env=env, cwd=str(work),
                       timeout=60)
    assert p.returncode == 0 and p.stdout.strip() == "{}"


def test_the_bootstrap_answers_no_change_on_an_old_python(tmp_path, monkeypatch):
    sys.path.insert(0, os.path.join(PLUGIN, "scripts"))
    try:
        import te_bootstrap
    finally:
        sys.path.pop(0)
    monkeypatch.setattr(te_bootstrap, "MIN_VERSION", (99, 0))
    monkeypatch.setattr(sys, "argv", ["te_bootstrap.py", "hook"])
    import io
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    assert te_bootstrap.main() == 0 and out.getvalue().strip() == "{}"


def test_the_bundle_runs_without_a_checkout(tmp_path):
    out = tmp_path / "market"
    subprocess.run([sys.executable, os.path.join(REPO, "scripts", "build_plugin.py"),
                    "--out", str(out)], check=True, capture_output=True, stdin=subprocess.DEVNULL)
    bundle = out / "tokeneconomizer"
    assert (bundle / "lib" / "tokeneconomizer" / "plugin" / "hook.py").exists()
    assert (bundle / "lib" / "tokeneconomizer" / "__init__.py").exists()
    assert (bundle / "omp" / "tokeneconomizer.ts").exists()       # the omp extension ships
    assert not [p for p in bundle.iterdir() if p.name.startswith(".")]  # no hidden manifests ship
    assert not (bundle / "lib" / "te_harness").exists()
    assert not (bundle / "lib" / "te_bench").exists()

    work = tmp_path / "work"
    work.mkdir()
    # Run the bundled launcher with the core reachable ONLY through the bundle's lib/: the
    # working folder is empty and the checkout is on no path the launcher reads.
    code, stdout, stderr = _run_hook(str(bundle / "scripts" / "te-hook.cmd"),
                                     {"hook_event_name": "SessionStart", "session_id": "s",
                                      "cwd": str(work), "source": "startup"},
                                     _hook_env(tmp_path, PYTHONNOUSERSITE="1"), cwd=str(work))
    assert code == 0 and json.loads(stdout) == {"omp": {"denyTools": []}}, stderr
    assert os.path.isdir(tmp_path / "home" / "repos")      # it ran, and recorded the session
    sys.path.insert(0, str(bundle / "scripts"))
    try:
        import importlib
        import te_bootstrap
        importlib.reload(te_bootstrap)
        assert te_bootstrap.core_path() == str(bundle / "lib")
    finally:
        sys.path.pop(0)


# ---- the layering the README promises ------------------------------------------------------
MECHANISMS = ("microskills", "toolprune", "loopguard", "trace")
CORE_MODULES = ("config.py", "domain.py", "ledger.py", "pricing.py", "stage.py")


def _imports(path):
    """(module, level) of every import statement in a file."""
    import ast
    tree = ast.parse(open(path, encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                yield a.name, 0
        elif isinstance(node, ast.ImportFrom):
            yield node.module or "", node.level
            if node.level and not node.module:          # `from .. import plugin`
                for a in node.names:
                    yield a.name, node.level


def _core_files():
    pkg = os.path.join(REPO, "tokeneconomizer")
    for name in CORE_MODULES + ("__init__.py",):
        yield os.path.join(pkg, name), 0
    for sub in MECHANISMS:
        for name in sorted(os.listdir(os.path.join(pkg, sub))):
            if name.endswith(".py"):
                yield os.path.join(pkg, sub, name), 1


def test_the_mechanisms_never_import_the_plugin_runtime():
    """The plugin runs the mechanisms; the mechanisms never know it exists. Checked on the source
    (no import names `plugin`, absolutely or relatively) and on a live import."""
    for path, depth in _core_files():
        for mod, level in _imports(path):
            if level == 0:
                assert not mod.startswith("tokeneconomizer.plugin"), f"{path} imports {mod}"
            else:
                # `from ..plugin import x` inside a subpackage, `from .plugin` at the top level
                parts = mod.split(".")
                reaches_top = level == depth + 1
                assert not (reaches_top and parts[0] == "plugin"), f"{path} imports .{mod}"
    probe = ("import sys, tokeneconomizer, tokeneconomizer.domain, tokeneconomizer.stage\n"
             + "".join(f"import tokeneconomizer.{m}\n" for m in MECHANISMS)
             + "print(sorted(m for m in sys.modules if m.startswith('tokeneconomizer.plugin') "
               "or m.split('.')[0] in ('te_bench', 'te_harness', 'te_live')))")
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, cwd=REPO,
                         stdin=subprocess.DEVNULL)
    assert out.stdout.strip() == "[]", out.stdout + out.stderr


def test_the_plugin_ships_and_loads_no_benchmark_code():
    """The benchmark adapters, the facade, the workloads, the baselines and the benchmark-only
    controllers live outside the shipped package. The plugin's library is the
    package alone, and running every part of the plugin loads none of them."""
    shipped = os.path.join(REPO, "tokeneconomizer")       # what lib/ carries
    assert sorted(n for n in os.listdir(shipped)
                  if os.path.isdir(os.path.join(shipped, n)) and n != "__pycache__") == sorted(
        MECHANISMS + ("plugin",))
    for gone in ("adapters", "mechanisms", "vendor", "facade.py", "workloads.py", "runner.py",
                 "arms.py", "baselines"):
        assert not os.path.exists(os.path.join(shipped, gone)), gone
    mods = ("hook", "controller", "turnsession", "fold", "cli", "denylist", "repomap",
            "sidecall", "judge", "adapter")
    probe = ("import sys\n" + "".join(f"import tokeneconomizer.plugin.{m}\n" for m in mods)
             + "print(sorted({m.split('.')[0] for m in sys.modules} "
               "& {'te_bench', 'te_harness', 'te_live'}))")
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, cwd=REPO,
                         stdin=subprocess.DEVNULL)
    assert out.stdout.strip() == "[]", out.stdout + out.stderr


def test_the_package_is_stdlib_only_and_never_touches_the_benchmark_harness():
    pkg = os.path.join(REPO, "tokeneconomizer")
    for top, _, files in os.walk(pkg):
        for name in sorted(files):
            # the repository-map generator runs as a subprocess under a Python that has aider;
            # nothing imports it
            if not name.endswith(".py") or name == "repomap_gen.py":
                continue
            path = os.path.join(top, name)
            for mod, level in _imports(path):
                if level:
                    continue
                root = mod.split(".")[0]
                assert root not in ("te_harness", "te_bench", "te_live", "docker"), \
                    f"{path} imports {mod}"
                # tomllib: standard library from 3.11, imported behind an ImportError guard
                assert root in sys.stdlib_module_names or root in (
                    "tokeneconomizer", "tomllib"), f"{path} imports non-stdlib {mod}"


def test_the_omp_manifest_names_its_own_extension():
    """oh-my-pi has no plugin directory: its manifest names a TypeScript extension that speaks
    the dispatcher's protocol. The root package and the plugin directory must name the same
    file, and that file must carry the levers omp exposes (tool deny list, system prompt, steer
    message, slash command) and only the six events the host contract uses."""
    pkg = _json(PLUGIN, "package.json")
    root = _json(os.path.dirname(PLUGIN), "package.json")
    (plugin_ext,) = pkg["omp"]["extensions"]
    (root_ext,) = root["omp"]["extensions"]
    path = os.path.join(PLUGIN, plugin_ext)
    assert os.path.realpath(os.path.join(os.path.dirname(PLUGIN), root_ext)) == \
        os.path.realpath(path)
    assert os.path.isfile(path)
    src = open(path, encoding="utf-8").read()
    assert 'import type { ExtensionAPI, ExtensionContext } from "@oh-my-pi/pi-coding-agent";' in src
    assert '"te-hook", ["--host", "omp"]' in src
    assert "setActiveTools" in src and "denyTools" in src
    # omp exposes the agent identity on the context, not the API object; using `api.agent`
    # threw "undefined is not an object" on every event in the live smoke.
    assert "ctx.agent.kind" in src and "api.agent" not in src
    assert 'registerCommand("te"' in src
    assert set(re.findall(r'\.on\("(\w+)"', src)) == {
        "session_start", "session_switch", "before_agent_start", "tool_result", "agent_end",
        "session_shutdown"}


@pytest.mark.skipif(not os.path.exists(os.path.join(PLUGIN, "lib", "tokeneconomizer")),
                    reason="core not linked into plugin/lib on this checkout")
def test_the_plugin_carries_the_core_an_install_copy_follows(tmp_path):
    """An install copies plugin/ into its cache, even from a local checkout, so the core must
    be reachable from inside plugin/ (as a symlink, or as the stub file Windows writes)."""
    import shutil
    assert os.listdir(os.path.join(PLUGIN, "lib")) == ["tokeneconomizer"]
    link = os.path.join(PLUGIN, "lib", "tokeneconomizer")
    if os.path.islink(link):
        # Windows reports the target with its own separator
        assert os.readlink(link).replace(os.sep, "/") == "../../tokeneconomizer"
    else:
        assert open(link, encoding="utf-8").read().strip() == "../../tokeneconomizer"
    assert os.path.isfile(os.path.join(REPO, "tokeneconomizer", "plugin", "hook.py"))
    # What an install does: copy the directory, following links. The copy runs on its own,
    # through the bootstrap directly (no shell, so no agent's `sh` path handling is involved).
    copy = str(tmp_path / "cache" / "tokeneconomizer" / "0.1.0")
    shutil.copytree(PLUGIN, copy, symlinks=False,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    env = dict(os.environ, TOKENECONOMIZER_HOME=str(tmp_path / "h"), PYTHONNOUSERSITE="1")
    env.pop("PYTHONPATH", None)
    env.pop("TE_CORE_PATH", None)
    env.pop("TE_PYTHON", None)
    run = [sys.executable, os.path.join(copy, "scripts", "te_bootstrap.py"), "cli", "doctor"]
    if os.path.islink(link):
        out = subprocess.run(run, capture_output=True, text=True, env=env, timeout=60,
                             stdin=subprocess.DEVNULL, cwd=str(tmp_path))
        assert out.returncode == 0 and "python" in out.stdout, out.stderr[-500:]
    else:
        # Windows without symlinks checks the link target out as a stub file: the copy names
        # the checkout, not a package, so it runs with the checkout named explicitly, and says
        # what is missing without it.
        env["TE_CORE_PATH"] = REPO
        out = subprocess.run(run, capture_output=True, text=True, env=env, timeout=60,
                             stdin=subprocess.DEVNULL, cwd=str(tmp_path))
        assert out.returncode == 0 and "python" in out.stdout, out.stderr[-500:]
        del env["TE_CORE_PATH"]
        out = subprocess.run(run, capture_output=True, text=True, env=env, timeout=60,
                             stdin=subprocess.DEVNULL, cwd=str(tmp_path))
        assert out.returncode == 1 and "was not found" in out.stderr


def test_the_commands_keep_their_earlier_names(tmp_path, monkeypatch, capsys):
    """`te facts` / `te untool` were the names before the mechanisms were renamed; they run
    exactly what `microskills` / `toolprune` run."""
    from tokeneconomizer.plugin import cli
    monkeypatch.setenv("TOKENECONOMIZER_HOME", str(tmp_path / "home"))
    work = tmp_path / "work"
    work.mkdir()
    for new, old in (("microskills", "facts"), ("toolprune", "untool")):
        outs = []
        for name in (new, old):
            assert cli.main(["--cwd", str(work), "--host", "omp", name]) == 0
            outs.append(capsys.readouterr().out)
        assert outs[0] == outs[1] and outs[0]


def test_toolprune_note_after_a_reported_catalog(tmp_path, monkeypatch, capsys):
    """Until a session reports omp's tool catalog, the note says only observed tools are known;
    once one is recorded, the note says it joins the evidence from the next session."""
    from tokeneconomizer.plugin import cli
    home = tmp_path / "home"
    monkeypatch.setenv("TOKENECONOMIZER_HOME", str(home))
    work = tmp_path / "work"
    work.mkdir()
    assert cli.main(["--cwd", str(work), "--host", "omp", "toolprune"]) == 0
    assert "omp reports its tool catalog at each SessionStart" in capsys.readouterr().out
    (home / "hosts" / "omp").mkdir(parents=True)
    (home / "hosts" / "omp" / "catalog.json").write_text("{}", encoding="utf-8")
    assert cli.main(["--cwd", str(work), "--host", "omp", "toolprune"]) == 0
    out = capsys.readouterr().out
    assert "joins this repository's evidence from its next session" in " ".join(out.split())


def _shipped_python():
    for top in ("tokeneconomizer", os.path.join("plugin", "scripts"), "scripts"):
        for root, _dirs, files in os.walk(os.path.join(REPO, top)):
            for name in sorted(files):
                if name.endswith(".py"):
                    yield os.path.join(root, name)


def test_text_io_names_its_encoding():
    """The agents' payloads, session logs and output are UTF-8 whatever the locale, and Python
    decodes a file or a pipe opened without an encoding with the locale's code page -- cp1251 on
    a Russian Windows, cp1252 on a US one -- which garbles a non-ASCII path and stops a fold at
    the first undecodable byte. So every text-mode `open()`, `read_text`/`write_text` and text
    subprocess call in the shipped code names its encoding."""
    import ast
    missing = []
    for path in _shipped_python():
        for call in ast.walk(ast.parse(open(path, encoding="utf-8").read())):
            if not isinstance(call, ast.Call):
                continue
            f, kw = call.func, {k.arg: k.value for k in call.keywords}
            if isinstance(f, ast.Name) and f.id == "open":
                mode = call.args[1] if len(call.args) > 1 else kw.get("mode")
                text = not (isinstance(mode, ast.Constant) and "b" in mode.value)
            elif isinstance(f, ast.Attribute) and f.attr in ("read_text", "write_text"):
                text = True
            elif (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name)
                  and f.value.id == "subprocess"):
                text = "text" in kw or "universal_newlines" in kw
            else:
                continue
            if text and "encoding" not in kw:
                missing.append(f"{os.path.relpath(path, REPO)}:{call.lineno}")
    assert not missing, missing
