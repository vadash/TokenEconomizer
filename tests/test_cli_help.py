"""`te help`: the user-facing subcommands, one line each, and bare `te` printing the same list.

The list leaves out the plumbing a person never types (`fold`, `judge`, `probe`), and every name
in it is a subcommand the parser accepts, so the help cannot drift from the command line.
"""
from tokeneconomizer.plugin import cli

EXPECTED = """\
status \u2014 what TokenEconomizer learned here and did
microskills \u2014 the facts it injects
toolprune [show|apply|restore] \u2014 the proposed deny list
config [show|set|unset] \u2014 turn mechanisms on/off
repos [list|migrate|merge] \u2014 every repository with state here
history [status|import] \u2014 sessions from before the plugin
repomap [show|refresh] \u2014 the cached repository map
doctor \u2014 check the installation
"""


def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENECONOMIZER_HOME", str(tmp_path / "home"))
    work = tmp_path / "work"
    work.mkdir()
    return ["--cwd", str(work), "--host", "omp"]


def test_help_prints_one_line_per_user_facing_subcommand(tmp_path, monkeypatch, capsys):
    argv = _home(tmp_path, monkeypatch)
    assert cli.main([*argv, "help"]) == 0
    assert capsys.readouterr().out == EXPECTED


def test_bare_te_prints_the_same_list_as_te_help(tmp_path, monkeypatch, capsys):
    argv = _home(tmp_path, monkeypatch)
    assert cli.main([*argv, "help"]) == 0
    helped = capsys.readouterr().out
    for bare in ([], argv):
        assert cli.main(bare) == 0
        assert capsys.readouterr().out == helped


def test_the_list_names_only_subcommands_te_accepts(tmp_path, monkeypatch, capsys):
    argv = _home(tmp_path, monkeypatch)
    assert cli.main([*argv, "help"]) == 0
    listed = {line.split()[0] for line in capsys.readouterr().out.splitlines()}
    choices = set(cli.build_parser()._subparsers._group_actions[0].choices)
    assert listed <= choices
    assert not listed & {"fold", "judge", "probe"}
