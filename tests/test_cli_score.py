"""`pokebench score` console behaviour (offline -- no ROM/key).

Covers the group-level cap-turns-disagreement gate specifically: it must be
visible on the console every time, not only discoverable by opening --out's
results.json afterward. Adversarial review, 2026-08-05: `_cap_turns_conflict`
fired only inside `result_row()`, which `cmd_score` invoked only when `--out`
was passed -- so `pokebench score <mixed-cap dirs>` without `--out` printed
every seed as if valid.
"""

from __future__ import annotations

import json
from pathlib import Path

from pokebench.cli import main


def _write_run(
    d: Path,
    *,
    model: str = "haiku",
    scenario: str = "s1",
    tier: int = 0,
    cap_turns: int | None = 100,
    n: int = 3,
    complete: bool = True,
) -> Path:
    """A minimal, offline-valid run trace: real tool calls, a plausible
    wall-clock, and (optionally) no summary.json to fail the "incomplete"
    validity check."""
    d.mkdir(parents=True, exist_ok=True)
    records = [
        {
            "turn": i,
            "usage": {"input_tokens": 100, "output_tokens": 10},
            "cost_usd": 0.001,
            "cumulative": {"usd": round(0.001 * i, 6)},
            "tool_calls": [{"name": "press_buttons", "input": {"buttons": ["right"]}}],
            "tool_call": {"name": "press_buttons", "input": {"buttons": ["right"]}},
            "action": {
                "executed": ["right"],
                "invalid": [],
                "is_noop": False,
                "over_limit": False,
            },
            "observation_hash": f"h{i}",
            "model": {"text": "", "stop_reason": "tool_use"},
            "state": {"map_name": "ROUTE_1", "map_id": 12, "x": i, "y": 0, "facing": "down"},
            "success": False,
        }
        for i in range(1, n + 1)
    ]
    (d / "run.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
    )
    meta = {
        "scenario": scenario,
        "model": model,
        "tier": tier,
        "provider": "anthropic",
        "caps": {"max_turns": cap_turns},
    }
    (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    if complete:
        summary = {
            "success": False,
            "stop_reason": "max_turns",
            "turns": n,
            "wall_seconds": n * 2.0,
        }
        (d / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    return d


def test_cmd_score_prints_cap_turns_conflict_without_out(tmp_path, capsys):
    a = _write_run(tmp_path / "a", cap_turns=100)
    b = _write_run(tmp_path / "b", cap_turns=500)

    rc = main(["score", str(a), str(b)])  # no --out
    captured = capsys.readouterr()

    assert rc == 0
    assert "CAP-TURNS CONFLICT" in captured.err
    assert "haiku" in captured.err and "s1" in captured.err
    assert "100" in captured.err and "500" in captured.err


def test_cmd_score_no_conflict_when_cap_turns_agree(tmp_path, capsys):
    a = _write_run(tmp_path / "a", cap_turns=100)
    b = _write_run(tmp_path / "b", cap_turns=100)

    rc = main(["score", str(a), str(b)])
    captured = capsys.readouterr()

    assert rc == 0
    assert "CAP-TURNS CONFLICT" not in captured.err


def test_cmd_score_include_invalid_can_introduce_a_cap_turns_conflict(tmp_path, capsys):
    """`--include-invalid` has no test coverage anywhere else in the suite.
    Here it changes the outcome: the incomplete seed is dropped by default
    (only one cap_turns value survives into `keep`, so no conflict), but
    promoted into `keep` under `--include-invalid`, where it disagrees with
    the valid seed's cap_turns."""
    a = _write_run(tmp_path / "a", cap_turns=100, complete=True)
    b = _write_run(tmp_path / "b", cap_turns=500, complete=False)  # incomplete -> invalid

    rc_default = main(["score", str(a), str(b)])
    assert rc_default == 0
    assert "CAP-TURNS CONFLICT" not in capsys.readouterr().err

    rc_included = main(["score", str(a), str(b), "--include-invalid"])
    assert rc_included == 0
    assert "CAP-TURNS CONFLICT" in capsys.readouterr().err


def test_cmd_score_rejects_the_real_incident_shape_3x100_plus_1x500(tmp_path, capsys):
    """Pins, with synthetic fixtures, the exact seed-count shape of the real
    incident this gate was built for (Task B, 2026-08-11): three M3 gpt/S3
    seeds at `cap_turns: 100` (`runs/bench/s3_viridian_forest-gpt-t0/
    20260720-115320/seed{0,1,2}`) scored alongside one `cap_turns: 500` probe
    seed (`runs/probe500/s3_viridian_forest-gpt-t0/20260804-180134/seed0`).

    Before this gate existed, that exact real call produced `seeds_valid: 4,
    exclusions: []` and silently blended a 500-turn run into 100-turn medians
    (HANDOFF.md "A real gap in the eval-integrity gate"). This fixture is
    synthetic, not the real trace data (which lives under gitignored `runs/`
    and cannot be a committed test fixture) -- the real traces were
    hand-verified against a live `pokebench score --out <tmp file>` call
    (not committed to results.json) to confirm this same rejection actually
    fires today; see the implementer report for that run's console output.
    This test is what keeps the *shape* of that finding under CI once the
    real-trace check itself is gone from any transcript.
    """
    a = _write_run(tmp_path / "a", model="gpt", scenario="s3", cap_turns=100, n=100)
    b = _write_run(tmp_path / "b", model="gpt", scenario="s3", cap_turns=100, n=100)
    c = _write_run(tmp_path / "c", model="gpt", scenario="s3", cap_turns=100, n=100)
    d = _write_run(tmp_path / "d", model="gpt", scenario="s3", cap_turns=500, n=500)
    out = tmp_path / "scratch_results.json"

    rc = main(["score", str(a), str(b), str(c), str(d), "--turn-matched", "--out", str(out)])
    captured = capsys.readouterr()

    assert rc == 0
    assert "CAP-TURNS CONFLICT [gpt/s3/tier0]" in captured.err
    doc = json.loads(out.read_text(encoding="utf-8"))
    row = doc["rows"][0]
    # The previously-silent outcome (seeds_valid: 4, exclusions: []) must not
    # recur: the whole group is now rejected, not a passthrough with a stray
    # `cap_turns: None`.
    assert row["seeds_valid"] == 0
    assert row["seeds_excluded"] == 4
    assert all(e["reason"] == "cap_turns_disagreement" for e in row["exclusions"])
    assert row["cap_turns"] is None
    assert row["median_turns"] is None  # no silent blend of [100, 100, 100, 500]
