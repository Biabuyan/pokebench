"""Matrix-driver tests — the meta-loop, driven entirely with fakes.

No ROM, no emulator, no key, no network: `run_cell` is injected, exactly like
`bench.run_seeds` takes `make_env`/`make_agent`.

The behaviours worth pinning are the ones a sweep can get silently wrong:
retrying invalid seeds, terminating on a stated condition, and never quietly
covering less of the matrix than it was asked to.
"""

from pathlib import Path

from pokebench.metrics.score import RunMetrics
from pokebench.replay import Run
from pokebench.runner.sweep import Cell, SweepCaps, run_sweep, valid_counts_from_rows


def _metrics(cell, *, cost=1.0, dead=0.0, ceiling=0.0, stop="max_turns", turns=100):
    return RunMetrics(
        scenario=cell.scenario, model=cell.model, tier=cell.tier,
        success=False, stop_reason=stop, turns=turns, steps_to_success=None,
        input_tokens=1000, output_tokens=100, cost_usd=cost,
        stuck_index=0.0, invalid_rate=0.0, idle_rate=0.5, tiles_explored=10,
        no_tool_call_rate=dead, ceiling_hit_rate=ceiling, turns_since_new_tile=0,
        cap_turns=100, reasoning=False, max_output_tokens=1024,
    )


def _run(wall=300.0, stop="max_turns"):
    return Run(dir=Path("x"), meta={}, records=[{}],
               summary={"success": False, "stop_reason": stop, "wall_seconds": wall})


CELL = Cell("s1_exit_pallet", "sonnet", 0)


def test_valid_seeds_accumulate_to_the_target():
    report = run_sweep([CELL], lambda c, a: (_run(), _metrics(c)), seeds=3)
    cr = report.cells[0]
    assert len(cr.valid) == 3 and cr.attempts == 3
    assert cr.satisfied and report.stopped_because == "complete"


def test_invalid_seeds_are_retried_and_recorded():
    """The closing of the loop: a seed that measured the harness is re-run, and
    the rejection still leaves a durable record."""
    calls = {"n": 0}

    def run_cell(cell, attempt):
        calls["n"] += 1
        if calls["n"] <= 2:  # first two attempts are truncated runs
            return _run(), _metrics(cell, dead=0.4, ceiling=0.4)
        return _run(), _metrics(cell)

    report = run_sweep([CELL], run_cell, seeds=2, max_retries=2)
    cr = report.cells[0]
    assert len(cr.valid) == 2
    assert len(cr.excluded) == 2
    assert {v.reason for _m, v in cr.excluded} == {"output_ceiling_truncation"}
    assert cr.satisfied


def test_retries_are_bounded_and_the_shortfall_is_reported():
    """A cell that can never produce a valid seed must stop and SAY it fell
    short — silent truncation of coverage reads as 'we covered everything'."""
    report = run_sweep(
        [CELL], lambda c, a: (_run(), _metrics(c, dead=0.4, ceiling=0.4)),
        seeds=3, max_retries=1,
    )
    cr = report.cells[0]
    assert cr.valid == [] and cr.attempts == 4  # 3 needed + 1 retry
    assert cr.stopped_because == "retries_exhausted" and not cr.satisfied
    assert any("INCOMPLETE" in line for line in report.summary_lines())


def test_sweep_dollar_cap_stops_the_matrix():
    """Bounds the whole sweep, not one episode — the gap that let an unattended
    matrix spend the project's remaining budget."""
    cells = [Cell("s1", "a", 0), Cell("s2", "b", 0)]
    report = run_sweep(
        cells, lambda c, a: (_run(), _metrics(c, cost=2.0)),
        seeds=3, caps=SweepCaps(max_usd=5.0),
    )
    assert report.stopped_because == "sweep_cap"
    assert report.usd_spent <= 6.0  # cap checked before launching, so it binds
    assert not all(c.satisfied for c in report.cells)


def test_max_seeds_cap_also_terminates():
    report = run_sweep(
        [CELL], lambda c, a: (_run(), _metrics(c)), seeds=10, caps=SweepCaps(max_seeds=4)
    )
    assert report.seeds_run == 4 and report.stopped_because == "sweep_cap"


def test_resume_skips_cells_already_satisfied():
    ran = []
    prior = [_metrics(CELL)] * 3

    def run_cell(cell, attempt):
        ran.append(cell.label)
        return _run(), _metrics(cell)

    report = run_sweep([CELL], run_cell, seeds=3, already_valid={CELL.label: prior})
    assert ran == [] and report.cells[0].stopped_because == "skipped"
    assert report.cells[0].satisfied
    # A skipped cell's cr.valid must still reflect the seeds it already has: otherwise
    # `cmd_sweep` (`result_row(cr.valid, cr.excluded) for cr in report.cells if
    # cr.valid or cr.excluded`) silently skips building a row for it too, and
    # summary_lines() would misreport "0 valid" for an already-complete cell.
    assert report.cells[0].valid == prior


def test_resume_tops_up_a_partial_cell():
    """The 2026-08-06 resume-bug fix: `already_valid` used to carry only a COUNT of
    prior valid seeds, so a resumed cell's `cr.valid` held only the seed(s) (re-)run
    THIS invocation -- `result_row(cr.valid, ...)` then built a row with a smaller
    seed count, and `merge_rows`'s unconditional overwrite silently dropped the
    earlier seeds' `seed_values` from results.json. `already_valid` now carries the
    prior seeds' actual `RunMetrics`, preloaded into `cr.valid`, so nothing is lost."""
    prior = [_metrics(CELL, cost=1.0), _metrics(CELL, cost=1.5)]
    report = run_sweep(
        [CELL], lambda c, a: (_run(), _metrics(c, cost=2.0)),
        seeds=3, already_valid={CELL.label: prior},
    )
    cr = report.cells[0]
    assert cr.attempts == 1  # only the missing seed is re-run
    assert len(cr.valid) == 3  # 2 reloaded + 1 new -- the earlier seeds are not dropped
    assert cr.valid[:2] == prior  # the reloaded RunMetrics objects themselves survive
    assert [m.cost_usd for m in cr.valid] == [1.0, 1.5, 2.0]


def test_resumed_row_no_longer_drops_earlier_seed_values():
    """End-to-end pin at the exact seam `cmd_sweep` uses: build the row the way the
    CLI does (`result_row(cr.valid, cr.excluded)`) and confirm it carries every valid
    seed, old and new -- not just the one(s) run this invocation."""
    from pokebench.metrics.results import result_row

    prior = [_metrics(CELL, cost=1.0), _metrics(CELL, cost=1.5)]
    report = run_sweep(
        [CELL], lambda c, a: (_run(), _metrics(c, cost=2.0)),
        seeds=3, already_valid={CELL.label: prior},
    )
    cr = report.cells[0]
    row = result_row(cr.valid, cr.excluded)
    assert row["seeds_valid"] == 3
    assert sorted(row["seed_values"]["cost_usd"]) == [1.0, 1.5, 2.0]


def test_already_excluded_is_preloaded_into_cr_excluded():
    """The 2026-08-16 sibling of `test_resume_tops_up_a_partial_cell`:
    `already_excluded` (from `reload_excluded_metrics`) must land in `cr.excluded`
    exactly like `already_valid` lands in `cr.valid`, so a resumed cell's row
    carries every exclusion it has ever earned, not just this invocation's."""
    from pokebench.metrics.validity import Validity

    prior_excluded = [(_metrics(CELL, cost=0.5), Validity(False, "incomplete", "died", {}))]
    report = run_sweep(
        [CELL], lambda c, a: (_run(), _metrics(c)),
        seeds=1, already_excluded={CELL.label: prior_excluded},
    )
    cr = report.cells[0]
    assert len(cr.valid) == 1  # the one new attempt still runs and succeeds
    assert cr.excluded == prior_excluded  # the reloaded exclusion is not dropped


def test_already_excluded_survives_a_skipped_cell():
    """A cell that already has enough valid seeds (no new attempt needed) must
    still carry its prior exclusions forward -- `cr.excluded` is history, not a
    signal about whether more attempts are required."""
    from pokebench.metrics.validity import Validity

    prior_valid = [_metrics(CELL)] * 3
    prior_excluded = [(_metrics(CELL, cost=0.5), Validity(False, "incomplete", "died", {}))]
    report = run_sweep(
        [CELL], lambda c, a: (_run(), _metrics(c)),
        seeds=3,
        already_valid={CELL.label: prior_valid},
        already_excluded={CELL.label: prior_excluded},
    )
    cr = report.cells[0]
    assert cr.stopped_because == "skipped"
    assert cr.excluded == prior_excluded


def test_executor_error_is_recorded_not_fatal():
    """One dead seed must not take the sweep down with it."""
    calls = {"n": 0}

    def run_cell(cell, attempt):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("provider exploded")
        return _run(), _metrics(cell)

    report = run_sweep([CELL], run_cell, seeds=1, max_retries=1)
    cr = report.cells[0]
    assert len(cr.valid) == 1
    assert [v.reason for _m, v in cr.excluded] == ["executor_error"]


def test_turn_matching_is_asserted_by_default():
    """A wall-clock stop is a confound in a cross-model sweep, so the driver
    rejects it unless the caller explicitly opts into fixed-budget runs."""
    report = run_sweep(
        [CELL], lambda c, a: (_run(stop="max_wall_clock"),
                              _metrics(c, stop="max_wall_clock")),
        seeds=1, max_retries=0,
    )
    assert [v.reason for _m, v in report.cells[0].excluded] == ["cap_mismatch"]

    ok = run_sweep(
        [CELL], lambda c, a: (_run(stop="max_wall_clock"),
                              _metrics(c, stop="max_wall_clock")),
        seeds=1, expect_turn_matched=False,
    )
    assert len(ok.cells[0].valid) == 1


def test_valid_counts_from_rows_reads_schema_3_and_2():
    rows = [
        {"scenario": "s1", "model": "gemini", "tier": 0, "seeds": 3, "seeds_valid": 2},
        {"scenario": "s2", "model": "gpt", "tier": 0, "seeds": 3},  # schema 2, no seeds_valid
    ]
    counts = valid_counts_from_rows(rows)
    assert counts["s1-gemini-t0"] == 2
    assert counts["s2-gpt-t0"] == 3
