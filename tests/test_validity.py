"""Eval-integrity gate tests.

The load-bearing one is `test_volitional_stop_is_valid_not_truncation`: it pins
the correction that motivated this whole module. Read `metrics/validity.py`'s
docstring before changing any threshold here.

All offline — no ROM, no emulator, no network, no key.
"""

from pathlib import Path

from pokebench.metrics.score import score_run
from pokebench.metrics.validity import check_validity, partition
from pokebench.replay import Run


def _rec(turn, *, out_tokens=100, tool=True, x=1, y=1, hashv=None):
    return {
        "turn": turn,
        "usage": {"input_tokens": 1000, "output_tokens": out_tokens},
        "cost_usd": 0.01,
        "tool_call": {"name": "press_buttons"} if tool else None,
        "action": {"executed": ["up"], "invalid": [], "is_noop": False} if tool else None,
        "observation_hash": hashv or f"h{turn}",
        "state": {"map_name": "REDS_HOUSE_2F", "map_id": 38, "x": x, "y": y, "facing": "up"},
        "success": False,
    }


def _run(records, *, summary=True, wall=300.0, stop="max_turns", meta=None):
    return Run(
        dir=Path("x"),
        meta=meta if meta is not None else {"scenario": "s1", "model": "sonnet", "tier": 0,
                                            "caps": {"max_turns": 100}},
        records=records,
        summary={"success": False, "stop_reason": stop, "wall_seconds": wall} if summary else None,
    )


def _check(run, **kw):
    return check_validity(run, score_run(run), **kw)


# --- The correction this module exists for --------------------------------


def test_volitional_stop_is_valid_not_truncation():
    """Sonnet S1 seed1: 16% of turns emitted no tool call, but max output was
    384 against a 1024 ceiling — it stopped because it believed it had reached
    Route 1, not because the harness clipped it.

    The obvious predicate ('>10% dead turns => invalid') would have deleted a
    legitimate seed and pushed the sweep's worst row lower still. That is a
    silent corruption of the published table, which is exactly the defect class
    this project is about. Dead turns alone must NEVER invalidate a run.
    """
    records = [_rec(i, out_tokens=384, tool=(i % 6 != 0)) for i in range(1, 101)]
    v = _check(_run(records))
    assert v.valid, f"a volitional stop must stay in the table, got {v.reason}: {v.detail}"
    # ...and it must not pass silently — it is a finding worth reading.
    assert "volitional_stop" in v.evidence
    assert v.evidence["no_tool_call_rate"] > 0.10
    assert v.evidence["ceiling_hit_rate"] == 0.0


def test_truncation_is_invalid_when_both_rates_fire():
    """Sonnet-with-thinking: dead turns AND ceiling hits together. This measures
    the 1024-token ceiling, not the model, and must not reach results.json."""
    records = [_rec(i, out_tokens=1024, tool=(i % 3 != 0)) for i in range(1, 101)]
    v = _check(_run(records))
    assert not v.valid and v.reason == "output_ceiling_truncation"
    assert v.evidence["no_tool_call_rate"] > 0.10
    assert v.evidence["ceiling_hit_rate"] > 0.05


def test_ceiling_hits_alone_do_not_invalidate():
    """A model that fills its output budget but still acts every turn is fine —
    it is verbose, not truncated. Only the co-occurrence is disqualifying."""
    records = [_rec(i, out_tokens=1024) for i in range(1, 101)]
    v = _check(_run(records))
    assert v.valid


# --- The incidents each remaining predicate came from ----------------------


def test_missing_summary_is_incomplete():
    """The tell that exposed the 429 sweep incident: the run dir looks populated
    but no summary was ever written."""
    v = _check(_run([_rec(i) for i in range(1, 16)], summary=False))
    assert not v.valid and v.reason == "incomplete"


def test_impossibly_fast_turns_are_transport_failure():
    """Three scenarios 'completed' in 15 seconds because a 429 traceback was
    swallowed by a stderr filter. 100 turns in 15s = 0.15s/turn."""
    v = _check(_run([_rec(i) for i in range(1, 101)], wall=15.0))
    assert not v.valid and v.reason == "transport_failure"
    assert v.evidence["seconds_per_turn"] < 0.5


def test_realistic_pace_is_not_flagged():
    """Gemini's ~2s/turn is the fastest honest leg measured; the floor must sit
    well clear of it."""
    v = _check(_run([_rec(i) for i in range(1, 101)], wall=200.0))
    assert v.valid


def test_wall_clock_stop_is_cap_mismatch_only_when_turn_matched():
    """The discarded qwen seed died on wall-clock, giving it fewer turns for a
    hardware reason. That is a confound under turn-matching — but legitimate for
    M2's deliberately fixed-budget baseline, so the rule is opt-in."""
    run = _run([_rec(i) for i in range(1, 51)], stop="max_wall_clock")
    assert not _check(run, expect_turn_matched=True).valid
    assert _check(run, expect_turn_matched=False).valid


def test_max_turns_and_success_are_fine_under_turn_matching():
    for stop in ("max_turns", "success"):
        run = _run([_rec(i) for i in range(1, 101)], stop=stop)
        assert _check(run, expect_turn_matched=True).valid, stop


# --- Wiring ---------------------------------------------------------------


def test_partition_keeps_rejections_rather_than_dropping_them():
    """A validator that leaves no record of its own rejections is how the
    exclusion list ended up being maintained by hand."""
    good = _run([_rec(i) for i in range(1, 101)])
    bad = _run([_rec(i) for i in range(1, 101)], wall=5.0)
    keep, drop = partition([(good, score_run(good)), (bad, score_run(bad))])
    assert len(keep) == 1 and len(drop) == 1
    _metrics, verdict = drop[0]
    assert verdict.reason == "transport_failure" and verdict.detail


def test_ceiling_source_is_reported_when_assumed():
    """Traces predating 2026-08-04 do not record max_output_tokens. Scoring
    still works, but a verdict resting on the fallback must say so."""
    m = score_run(_run([_rec(i) for i in range(1, 11)]))
    assert m.max_output_tokens == 1024 and m.max_output_tokens_source == "assumed"

    meta = {"scenario": "s1", "model": "sonnet", "tier": 0, "max_output_tokens": 4096}
    m2 = score_run(_run([_rec(i) for i in range(1, 11)], meta=meta))
    assert m2.max_output_tokens == 4096 and m2.max_output_tokens_source == "meta"
