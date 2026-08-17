"""Eval integrity: decide whether a run measured the **model** or the **harness**.

A benchmark's worst failure mode is not a wrong number, it is a wrong number
that looks right. This project has already produced one: a sweep script piped
bench output through `Select-String`, swallowed a 429 traceback, and three
scenarios "completed" in 15 seconds looking entirely fine. Only the missing
`summary.json` files gave it away — a human noticing, not a check.

This module turns that noticing into code. Every predicate below is derived
from an incident that actually happened to this repo; none is speculative.

    incomplete                  the run never finished (no summary / no turns)
    transport_failure           turns too fast to have involved a model call
    output_ceiling_truncation   the harness clipped the model mid-thought
    cap_mismatch                a non-turn cap bound during a turn-matched sweep

**The gate that matters most is `output_ceiling_truncation`.** The obvious
version of that rule — "more than ~10% of turns emitted no tool call, therefore
the run is broken" — is *wrong*, and would have silently corrupted the M4
leaderboard. Sonnet's S1 seed1 has a 16% no-tool-call rate with a 384-token max
output, nowhere near the 1024 ceiling: it stopped acting because it believed it
had already reached Route 1, not because it was truncated. That is a **finding**
and must survive into the table. Excluding it would have deleted a legitimate
seed and pushed the sweep's worst row lower still.

So the rule is conditioned on the two rates *co-occurring*:

    dead turns + ceiling hits  -> truncation, a harness artifact  -> INVALID
    dead turns alone           -> a volitional stop, a result     -> VALID

Two runs with identical dead-turn rates can require opposite treatment, and
only `ceiling_hit_rate` separates them. See HANDOFF "Why sonnet scored 0/9".

Everything here is offline and pure — no ROM, no emulator, no API, no network.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from pokebench.metrics.score import RunMetrics
from pokebench.replay import Run

# --- Thresholds ------------------------------------------------------------
# Each is set from a real incident, not from taste. Widen them only with a
# trace that justifies the change.

#: Below this, a "turn" cannot have contained a real model call. The fastest
#: legitimate leg measured on this project is gemini at roughly 2 s/turn; the
#: 429 incident produced whole 100-turn scenarios in ~15 s (~0.15 s/turn). The
#: floor sits an order of magnitude under the honest case and an order of
#: magnitude over the failure, so it discriminates without being touchy.
MIN_SECONDS_PER_TURN = 0.5

#: A run may lose a few turns to a dead response without being broken. Both
#: rates must clear their threshold together for a truncation verdict.
MAX_NO_TOOL_CALL_RATE = 0.10
MAX_CEILING_HIT_RATE = 0.05

#: Stop reasons that mean "the model ran out of turns" (fine under
#: turn-matching) versus "something other than turns bound first".
_TURN_MATCHED_OK = frozenset({"success", "max_turns"})


@dataclass(frozen=True)
class Validity:
    """A verdict on one seed, with the numbers that produced it.

    `reason` is a stable machine-readable slug so `results.json` can carry
    exclusions without prose. `detail` is the human sentence. `evidence` holds
    the measured values so a reader can audit the call without the trace.
    """

    valid: bool
    reason: str | None = None
    detail: str | None = None
    evidence: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def check_validity(
    run: Run,
    metrics: RunMetrics,
    *,
    expect_turn_matched: bool = False,
    min_seconds_per_turn: float = MIN_SECONDS_PER_TURN,
    max_no_tool_call_rate: float = MAX_NO_TOOL_CALL_RATE,
    max_ceiling_hit_rate: float = MAX_CEILING_HIT_RATE,
) -> Validity:
    """Judge whether `run` measured the model. First failing rule wins.

    `expect_turn_matched=True` additionally rejects seeds where a wall-clock or
    dollar cap bound before the turn cap — under turn-matching those give a row
    fewer turns for a *hardware or price* reason rather than a capability one,
    which is exactly the confound turn-matching exists to remove. It is off by
    default because M2's fixed-budget baseline is legitimately dollar-bound.
    """
    summary = run.summary
    turns = metrics.turns

    # 1. Incomplete. The tell that caught the 429 incident: the run directory
    #    exists and looks populated, but no summary was ever written.
    if summary is None:
        return Validity(
            False,
            "incomplete",
            "no summary.json — the run never finished (this is the tell that "
            "exposed the 429 sweep incident)",
            {"turns": turns},
        )
    if turns == 0:
        return Validity(False, "incomplete", "no turns recorded", {"turns": 0})

    # 2. Transport failure. Turns faster than a network round-trip mean the
    #    model was never really consulted — errors were being counted as turns.
    wall = summary.get("wall_seconds")
    if wall is not None and turns:
        per_turn = wall / turns
        if per_turn < min_seconds_per_turn:
            return Validity(
                False,
                "transport_failure",
                f"{per_turn:.3f}s per turn is below the {min_seconds_per_turn}s "
                "floor — too fast to have contained a real model call",
                {"wall_seconds": wall, "turns": turns, "seconds_per_turn": round(per_turn, 3)},
            )

    # 3. Output-ceiling truncation — the gated rule. Read the module docstring
    #    before loosening either half of this condition.
    dead = metrics.no_tool_call_rate
    ceiling = metrics.ceiling_hit_rate
    if dead > max_no_tool_call_rate and ceiling > max_ceiling_hit_rate:
        return Validity(
            False,
            "output_ceiling_truncation",
            f"{dead:.0%} of turns emitted no tool call while {ceiling:.0%} hit the "
            f"{metrics.max_output_tokens}-token ceiling — the harness clipped the "
            "model mid-thought, so this measures the ceiling, not the model",
            {
                "no_tool_call_rate": dead,
                "ceiling_hit_rate": ceiling,
                "max_output_tokens": metrics.max_output_tokens,
                "max_output_tokens_source": metrics.max_output_tokens_source,
            },
        )

    # 4. Cap mismatch — only meaningful when the caller asserts turn-matching.
    if expect_turn_matched and metrics.stop_reason not in _TURN_MATCHED_OK:
        return Validity(
            False,
            "cap_mismatch",
            f"stopped on {metrics.stop_reason!r} during a turn-matched sweep — the "
            "row would carry fewer turns for a budget/hardware reason, not a "
            "capability one",
            {"stop_reason": metrics.stop_reason, "cap_turns": metrics.cap_turns},
        )

    # Valid. Where a rate is high but uncorroborated, say so — a volitional stop
    # is a result worth reading, not a defect, and it should not pass silently.
    notes = {}
    if dead > max_no_tool_call_rate:
        notes["volitional_stop"] = (
            f"{dead:.0%} of turns emitted no tool call with no ceiling hits "
            f"({ceiling:.0%}) — the model stopped acting by choice, not by "
            "truncation. This is a finding; it is kept in the table."
        )
    return Validity(True, None, None, {"no_tool_call_rate": dead, "ceiling_hit_rate": ceiling,
                                       **notes})


def partition(
    scored: list[tuple[Run, RunMetrics]], **kwargs
) -> tuple[list[RunMetrics], list[tuple[RunMetrics, Validity]]]:
    """Split scored runs into (valid metrics, [(metrics, verdict), ...] excluded).

    The excluded list is deliberately returned rather than dropped: a validator
    that leaves no record of its own rejections is how `results_traces.txt`
    ended up being maintained by hand.
    """
    keep: list[RunMetrics] = []
    drop: list[tuple[RunMetrics, Validity]] = []
    for run, metrics in scored:
        verdict = check_validity(run, metrics, **kwargs)
        (keep if verdict.valid else drop).append(metrics if verdict.valid else (metrics, verdict))
    return keep, drop
