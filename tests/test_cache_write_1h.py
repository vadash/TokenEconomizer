"""A cache write is billed by its lifetime: Anthropic charges 1.25x input for the 5-minute cache
and 2x for the 1-hour one, and a message's usage says how its cache writes split between them
(`cache_creation: {ephemeral_5m_input_tokens, ephemeral_1h_input_tokens}`). A session that
writes to the 1-hour cache needs the split priced, or pricing every cache write at 1.25x
understates it. A cache hit is billed at 0.1x input except on a few models (0.05x on Opus 5.5 and
Sonnet 5.5, 0.025x on Fable and Mythos 5.1 and 5.2), and hits are most of a session's tokens."""
import json

import pytest

from tokeneconomizer import pricing, trace
from tokeneconomizer.plugin import accounting


def _row(cache_write=1000, one_hour=None, model="claude-opus-4-8"):
    row = {"input_tokens": 100, "cache_read_input_tokens": 10_000,
           "cache_creation_input_tokens": cache_write, "output_tokens": 10, "model": model}
    if one_hour is not None:
        row["cache_creation_1h_input_tokens"] = one_hour
    return row


BASE = 100 * 1.0 + 10_000 * 0.10 + 10 * 5.0          # everything but the cache writes


# ---- pricing ---------------------------------------------------------------------------------
def test_one_hour_cache_writes_are_priced_at_twice_input():
    assert pricing.effective_tokens(_row(1000, one_hour=1000)) == BASE + 1000 * 2.0


def test_a_mixed_row_prices_each_share_at_its_own_rate():
    assert pricing.effective_tokens(_row(1000, one_hour=400)) == BASE + 600 * 1.25 + 400 * 2.0


def test_a_row_without_the_split_is_priced_as_before():
    assert pricing.effective_tokens(_row(1000)) == BASE + 1000 * 1.25
    assert pricing.effective_tokens(_row(1000, one_hour=0)) == BASE + 1000 * 1.25


def test_a_claude_id_outside_the_table_gets_the_one_hour_rate_too():
    row = _row(1000, one_hour=1000, model="claude-opus-4-5")
    assert pricing.effective_tokens(row) == BASE + 1000 * 2.0


def test_the_one_hour_share_is_never_more_than_the_cache_writes():
    assert pricing.cache_write_1h_tokens(_row(1000, one_hour=5000)) == 1000
    assert pricing.cache_write_1h_tokens(_row(1000, one_hour=-5)) == 0
    assert pricing.cache_write_1h_tokens(_row(1000, one_hour="x")) == 0
    assert pricing.cache_write_1h_tokens(None) == 0


def test_a_weight_table_without_a_one_hour_rate_uses_its_cache_write_rate():
    # gpt-5.6-sol has no 1-hour entry: its cache writes stay at 1.25x whatever the row says.
    row = _row(1000, one_hour=1000, model="gpt-5.6-sol")
    assert pricing.effective_tokens(row) == 100 + 10_000 * 0.10 + 1000 * 1.25 + 10 * 6.0
    explicit = dict(input=1.0, cache_read=0.0, cache_write=1.0, output=1.0)
    assert pricing.effective_tokens(_row(1000, one_hour=1000), weights=explicit) == 100 + 1000 + 10


def test_the_split_is_read_from_an_anthropic_usage_object():
    usage = {"cache_creation_input_tokens": 248,
             "cache_creation": {"ephemeral_5m_input_tokens": 148, "ephemeral_1h_input_tokens": 100}}
    assert pricing.cache_write_1h_tokens(usage) == 100
    assert pricing.cache_write_1h_tokens({"cache_creation_input_tokens": 248,
                                          "cache_creation": "not an object"}) == 0


# ---- omp session files --------------------------------------------------------------------
# omp's usage names the 1-hour share of `cacheWrite` as `cacheWrite1h` (Anthropic only).
def _usage(cache_write, one_hour):
    return {"input": 10, "output": 50, "cacheRead": 1000, "cacheWrite": cache_write,
            "cacheWrite1h": one_hour, "totalTokens": 1060 + cache_write,
            "cost": {"total": 0.001}}


def _transcript(path, entries):
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    return str(path)


def _omp_session(path, usages, model="claude-opus-4-8"):
    """An omp session tree whose assistant messages carry `usages`."""
    lines = [{"type": "session", "version": 3, "id": "omp-1", "cwd": "/w/repo"},
             {"type": "message", "id": "e1", "parentId": None,
              "message": {"role": "user", "content": "fix the bug"}}]
    for i, usage in enumerate(usages, start=2):
        lines.append({"type": "message", "id": f"e{i}", "parentId": f"e{i - 1}", "message": {
            "role": "assistant", "provider": "anthropic", "model": model,
            "usage": usage, "content": [{"type": "text", "text": "ok"}]}})
    return _transcript(path, lines)


def test_an_omp_session_reports_its_one_hour_cache_writes(tmp_path):
    p = _omp_session(tmp_path / "s.jsonl", [_usage(1000, 1000), _usage(200, 50)])
    u = trace.omp_session_usage(p)
    assert u["cache_creation_input_tokens"] == 1200
    assert u["cache_creation_1h_input_tokens"] == 1050
    assert accounting.effective(u) == (
        20 + 2000 * 0.10 + 150 * 1.25 + 1050 * 2.0 + 100 * 5.0, "model")


# ---- the ledger ------------------------------------------------------------------------------
def test_a_session_is_priced_with_its_one_hour_cache_writes(tmp_path):
    p = _omp_session(tmp_path / "s.jsonl", [_usage(1000, 1000)])
    eff, priced_by = accounting.effective(trace.omp_session_usage(p))
    assert priced_by == "model"
    assert eff == 10 + 1000 * 0.10 + 1000 * 2.0 + 50 * 5.0

def test_a_side_call_is_priced_from_the_usage_object_it_returns():
    usage = {"input_tokens": 10, "cache_read_input_tokens": 0, "output_tokens": 0,
             "cache_creation_input_tokens": 1000, "model": "claude-haiku-4-5",
             "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 1000}}
    assert accounting.effective(usage) == (10 + 1000 * 2.0, "model")


def test_an_omp_side_call_is_priced_with_its_one_hour_cache_writes():
    run = [{"type": "message_end", "message": {
        "role": "assistant", "model": "claude-haiku-4-5", "usage": _usage(1000, 1000),
        "content": [{"type": "text", "text": "- use python -m pytest"}]}}]
    _, usage = trace.omp_json_reply("\n".join(json.dumps(e) for e in run))
    usage.pop("cost_usd")
    assert accounting.effective(usage) == (10 + 1000 * 0.10 + 1000 * 2.0 + 50 * 5.0, "model")


# ---- cache hits ------------------------------------------------------------------------------
@pytest.mark.parametrize("model, ratio", [
    ("claude-opus-5-5", 0.05), ("claude-sonnet-5-5", 0.05),
    ("claude-fable-5-1", 0.025), ("claude-fable-5-2", 0.025),
    ("claude-mythos-5-1", 0.025), ("claude-mythos-5-2", 0.025),
    ("claude-sonnet-5.5", 0.05),                                # a router's dotted spelling
    ("anthropic/claude-opus-5.5", 0.05),                        # and its provider prefix
    ("claude-opus-5-5-20261001", 0.05),                         # a dated snapshot
])
def test_a_cache_hit_is_billed_at_the_models_own_fraction(model, ratio):
    assert pricing.weights_for(_row(model=model))["cache_read"] == ratio
    cache_hits = 10_000 * ratio
    assert pricing.effective_tokens(_row(0, model=model)) == 100 + cache_hits + 10 * 5.0


@pytest.mark.parametrize("model", [
    "claude-opus-5", "claude-opus-4-8", "claude-sonnet-5", "claude-sonnet-4-6",
    "claude-haiku-5-5", "claude-haiku-4-5", "claude-fable-5", "claude-mythos-5",
    "claude-opus-4.5",
])
def test_the_other_claude_models_keep_the_usual_cache_hit_fraction(model):
    assert pricing.weights_for(_row(model=model))["cache_read"] == 0.10
    assert pricing.effective_tokens(_row(0, model=model)) == BASE


def test_a_session_on_opus_5_5_is_priced_with_its_cheaper_cache_hits(tmp_path):
    p = _omp_session(tmp_path / "s.jsonl", [_usage(0, 0)], model="claude-opus-5-5")
    eff, priced_by = accounting.effective(trace.omp_session_usage(p))
    assert priced_by == "model"
    assert eff == 10 + 1000 * 0.05 + 50 * 5.0
