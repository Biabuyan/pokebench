import json

from pokebench.cli import main
from pokebench.replay import (
    find_stuck_segments,
    format_run,
    latest_run_dir,
    load_run,
    token_report,
)


def _rec(turn, hashv, x, y, executed=None, note=None, success=False, text="", inp=100, out=10):
    return {
        "turn": turn,
        "usage": {"input_tokens": inp, "output_tokens": out},
        "cost_usd": 0.001,
        "cumulative": {"usd": round(0.001 * turn, 6)},
        "tool_calls": [],
        "tool_call": None,
        "action": {"executed": executed} if executed is not None else None,
        "notes_update": {"chars": note, "truncated": False} if note is not None else None,
        "observation_hash": hashv,
        "model": {"text": text, "stop_reason": "tool_use"},
        "state": {"map_name": "REDS_HOUSE_2F", "map_id": 38, "x": x, "y": y, "facing": "down"},
        "success": success,
    }


def _write_run(d, records, meta=None, summary=None):
    d.mkdir(parents=True, exist_ok=True)
    (d / "run.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
    )
    if meta is not None:
        (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    if summary is not None:
        (d / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    return d


def _sample(tmp_path):
    records = [
        _rec(1, "aaa", 1, 0, executed=["right"], text="going east"),
        _rec(2, "bbb", 1, 7),  # stuck at (1,7) for turns 2-4
        _rec(3, "bbb", 1, 7),
        _rec(4, "bbb", 1, 7),
        _rec(5, "ccc", 0, 0, executed=["up"], success=True),
    ]
    meta = {"model": "haiku", "tier": 1, "tools": ["press_buttons", "update_notes"],
            "system_prompt_version": 2}
    summary = {"success": True, "stop_reason": "success", "turns": 5, "usd": 0.005,
               "wall_seconds": 12.3}
    return _write_run(tmp_path / "run1", records, meta, summary)


def test_load_run(tmp_path):
    run = load_run(_sample(tmp_path))
    assert len(run.records) == 5
    assert run.meta["model"] == "haiku" and run.meta["tier"] == 1
    assert run.summary["success"] is True


def test_find_stuck_segments_by_hash(tmp_path):
    run = load_run(_sample(tmp_path))
    segs = find_stuck_segments(run.records, min_len=3)
    assert len(segs) == 1
    start, end, count, label = segs[0]
    assert (start, end, count) == (2, 4, 3)
    assert label == "REDS_HOUSE_2F (1,7)"


def test_stuck_falls_back_to_position_without_hash(tmp_path):
    # older traces have no observation_hash -> detect via map+position
    records = [_rec(i, None, 1, 7) for i in range(1, 5)]
    d = _write_run(tmp_path / "old", records)
    segs = find_stuck_segments(load_run(d).records)
    assert segs and segs[0][2] == 4  # all four turns unchanged at (1,7)


def test_token_report(tmp_path):
    run = load_run(_sample(tmp_path))
    tr = token_report(run.records)
    assert tr["turns"] == 5
    assert tr["input_tokens"] == 500 and tr["output_tokens"] == 50
    assert tr["avg_input_per_turn"] == 100


def test_format_run_contains_key_lines(tmp_path):
    text = format_run(load_run(_sample(tmp_path)), full=True)
    assert "model=haiku tier=1" in text
    assert "stuck-loops (1)" in text and "turns 2-4" in text
    assert "SUCCESS" in text
    assert "tokens: 500+50" in text
    assert "reasoning: going east" in text  # --full shows model text


def test_latest_run_dir_skips_dirs_without_trace(tmp_path):
    good = _sample(tmp_path)
    (tmp_path / "empty").mkdir()  # no run.jsonl -> ignored
    assert latest_run_dir(tmp_path) == good


def test_cli_replay_returns_0_and_2(tmp_path):
    assert main(["replay", str(_sample(tmp_path))]) == 0
    assert main(["replay", str(tmp_path / "nope")]) == 2
