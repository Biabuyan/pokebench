"""`TracingWalker` — the M0 spike's audit log (offline: no ROM/key).

Covers the `tap()` passthrough added for Task B/C (2026-08-06): the CLI wires
`EncounterAwareWalker` around `TracingWalker` so every escape attempt (the
HP/PP hazard flagged in navigate.py) lands in `trace.jsonl` right alongside
the `dpad()` steps, sharing one physical-action-ordered step counter -- not a
separate, easy-to-miss log.
"""

from __future__ import annotations

import json

from pokebench.harness.trace import TracingWalker
from tests.fake_world import build_pallet_world


def test_tap_passes_through_to_the_inner_walker(tmp_path):
    w = build_pallet_world()
    w.in_battle = 1
    w.escape_attempts_left = 1  # one tap clears a wild battle
    tw = TracingWalker(w, tmp_path / "out", screenshots=False)
    tw.tap("a")
    tw.close()
    assert w.in_battle == 0  # the tap really reached the inner walker


def test_tap_is_logged_to_trace_jsonl_like_a_dpad_step(tmp_path):
    w = build_pallet_world()
    w.in_battle = 1
    w.escape_attempts_left = 2
    tw = TracingWalker(w, tmp_path / "out", screenshots=False)
    tw.tap("a")
    tw.close()
    lines = (tmp_path / "out" / "trace.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["step"] == 1
    assert rec["action"] == {"type": "tap", "button": "a"}
    assert rec["state"]["x"] == w.pos[0]  # the resulting state, same shape as a dpad record


def test_dpad_and_tap_share_one_step_counter_in_call_order(tmp_path):
    """The escape-attempt audit trail is only useful if it's interleaved with
    the dpad steps in the order they actually happened, not a separate
    sequence starting back at 1."""
    w = build_pallet_world()
    tw = TracingWalker(w, tmp_path / "out", screenshots=False)
    tw.dpad("right")  # step 1
    w.in_battle = 1
    w.escape_attempts_left = 1
    tw.tap("a")  # step 2
    tw.dpad("right")  # step 3
    tw.close()
    lines = (tmp_path / "out" / "trace.jsonl").read_text(encoding="utf-8").splitlines()
    records = [json.loads(line) for line in lines]
    assert [r["step"] for r in records] == [1, 2, 3]
    assert [r["action"]["type"] for r in records] == ["dpad", "tap", "dpad"]


# --- RunTracer: raw provider response bodies (Task A, 2026-08-11) ----------
#
# Raw bodies are written to a per-turn side file, not inlined into run.jsonl:
# a 500-turn sweep leg would otherwise bloat the main trace with bytes nothing
# reads back today. Opt-in (`save_raw=False` default) because a raw body is
# pure debug payload with real disk cost across a long run, and defaulting it
# on would silently grow every run's footprint for a feature added to catch a
# parser bug after the fact, not one anything currently depends on.


def test_record_turn_does_not_write_raw_by_default(tmp_path):
    from pokebench.harness.trace import RunTracer

    tracer = RunTracer(tmp_path, screenshots=False)
    tracer.record_turn({"turn": 1}, raw={"output": ["whatever"]})
    tracer.close()
    assert not (tmp_path / "raw").exists()


def test_record_turn_writes_raw_side_file_when_enabled(tmp_path):
    from pokebench.harness.trace import RunTracer

    tracer = RunTracer(tmp_path, screenshots=False, save_raw=True)
    tracer.record_turn({"turn": 1}, raw={"output": [{"type": "message"}], "usage": {"x": 1}})
    tracer.close()
    raw_file = tmp_path / "raw" / "turn_0001.json"
    assert raw_file.exists()
    assert json.loads(raw_file.read_text(encoding="utf-8")) == {
        "output": [{"type": "message"}],
        "usage": {"x": 1},
    }


def test_record_turn_skips_raw_file_when_raw_is_none_even_if_enabled(tmp_path):
    from pokebench.harness.trace import RunTracer

    tracer = RunTracer(tmp_path, screenshots=False, save_raw=True)
    tracer.record_turn({"turn": 1}, raw=None)
    tracer.close()
    assert not (tmp_path / "raw").exists()


def test_raw_file_numbering_matches_the_turn_counter(tmp_path):
    from pokebench.harness.trace import RunTracer

    tracer = RunTracer(tmp_path, screenshots=False, save_raw=True)
    tracer.record_turn({"turn": 1}, raw={"n": 1})
    tracer.record_turn({"turn": 2}, raw=None)  # no tool call this turn -> no raw body
    tracer.record_turn({"turn": 3}, raw={"n": 3})
    tracer.close()
    raw_dir = tmp_path / "raw"
    assert sorted(p.name for p in raw_dir.iterdir()) == ["turn_0001.json", "turn_0003.json"]


def test_raw_body_never_carries_the_run_jsonl_record_inline(tmp_path):
    # The design point of a side file: the main trace stays lean, so a human
    # (or a script) skimming run.jsonl never has to page past raw provider
    # payloads to find the fields metrics/replay actually read.
    from pokebench.harness.trace import RunTracer

    tracer = RunTracer(tmp_path, screenshots=False, save_raw=True)
    tracer.record_turn({"turn": 1, "tool_call": {"name": "press_buttons"}}, raw={"big": "x" * 500})
    tracer.close()
    line = (tmp_path / "run.jsonl").read_text(encoding="utf-8").strip()
    assert "big" not in line
