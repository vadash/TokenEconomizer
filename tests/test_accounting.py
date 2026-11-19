"""accounting.session_fragment: the session-doc to ledger-record mapping."""
from tokeneconomizer.plugin import accounting


def test_session_fragment_worked_example():
    sess = {"turns": [{}, {}, {}], "nudges": 2, "aborts": 1, "injected_chars": 50,
            "judge_tokens": 4.0, "writer_tokens": 6.0}
    assert accounting.session_fragment(sess, {"optimizer_tokens": 7.0}, ["WebFetch"]) == {
        "turns": 3, "nudges": 2, "aborts": 1, "injected_chars": 50,
        "untool_applied": ["WebFetch"],
        "overhead": {"optimizer_tokens": 7.0, "spiral_judge_tokens": 4.0,
                     "spiral_nudge_writer_tokens": 6.0},
    }


def test_session_fragment_missing_keys_default_to_zero():
    assert accounting.session_fragment({}, None, None) == {
        "turns": 0, "nudges": 0, "aborts": 0, "injected_chars": 0,
        "untool_applied": [],
        "overhead": {"optimizer_tokens": 0, "spiral_judge_tokens": 0.0,
                     "spiral_nudge_writer_tokens": 0.0},
    }
