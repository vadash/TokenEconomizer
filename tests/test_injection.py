"""Injection: the distiller lifecycle + the PrefixStage render/after_task cycle."""
import os

from tokeneconomizer.microskills import Distiller, PrefixStage
from tokeneconomizer.config import InjectionConfig
from fixtures import write_omp_session


def _facts_runner(payload):
    """An optimizer runner returning a fixed proposer payload + 5 eff tokens."""
    return lambda prompt, model="agent": (payload, 5.0)


def test_distiller_empty_render_before_formation(tmp_path):
    d = Distiller("acme/widget", runner=_facts_runner('{"extractors":[],"facts":[]}'))
    tr = str(tmp_path / "t.jsonl")
    write_omp_session(tr, [("bash", {"command": "pytest"}, "ok")])
    d.update(tr, iid="widget-1", resolved=True)
    assert d.render() == ""          # nothing has fired/formed yet
    assert d.now == 1


def test_distiller_persist_roundtrip(tmp_path):
    root = str(tmp_path)
    d = Distiller("acme/widget", runner=_facts_runner('{"extractors":[],"facts":[]}'))
    tr = str(tmp_path / "t.jsonl")
    write_omp_session(tr, [("bash", {"command": "pytest"}, "ok")])
    d.update(tr, iid="w-1", resolved=True)
    d.save(root)
    d2 = Distiller.load("acme/widget", root)
    assert d2.now == d.now


def test_distiller_grounds_and_fires_a_fact(tmp_path):
    # A proposer fact whose confirm regex matches a repeated pytest form; after enough
    # drifted confirmations it should promote and (once past the firing gate) inject.
    payload = ('{"extractors":[],"facts":[{"id":"use-pytest","claim":"pytest is canonical",'
               '"injection":"run tests with pytest","high_stakes":true,'
               '"detector":{"scope":"ran_tests","confirm":"passed","fired":"ran_tests"},'
               '"cite":["w-0","w-1"]}]}')
    d = Distiller("acme/widget", runner=_facts_runner(payload))
    d.last_propose = -1
    # feed several tasks that each run pytest and pass, on disjoint edited files
    import tokeneconomizer.microskills.distiller as inj
    inj.PROPOSE_EVERY = 1
    for i in range(8):
        tr = str(tmp_path / f"t{i}.jsonl")
        write_omp_session(tr, [
            ("edit", {"file_path": f"pkg/mod{i}.py"}, "ok"),
            ("bash", {"command": "python -m pytest tests/test_x.py"}, "1 passed")])
        d.update(tr, iid=f"w-{i}", resolved=True)
    assert "use-pytest" in d.facts
    # at least reached ACTIVE or accrued firing evidence
    f = d.facts["use-pytest"]
    assert f.life.fire_den >= 1


def test_prefix_stage_after_task_reports_tokens(tmp_path):
    root = str(tmp_path)
    cfg = InjectionConfig(enabled=True, propose_every=5)
    stage = PrefixStage(cfg, "acme/widget", root, fresh=True,
                        runner=_facts_runner('{"extractors":[],"facts":[]}'),
                        agent="omp")
    rd = str(tmp_path / "run"); os.makedirs(rd)
    write_omp_session(os.path.join(rd, "transcript.jsonl"),
                            [("bash", {"command": "pytest"}, "ok")])
    next_doc, outcome = stage.fold_session(None, rd, "w-1", {})
    assert next_doc["reason"] == "manual-distill"
    assert next_doc["facts"]
    assert set(outcome) == {"optimizer_tokens", "rendered_chars"}


def test_microskills_progress_counts_only_sessions_folded_while_it_was_on(
        tmp_path, monkeypatch, capsys):
    import json

    from tokeneconomizer.plugin import cli, hosts
    from tokeneconomizer.plugin.config import load_config
    from tokeneconomizer.plugin.context import LiveContext

    repo = tmp_path / "repo"
    repo.mkdir()
    env = {"TOKENECONOMIZER_HOME": str(tmp_path / "home"),
           "GIT_CEILING_DIRECTORIES": str(tmp_path)}
    monkeypatch.delenv("TE_MICROSKILLS", raising=False)
    ctx = LiveContext.build(hosts.Omp(), str(repo), env, settle=False)
    ctx.store.ensure()
    repo_dir = os.path.dirname(ctx.store.base)
    with open(os.path.join(repo_dir, "config.json"), "w", encoding="utf-8") as fh:
        json.dump({"microskills": "on"}, fh)
    ctx.config = load_config(ctx.root, repo_dir=repo_dir, env=env)
    ctx.store.write("injection.json", {"sessions": 5, "imported_sessions": 2, "rendered": ""})
    monkeypatch.setattr(cli, "_ctx", lambda args: ctx)

    assert cli.main(["--host", "omp", "--cwd", str(repo), "microskills"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0] == (
        "micro-skills is on here (repo config: envfacts+distiller; default off)")
    assert "3/4 sessions toward firing gate" in out
    assert "fired in 40% of at least 4 sessions" in out
    assert "(nothing injected yet: no fact has cleared its firing gate)" in out
