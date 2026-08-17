"""The matrix driver: run many (scenario, model, tier) cells to N *valid* seeds.

`runner/bench.py` runs N seeds of one config. This runs a matrix of configs and
closes the loop that `bench` leaves open: it checks each seed against
`metrics/validity.py` **before** the seed can reach `results.json`, retries the
ones that measured the harness instead of the model, and stops on an explicit
condition rather than "when the script ends".

Why this is a loop and not a script — and why it does not violate the frozen
contract. The agent loop (observe → decide → act) is untouched; this is a
*meta-loop over runs*. It changes nothing a model sees. That distinction is the
whole reason it is allowed to exist: a framework inside the agent loop would
invalidate every published row, a driver outside it cannot.

**Termination.** The council's review of this design noted that nobody had
specified one, despite the brief defining a closed loop as one that terminates
on a condition. It is:

    stop when every cell holds `seeds` valid seeds,
      OR the cell has exhausted `max_retries` extra attempts,
      OR the sweep-level dollar cap would be exceeded.

Every one of those is reported, never silent. A sweep that quits early because
it ran out of money and says nothing is the same class of defect as a 429
reported as success.

**Resume.** Cells already holding enough valid seeds are skipped, so an
interrupted sweep is re-runnable without re-spending. Pass `already_valid` --
a `Cell.label -> list[RunMetrics]` map of the seeds already on disk, built by
`reload_valid_metrics` from the trace directories a prior invocation left
under `out_dir` (NOT from `results.json`'s already-aggregated numbers: a
published row's `seed_values` only carries six of `RunMetrics`' fields --
input_tokens, output_tokens, stuck_index, invalid_rate, ceiling_hit_rate,
stop_reason, cap_turns, reasoning and reasoning_provenance are not
recoverable from it, so it cannot seed a correct resume).

Until 2026-08-06, `already_valid` carried only a *count*. `cr.valid` for a
resumed cell then held only the seed(s) run in that invocation; `result_row`
built a row with that smaller seed count, and `merge_rows`'s unconditional
`merged[key] = row` overwrite silently dropped the earlier seeds'
`seed_values` from results.json. Carrying the actual prior `RunMetrics` and
preloading `cr.valid` with them (rather than trying to reconstruct-and-merge
two already-aggregated rows, which cannot be done exactly for the fields
`seed_values` does not carry) fixes it without any lossy approximation.

**2026-08-16: the same bug, one seam over.** That fix reconstructed only the
VALID prior seeds. `cr.excluded` for a resumed cell still held only the
seed(s) rejected in *that* invocation, so `result_row(cr.valid, cr.excluded)`
built a row whose `exclusions` were correct for the invocation but not for
the cell — and `merge_rows` overwrote the earlier, more-complete row with
it. Confirmed on `runs/sweep_s4/`: 25 attempt directories back 15 valid
seeds, and `results_s4.json` published `seeds_excluded: 0` / `exclusions: []`
for every row. `reload_excluded_metrics` closes it the same way: read the
excluded half of the SAME on-disk scan `reload_valid_metrics` already does
(`_reload_attempts` — one pass, so the two views of a cell's history cannot
drift apart), and preload `cr.excluded` with it before any new attempt runs.
This also means `merge_rows` needed no change: once a resumed invocation's
own row is built from the FULL reconstructed history (valid and excluded),
overwriting the old row with it is correct, not lossy — the two fixes are
the same shape, and a `merge_rows`-side fix would have been the wrong layer
twice. It would also have had no principled way to let a legitimately
re-run cell's stale exclusions disappear (see `_reload_attempts`'s
docstring); reading disk does, because deleting the old attempt directories
is what "legitimately re-run" already means operationally.

Not every crashed attempt is recoverable this way: if the executor dies
before `RunTracer.__init__` ever creates `seed0/` (e.g. `make_env()` raising
before a single file is written), there is nothing on disk to re-score —
that attempt is invisible to a resumed sweep, not merely excluded from it,
and this module cannot manufacture it back. Say so plainly rather than
rounding: `_reload_attempts` skips such attempts without counting them, so
a report built after resume cannot distinguish "0 crashes here" from "N
crashes here, none of which left a trace" — only the within-invocation
`executor_error` placeholder (`_placeholder_metrics`) ever sees those, and
only for the invocation that witnessed them.

The executor is injected (`run_cell`), exactly like `bench.run_seeds` takes
`make_env`/`make_agent`, so the whole driver is testable offline with fakes —
no ROM, no key, no network.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from pokebench.metrics.score import RunMetrics, score_run
from pokebench.metrics.validity import Validity, check_validity
from pokebench.replay import Run, load_run


@dataclass(frozen=True)
class Cell:
    """One (scenario, model, tier) configuration in the matrix."""

    scenario: str
    model: str
    tier: int = 0

    @property
    def label(self) -> str:
        return f"{self.scenario}-{self.model}-t{self.tier}"


@dataclass(frozen=True)
class SweepCaps:
    """Sweep-level limits, enforced here in code — never in a prompt.

    `runner/loop.Caps` bounds a single episode; nothing bounded a *sweep* until
    now, which is how an unattended matrix could quietly spend a project's
    remaining budget. `max_usd` is checked before each seed is launched, so the
    cap is a floor on what is spent, not a discovery made afterwards.
    """

    max_usd: float | None = None
    max_seeds: int | None = None


@dataclass
class SeedOutcome:
    cell: Cell
    attempt: int
    metrics: RunMetrics
    validity: Validity


@dataclass
class CellReport:
    cell: Cell
    valid: list[RunMetrics] = field(default_factory=list)
    excluded: list[tuple[RunMetrics, Validity]] = field(default_factory=list)
    attempts: int = 0
    stopped_because: str = "complete"  # complete | retries_exhausted | sweep_cap | skipped

    @property
    def satisfied(self) -> bool:
        return self.stopped_because in ("complete", "skipped")


@dataclass
class SweepReport:
    cells: list[CellReport] = field(default_factory=list)
    usd_spent: float = 0.0
    seeds_run: int = 0
    stopped_because: str = "complete"  # complete | sweep_cap

    def summary_lines(self) -> list[str]:
        """Human summary. Every cap and every exclusion is named — a sweep that
        silently covered less than it was asked to is the failure this module
        exists to prevent."""
        lines = [
            f"sweep {self.stopped_because}: {self.seeds_run} seed(s) run, "
            f"${self.usd_spent:.2f} spent across {len(self.cells)} cell(s)"
        ]
        for c in self.cells:
            bit = f"  {c.cell.label}: {len(c.valid)} valid"
            if c.excluded:
                reasons = ", ".join(sorted({v.reason or "?" for _, v in c.excluded}))
                bit += f", {len(c.excluded)} excluded ({reasons})"
            if not c.satisfied:
                bit += f"  [INCOMPLETE: {c.stopped_because}]"
            lines.append(bit)
        return lines


#: Signature of the injected executor: run one seed of `cell` and return its
#: trace + metrics. Raising is allowed — a raised exception is recorded as an
#: excluded seed rather than killing the sweep.
RunCell = Callable[[Cell, int], tuple[Run, RunMetrics]]


def run_sweep(
    cells: Sequence[Cell],
    run_cell: RunCell,
    *,
    seeds: int = 3,
    max_retries: int = 2,
    caps: SweepCaps | None = None,
    already_valid: dict[str, list[RunMetrics]] | None = None,
    already_excluded: dict[str, list[tuple[RunMetrics, Validity]]] | None = None,
    expect_turn_matched: bool = True,
    log: Callable[[str], None] | None = None,
    on_seed: Callable[[SeedOutcome], None] | None = None,
) -> SweepReport:
    """Drive the matrix to `seeds` valid seeds per cell. See module docstring.

    `already_valid` maps `Cell.label` -> the `RunMetrics` of valid seeds already on
    disk (from `reload_valid_metrics`), which is what makes the sweep resumable.
    `already_excluded` is its 2026-08-16 sibling: `Cell.label` -> the excluded
    seeds already on disk (from `reload_excluded_metrics`) -- without it, a
    cell's earlier exclusions vanish the moment a later invocation's row
    overwrites the first in results.json (see module docstring). `expect_turn_
    matched` defaults to True because that is what a cross-model sweep is for;
    set it False for a deliberately fixed-budget run (M2's baseline).
    """
    caps = caps or SweepCaps()
    already_valid = already_valid or {}
    already_excluded = already_excluded or {}
    report = SweepReport()
    say = log or (lambda _m: None)

    for cell in cells:
        cr = CellReport(cell)
        report.cells.append(cr)

        prior = already_valid.get(cell.label, [])
        # Preload prior exclusions unconditionally and BEFORE the skip check
        # below: they are the cell's history, not a signal about whether more
        # attempts are needed, so a cell that already holds enough valid seeds
        # must still carry every earlier attempt's exclusion forward rather
        # than losing it the moment no new attempt runs.
        cr.excluded = list(already_excluded.get(cell.label, []))
        if len(prior) >= seeds:
            cr.stopped_because = "skipped"
            # Populate cr.valid even on skip: `cmd_sweep` only builds a row when
            # `cr.valid or cr.excluded` is non-empty, and an empty list here would
            # both under-report "0 valid" in summary_lines() AND (were this cell's
            # row ever rebuilt) wrongly zero out a row that is in fact complete.
            cr.valid = list(prior)
            say(f"{cell.label}: skip — {len(prior)} valid seed(s) already on disk")
            continue

        # Preload the seeds this cell already has, so the row this invocation
        # produces reflects EVERY valid seed the cell has ever earned, not just the
        # one(s) (re-)run below. Only new attempts count against `max_retries`.
        cr.valid = list(prior)
        needed = seeds - len(prior)
        budget_exhausted = False
        while len(cr.valid) < seeds and cr.attempts < needed + max_retries:
            if caps.max_seeds is not None and report.seeds_run >= caps.max_seeds:
                budget_exhausted = True
                break
            # Check the dollar cap *before* launching, so it bounds spending
            # rather than merely reporting it after the fact.
            if caps.max_usd is not None and report.usd_spent >= caps.max_usd:
                budget_exhausted = True
                break

            cr.attempts += 1
            report.seeds_run += 1
            try:
                run, metrics = run_cell(cell, cr.attempts - 1)
            except Exception as exc:  # noqa: BLE001 - a dead seed must not kill the sweep
                say(f"{cell.label} attempt {cr.attempts}: FAILED ({exc!r}) — counted as excluded")
                cr.excluded.append(
                    (
                        _placeholder_metrics(cell),
                        Validity(False, "executor_error", repr(exc), {}),
                    )
                )
                continue

            report.usd_spent += metrics.cost_usd
            verdict = check_validity(run, metrics, expect_turn_matched=expect_turn_matched)
            if on_seed is not None:
                on_seed(SeedOutcome(cell, cr.attempts - 1, metrics, verdict))

            if verdict.valid:
                cr.valid.append(metrics)
                say(
                    f"{cell.label} attempt {cr.attempts}: valid "
                    f"(success={metrics.success} turns={metrics.turns} ${metrics.cost_usd})"
                )
            else:
                cr.excluded.append((metrics, verdict))
                say(
                    f"{cell.label} attempt {cr.attempts}: "
                    f"INVALID [{verdict.reason}] {verdict.detail}"
                )

        if budget_exhausted:
            cr.stopped_because = "sweep_cap"
            report.stopped_because = "sweep_cap"
            say(f"{cell.label}: stopped — sweep cap reached with {len(cr.valid)}/{seeds} valid")
            break
        if len(cr.valid) < seeds:
            cr.stopped_because = "retries_exhausted"
            say(
                f"{cell.label}: stopped — {cr.attempts} attempt(s) produced "
                f"{len(cr.valid) - len(prior)} new valid seed(s); "
                f"{len(cr.valid)}/{seeds} valid total"
            )

    return report


def _placeholder_metrics(cell: Cell) -> RunMetrics:
    """A stand-in row for a seed that died before it could be scored, so the
    exclusion still names its cell in the report."""
    return RunMetrics(
        scenario=cell.scenario,
        model=cell.model,
        tier=cell.tier,
        success=False,
        stop_reason="executor_error",
        turns=0,
        steps_to_success=None,
        input_tokens=0,
        output_tokens=0,
        cost_usd=0.0,
        stuck_index=0.0,
        invalid_rate=0.0,
        idle_rate=0.0,
        tiles_explored=0,
        no_tool_call_rate=0.0,
        ceiling_hit_rate=0.0,
        turns_since_new_tile=None,  # died before any state was ever recorded
    )


def valid_counts_from_rows(rows: Iterable[dict]) -> dict[str, int]:
    """How many valid seeds each cell's *published* row currently claims.

    A count only -- `results.json` cannot yield a full `RunMetrics` per seed (see
    `run_sweep`'s docstring), so this is no longer what feeds `run_sweep`'s
    `already_valid` (use `reload_valid_metrics` for that). Kept as a small,
    independent utility: comparing this against `reload_valid_metrics`'s counts is
    a cheap way to notice `results.json` and the on-disk traces have drifted apart.
    """
    counts: dict[str, int] = {}
    for r in rows:
        label = Cell(str(r.get("scenario")), str(r.get("model")), int(r.get("tier", 0))).label
        counts[label] = int(r.get("seeds_valid", r.get("seeds", 0)) or 0)
    return counts


def _reload_attempts(
    out_dir: str | Path,
    cells: Sequence[Cell],
    *,
    expect_turn_matched: bool = True,
) -> dict[str, tuple[list[RunMetrics], list[tuple[RunMetrics, Validity]]]]:
    """Re-score and re-validate EVERY attempt directory a prior `pokebench sweep`
    invocation left on disk, for each cell. The one on-disk scan behind both
    `reload_valid_metrics` and `reload_excluded_metrics` -- a single pass, so the
    two views of a cell's history (what counted, what didn't) can never drift
    apart the way they would if each function walked the directory tree on its
    own with subtly different logic.

    Expects the layout `cmd_sweep` writes: `out_dir/<cell.label>/<attempt>/seed0/
    {meta,run,summary}.json`. Every candidate attempt is judged fresh against
    `check_validity` -- the SAME gate a fresh sweep applies -- so a seed that
    would be rejected today (e.g. a wall-clock stop under turn-matching) is not
    silently readmitted just because it predates this resume, and conversely a
    seed an updated gate would now accept is promoted rather than stuck with a
    stale verdict.

    This is also how a legitimately re-run cell sheds its old exclusions: delete
    the earlier attempt directories and there is nothing left here to
    reconstruct, so the next resume starts clean. Leave them, and their
    history -- valid or excluded -- carries forward exactly as it happened,
    which is the correct default: a resumed sweep did not un-happen its past
    attempts just because this invocation is the one that finally succeeded.

    An attempt whose `seed0/` directory does not exist at all (the executor
    died before `RunTracer.__init__` wrote anything -- see module docstring) is
    skipped, not fabricated into a placeholder: there is no meta.json to
    attribute it to this cell and no turns to report, so a manufactured entry
    would assert evidence that does not exist. Such attempts are invisible to a
    resumed sweep; report that fact rather than rounding it away.
    """
    out_dir = Path(out_dir)
    found: dict[str, tuple[list[RunMetrics], list[tuple[RunMetrics, Validity]]]] = {}
    for cell in cells:
        cell_dir = out_dir / cell.label
        if not cell_dir.is_dir():
            continue
        valid: list[RunMetrics] = []
        excluded: list[tuple[RunMetrics, Validity]] = []
        for attempt_dir in sorted(p for p in cell_dir.iterdir() if p.is_dir()):
            seed_dir = attempt_dir / "seed0"
            if not seed_dir.is_dir():
                continue
            run = load_run(seed_dir)
            metrics = score_run(run)
            verdict = check_validity(run, metrics, expect_turn_matched=expect_turn_matched)
            if verdict.valid:
                valid.append(metrics)
            else:
                excluded.append((metrics, verdict))
        if valid or excluded:
            found[cell.label] = (valid, excluded)
    return found


def reload_valid_metrics(
    out_dir: str | Path,
    cells: Sequence[Cell],
    *,
    expect_turn_matched: bool = True,
) -> dict[str, list[RunMetrics]]:
    """Rebuild `run_sweep`'s `already_valid` from the trace directories a prior
    `pokebench sweep` invocation left on disk, so a resumed sweep can preload
    `cr.valid` with the SEEDS themselves, not just a count of them (the 2026-08-06
    resume-bug fix -- see `run_sweep`'s docstring for why `results.json` cannot be
    the source here). See `_reload_attempts` for the scan this wraps.
    """
    return {
        label: valid
        for label, (valid, _excluded) in _reload_attempts(
            out_dir, cells, expect_turn_matched=expect_turn_matched
        ).items()
        if valid
    }


def reload_excluded_metrics(
    out_dir: str | Path,
    cells: Sequence[Cell],
    *,
    expect_turn_matched: bool = True,
) -> dict[str, list[tuple[RunMetrics, Validity]]]:
    """Rebuild `run_sweep`'s `already_excluded` from the same trace directories
    `reload_valid_metrics` reads, so a resumed sweep can preload `cr.excluded`
    with the cell's full exclusion history instead of losing everything but
    this invocation's own rejections (the 2026-08-16 resume-bug fix -- see
    module docstring). See `_reload_attempts` for the scan this wraps.
    """
    return {
        label: excluded
        for label, (_valid, excluded) in _reload_attempts(
            out_dir, cells, expect_turn_matched=expect_turn_matched
        ).items()
        if excluded
    }
