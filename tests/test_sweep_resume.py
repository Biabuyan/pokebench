"""Offline tests for `reload_valid_metrics` / `reload_excluded_metrics` -- the
disk-reading half of the resume-bug fixes in `runner/sweep.py`
(`run_sweep`'s `already_valid` / `already_excluded`).

No ROM, no key, no network: these are ordinary JSONL trace files fabricated
under `tmp_path`, the same pattern `tests/test_cli_score.py::_write_run` uses
to exercise `pokebench score` offline. `reload_valid_metrics` only reads files
`cmd_sweep` itself already wrote in a prior invocation -- `out_dir/<cell.label>
/<timestamp>/seed0/{meta,run,summary}.json` -- so building that directly is
the correct seam, not a ROM or a live sweep.

The 2026-08-16 addition (`test_resume_across_two_invocations_preserves_
exclusions`) covers a distinct loss the 2026-08-06 fix did not: that fix
made `already_valid` carry real `RunMetrics` so a resumed cell's VALID seeds
survive a second invocation. It reconstructed only the valid half, so a
cell's EXCLUDED seeds from an earlier invocation were still lost the moment
a later invocation's `result_row` -- built from only that invocation's own
`cr.excluded` -- overwrote the row in results.json via `merge_rows`.
"""

from __future__ import annotations

import json
from pathlib import Path

from pokebench.metrics.results import result_row, write_results
from pokebench.metrics.score import score_run
from pokebench.replay import load_run
from pokebench.runner.sweep import Cell, reload_excluded_metrics, reload_valid_metrics, run_sweep

CELL = Cell("s1_exit_pallet", "sonnet", 0)


def _write_seed(
    attempt_dir: Path, *, cap_turns: int = 100, n: int = 5, complete: bool = True,
    stop_reason: str = "max_turns", wall_seconds: float | None = None,
) -> Path:
    """A minimal, offline-valid seed trace -- real tool calls, a plausible
    wall-clock, optionally no summary.json (incomplete) or a non-turn stop
    reason (cap mismatch under turn-matching)."""
    d = attempt_dir / "seed0"
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
                "executed": ["right"], "invalid": [], "is_noop": False, "over_limit": False,
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
        "scenario": CELL.scenario, "model": CELL.model, "tier": CELL.tier,
        "provider": "anthropic", "caps": {"max_turns": cap_turns},
    }
    (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    if complete:
        summary = {
            "success": False, "stop_reason": stop_reason, "turns": n,
            "wall_seconds": wall_seconds if wall_seconds is not None else n * 2.0,
        }
        (d / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    return d


def test_reload_valid_metrics_finds_valid_seeds_on_disk(tmp_path):
    cell_dir = tmp_path / CELL.label
    _write_seed(cell_dir / "20260805-100000")
    _write_seed(cell_dir / "20260805-101000")

    found = reload_valid_metrics(tmp_path, [CELL])
    assert len(found[CELL.label]) == 2
    assert all(m.scenario == CELL.scenario and m.model == CELL.model for m in found[CELL.label])


def test_reload_valid_metrics_excludes_incomplete_seeds(tmp_path):
    """The 429 incident's tell -- no summary.json -- must not be readmitted on
    resume just because the directory exists."""
    cell_dir = tmp_path / CELL.label
    _write_seed(cell_dir / "20260805-100000", complete=True)
    _write_seed(cell_dir / "20260805-101000", complete=False)

    found = reload_valid_metrics(tmp_path, [CELL])
    assert len(found[CELL.label]) == 1


def test_reload_valid_metrics_respects_turn_matching(tmp_path):
    """A wall-clock stop must not be silently laundered back into the row on
    resume -- the same turn-matching gate a fresh sweep applies."""
    cell_dir = tmp_path / CELL.label
    _write_seed(cell_dir / "20260805-100000", stop_reason="max_wall_clock")

    found = reload_valid_metrics(tmp_path, [CELL], expect_turn_matched=True)
    assert found == {}

    found_fixed = reload_valid_metrics(tmp_path, [CELL], expect_turn_matched=False)
    assert len(found_fixed[CELL.label]) == 1


def test_reload_valid_metrics_ignores_unrelated_cells_and_missing_dirs(tmp_path):
    other = Cell("s2_viridian_pokecenter", "gpt", 0)
    _write_seed(tmp_path / CELL.label / "20260805-100000")

    found = reload_valid_metrics(tmp_path, [other])
    assert found == {}


def test_reload_valid_metrics_on_nonexistent_out_dir_returns_empty(tmp_path):
    found = reload_valid_metrics(tmp_path / "does-not-exist-yet", [CELL])
    assert found == {}


def test_reload_excluded_metrics_finds_excluded_seeds_on_disk(tmp_path):
    """The excluded-side mirror of `test_reload_valid_metrics_finds_valid_seeds_on_disk`
    -- an incomplete attempt (no summary.json, the 429 incident's tell) must be
    reconstructed as an `(RunMetrics, Validity)` pair, not silently skipped."""
    cell_dir = tmp_path / CELL.label
    _write_seed(cell_dir / "20260805-100000", complete=True)
    _write_seed(cell_dir / "20260805-101000", complete=False)

    excluded = reload_excluded_metrics(tmp_path, [CELL])
    assert len(excluded[CELL.label]) == 1
    metrics, verdict = excluded[CELL.label][0]
    assert metrics.scenario == CELL.scenario and metrics.model == CELL.model
    assert verdict.reason == "incomplete"

    # And the valid half of the same scan still only counts the complete one.
    assert len(reload_valid_metrics(tmp_path, [CELL])[CELL.label]) == 1


def test_reload_excluded_metrics_on_nonexistent_out_dir_returns_empty(tmp_path):
    assert reload_excluded_metrics(tmp_path / "does-not-exist-yet", [CELL]) == {}


def _run_cell_writing_real_traces(out_dir: Path, *, complete: bool):
    """A fake `run_cell` that mirrors what `cmd_sweep`'s real one does: write an
    attempt's trace files to disk under `out_dir/<cell.label>/<attempt>/seed0/`
    and return `(load_run(seed_dir), score_run(...))`. Using real files (rather
    than in-memory `Run`/`RunMetrics` objects) is the point -- the bug under test
    only reproduces when a later 'invocation' reads genuine disk state back."""

    def run_cell(cell: Cell, attempt: int):
        attempt_dir = out_dir / cell.label / f"attempt{attempt}"
        seed_dir = _write_seed(attempt_dir, complete=complete)
        run = load_run(seed_dir)
        return run, score_run(run)

    return run_cell


def test_resume_across_two_invocations_preserves_exclusions(tmp_path):
    """Regression for the 2026-08-16 bug: `results_s4.json` reported
    `seeds_excluded: 0` / `exclusions: []` for every row despite 25 attempt
    directories backing 15 valid seeds on disk, because `reload_valid_metrics`
    reconstructed only the VALID prior seeds on resume -- a cell's excluded
    seeds from an earlier invocation vanished the instant a later invocation's
    `result_row` (built from only that invocation's own `cr.excluded`)
    overwrote the row in results.json via `merge_rows`.

    Invocation 1 burns its retries on two incomplete (crashed) attempts and
    never reaches a valid seed. Invocation 2 resumes, reloads BOTH the (empty)
    valid history and the two-entry excluded history from disk, and produces
    the one valid seed the cell needed. The final results.json row must still
    show both of invocation 1's exclusions -- not just invocation 2's (zero).
    """
    results_path = tmp_path / "results.json"

    # --- Invocation 1: two crashed attempts, never reaches a valid seed. ---
    report1 = run_sweep(
        [CELL],
        _run_cell_writing_real_traces(tmp_path, complete=False),
        seeds=1,
        max_retries=1,
    )
    cr1 = report1.cells[0]
    assert cr1.valid == []
    assert len(cr1.excluded) == 2
    assert cr1.stopped_because == "retries_exhausted"
    row1 = result_row(cr1.valid, cr1.excluded)
    assert row1["seeds_valid"] == 0
    assert row1["seeds_excluded"] == 2
    write_results(results_path, [row1], generated="t1")

    # --- Invocation 2 (resume): reload BOTH halves from disk. ---
    already_valid = reload_valid_metrics(tmp_path, [CELL])
    already_excluded = reload_excluded_metrics(tmp_path, [CELL])
    assert already_valid == {}  # nothing valid yet -- both prior attempts crashed
    assert len(already_excluded[CELL.label]) == 2

    report2 = run_sweep(
        [CELL],
        _run_cell_writing_real_traces(tmp_path, complete=True),
        seeds=1,
        already_valid=already_valid,
        already_excluded=already_excluded,
    )
    cr2 = report2.cells[0]
    assert len(cr2.valid) == 1
    assert cr2.satisfied
    row2 = result_row(cr2.valid, cr2.excluded)
    write_results(results_path, [row2], generated="t2")

    # --- The published artifact must carry invocation 1's exclusions. ---
    published = json.loads(results_path.read_text(encoding="utf-8"))
    row = next(
        r for r in published["rows"]
        if r["model"] == CELL.model and r["scenario"] == CELL.scenario
    )
    assert row["seeds_valid"] == 1
    assert row["seeds_excluded"] == 2
    assert len(row["exclusions"]) == 2
    assert all(e["reason"] == "incomplete" for e in row["exclusions"])
