"""Aggregate N seeds into a leaderboard row, and build/merge `results.json`.

The plan reports the **median over N=3 seeds** (the emulator is deterministic,
so all variance is the model's sampling at temperature 1.0). One row per
(model, scenario, tier). `results.json` is the artifact the M4 leaderboard
renders; rows merge by that key so you can accumulate across bench runs.
"""

from __future__ import annotations

import json
from pathlib import Path

from pokebench.metrics.score import RunMetrics
from pokebench.metrics.validity import Validity

# 2 adds per-row provenance (`cap_turns`, `reasoning`, `stop_reasons`) so a
# reader can tell M3's turn-matched rows from M2's fixed-budget ones without
# consulting the traces. Metric fields are unchanged from schema 1.
#
# 3 (2026-08-04) makes eval integrity part of the artifact instead of a
# convention. Adds:
#   - `seeds_valid` / `seeds_excluded` and an `exclusions` list, so every
#     rejected seed leaves a durable, machine-readable record *in the published
#     file*. Previously exclusions lived only in a hand-written comment block in
#     `results_traces.txt` — a validator with no memory of its own rejections.
#     Medians are computed over valid seeds only.
#   - `seed_values`: the per-seed numbers behind each median. Added because the
#     sonnet S1 row is a median over 4/18/2 tiles — honest, but hiding a bimodal
#     distribution. A leaderboard rendering only medians misrepresents it.
#   - medians for `no_tool_call_rate` / `ceiling_hit_rate` (see score.py).
#
# 4 (2026-08-06) adds `reasoning_provenance` to every row (the field itself landed in
# `metrics/score.py::_reasoning_provenance` on 2026-08-05, but the schema constant was not
# bumped when it did, so the committed `results.json` was regenerated at schema 3 without
# it -- the sonnet-reasoning caveat stayed prose-only in HANDOFF.md). No other field
# changed; see `results_traces.txt` for the regen command and the before/after diff.
#
# 5 (2026-08-15) adds `median_turns_since_new_tile` and a `turns_since_new_tile` array in
# `seed_values` -- the "was it turn-limited?" diagnostic (see
# `metrics/score.py::_turns_since_new_tile`). Every other field is unchanged.
RESULTS_SCHEMA = 5


def median(values) -> float | int | None:
    """Median of the non-None values (None if there are none)."""
    xs = sorted(v for v in values if v is not None)
    if not xs:
        return None
    mid = len(xs) // 2
    if len(xs) % 2:
        return xs[mid]
    avg = (xs[mid - 1] + xs[mid]) / 2
    return round(avg, 4) if isinstance(avg, float) else avg


def _unanimous(values):
    """The shared value across seeds, or None if they disagree.

    A row is only meaningful when its seeds ran under the same conditions, so a
    disagreement is reported as "unknown" rather than silently averaged away.
    """
    xs = {v for v in values}
    return xs.pop() if len(xs) == 1 else None


def _cap_turns_conflict(metrics: list[RunMetrics]) -> Validity | None:
    """Seeds that ran under different turn budgets are not repeats of the same
    experiment, no matter how each one stopped -- both a 100-turn and a
    500-turn seed legitimately stop on `max_turns`, so `validity.py`'s
    `cap_mismatch` rule (which only looks at `stop_reason`) cannot catch this.
    `aggregate()`'s `_unanimous` already computes the signal; this promotes it
    from "report as unknown" to "reject the group", because a median silently
    blended across incomparable budgets is worse than a hole in the table.

    `cap_turns: None` means "the trace predates the `caps.max_turns` field",
    not a third disagreeing value (see `RunMetrics.cap_turns`) -- it is
    excluded from the comparison entirely, never counted as a mismatch.
    """
    known = {m.cap_turns for m in metrics if m.cap_turns is not None}
    if len(known) < 2:
        return None
    return Validity(
        False,
        "cap_turns_disagreement",
        f"seeds ran under different turn budgets {sorted(known)} -- a median "
        "over them would blend incomparable caps into one number",
        {"cap_turns_seen": sorted(known)},
    )


def aggregate(
    metrics: list[RunMetrics],
    excluded: list[tuple[RunMetrics, Validity]] | None = None,
) -> dict:
    """Median-over-seeds summary for one (model, scenario, tier).

    `metrics` are the seeds that count. `excluded` are seeds that ran but were
    rejected by `metrics/validity.py`; they are recorded in the row rather than
    dropped, because an exclusion nobody can see is indistinguishable from a
    seed that never ran.
    """
    n = len(metrics)
    excluded = excluded or []
    successes = sum(1 for m in metrics if m.success)
    stop_reasons: dict[str, int] = {}
    for m in metrics:
        stop_reasons[m.stop_reason] = stop_reasons.get(m.stop_reason, 0) + 1
    return {
        "seeds": n,
        # Eval integrity, first-class: how many seeds ran, how many counted, and
        # exactly why each rejection happened.
        "seeds_valid": n,
        "seeds_excluded": len(excluded),
        "exclusions": [
            {
                "reason": v.reason,
                "detail": v.detail,
                "stop_reason": m.stop_reason,
                "turns": m.turns,
                "evidence": v.evidence,
            }
            for m, v in excluded
        ],
        # Provenance first: what the row may fairly be compared against.
        "cap_turns": _unanimous(m.cap_turns for m in metrics),
        "reasoning": _unanimous(m.reasoning for m in metrics),
        # Additive (2026-08-05): `reasoning` alone reports `None` for both "off,
        # unrecorded" (sonnet's legacy rows) and "not applicable" (gpt/gemini,
        # which reason by default and ignore the flag) -- see
        # `metrics/score.py::_reasoning_provenance`. This is the field that
        # tells them apart so the distinction survives in results.json itself,
        # not only in README/HANDOFF prose.
        "reasoning_provenance": _unanimous(m.reasoning_provenance for m in metrics),
        # How the seeds ended. A cap means the run ran out of budget; anything
        # else means the episode itself resolved.
        "stop_reasons": dict(sorted(stop_reasons.items())),
        "success_rate": round(successes / n, 3) if n else 0.0,
        "success_at_cap": successes * 2 > n,  # majority = median of booleans
        "median_steps_to_success": median([m.steps_to_success for m in metrics]),
        "median_turns": median([m.turns for m in metrics]),
        "median_input_tokens": median([m.input_tokens for m in metrics]),
        "median_output_tokens": median([m.output_tokens for m in metrics]),
        "median_cost_usd": median([m.cost_usd for m in metrics]),
        "median_stuck_index": median([m.stuck_index for m in metrics]),
        "median_invalid_rate": median([m.invalid_rate for m in metrics]),
        "median_idle_rate": median([m.idle_rate for m in metrics]),
        "median_tiles_explored": median([m.tiles_explored for m in metrics]),
        "median_no_tool_call_rate": median([m.no_tool_call_rate for m in metrics]),
        "median_ceiling_hit_rate": median([m.ceiling_hit_rate for m in metrics]),
        "median_turns_since_new_tile": median([m.turns_since_new_tile for m in metrics]),
        # The numbers behind the medians. A median of 4/18/2 and a median of
        # 4/4/4 are the same figure and completely different results; M4 must be
        # able to tell them apart without opening the traces.
        "seed_values": {
            "success": [m.success for m in metrics],
            "turns": [m.turns for m in metrics],
            "cost_usd": [m.cost_usd for m in metrics],
            "tiles_explored": [m.tiles_explored for m in metrics],
            "idle_rate": [m.idle_rate for m in metrics],
            "no_tool_call_rate": [m.no_tool_call_rate for m in metrics],
            "turns_since_new_tile": [m.turns_since_new_tile for m in metrics],
        },
    }


def result_row(
    metrics: list[RunMetrics],
    excluded: list[tuple[RunMetrics, Validity]] | None = None,
) -> dict:
    """One leaderboard row: the (model, scenario, tier) key + aggregated metrics.

    A row may legitimately have zero valid seeds — every seed excluded is itself
    a result, and publishing the row with `seeds_valid: 0` plus the exclusion
    reasons is more honest than omitting it and leaving a silent hole in the
    table.
    """
    excluded = list(excluded or [])
    if not metrics and not excluded:
        raise ValueError("no metrics to build a row from")

    conflict = _cap_turns_conflict(metrics)
    if conflict is not None:
        # Reject the WHOLE row, not the minority seeds: at 2-vs-2 there is no
        # principled minority, and shrinking the row silently is exactly the
        # failure class this eval-integrity gate exists to prevent.
        excluded = excluded + [(m, conflict) for m in metrics]
        metrics = []

    first = metrics[0] if metrics else excluded[0][0]
    return {
        "model": first.model,
        "scenario": first.scenario,
        "tier": first.tier,
        **aggregate(metrics, excluded),
    }


def _key(row: dict) -> tuple:
    return (row.get("model"), row.get("scenario"), row.get("tier"))


def merge_rows(existing: list[dict], new: list[dict]) -> list[dict]:
    """Merge new rows over existing ones, replacing by (model, scenario, tier)."""
    merged = {_key(r): r for r in existing}
    for r in new:
        merged[_key(r)] = r
    return sorted(
        merged.values(),
        key=lambda r: (str(r.get("scenario")), str(r.get("model")), r.get("tier", 0)),
    )


def write_results(path: str | Path, rows: list[dict], generated: str | None = None) -> None:
    """Write/merge rows into results.json. `generated` is a caller-supplied
    timestamp (Date is unavailable inside workflow scripts; the CLI stamps it)."""
    path = Path(path)
    existing = []
    if path.exists():
        prior = json.loads(path.read_text(encoding="utf-8"))
        existing = prior.get("rows", [])
    doc = {
        "schema": RESULTS_SCHEMA,
        "generated": generated,
        "rows": merge_rows(existing, rows),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
