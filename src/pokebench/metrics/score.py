"""Score one run's trace into the M2 metrics (plan #6).

Pure functions over a `replay.Run` (the JSONL trace + summary) — no ROM, no
emulator, no API — so scoring is fully offline and testable, and `pokebench
score` can run against any existing `runs/agent/<ts>/` directory.

Metrics:
- **success@cap** — did the run reach the goal within the caps?
- **steps-to-success** — turns taken (only meaningful when it succeeded).
- **tokens + $** per run.
- **stuck-loop index** — fraction of turns spent in a repeated-state loop
  (the classic failure mode; from `replay.find_stuck_segments`).
- **invalid-action rate** — fraction of turns wasted on malformed input: no
  tool call, or a `press_buttons` that was a no-op / carried invalid buttons.
  (This is *malformed input*, distinct from an ineffective-but-valid move like
  bumping a wall — that shows up in the stuck-loop index instead.)
- **idle rate** — fraction of turns where the player's *tile* did not change.
  Directly captures "not moving much" — a weak navigator that fidgets in place
  (turning, mashing A, bumping walls) shows a high idle rate even when the
  screen keeps changing enough to fool the observation-hash stuck index.
- **tiles explored** — count of distinct (map, x, y) tiles visited.
- **no-tool-call rate** — fraction of turns the model emitted no tool call at
  all. A *subset* of `invalid_rate` (which also counts no-ops and malformed
  buttons), broken out because it is the only metric that separates "kept trying
  and failed" from "stopped trying". Added 2026-08-04 after the sonnet trace
  analysis: S1 seed0 (parked against a wall, still acting every turn) and seed1
  (believed it had already won, stopped acting) are opposite failures with
  near-identical `idle_rate`. See HANDOFF "Why sonnet scored 0/9".
- **ceiling-hit rate** — fraction of turns whose output hit `max_output_tokens`.
  Not a model-quality metric: it measures the *harness* clipping the model. Its
  job is to sit next to `no_tool_call_rate` so `metrics/validity.py` can tell
  truncation (a harness artifact — exclude the run) from a volitional stop (a
  finding — keep it). Those two look identical without it.
- **turns since new tile** — the "was it turn-limited?" diagnostic. Every
  failing seed reports `stop_reason: max_turns`, which conflates two opposite
  situations: sonnet S1 seed0 sat at `REDS_HOUSE_2F (5,1)` from turn 10 to
  turn 100 (more turns would have bought nothing) vs gemini S2 seed2, still
  finding new positions on turns 97-100 (genuinely still discovering ground
  when the cap hit). `None` for a successful run — the question does not
  apply, matching how `steps_to_success` handles the inverse case. Battle
  turns are excluded from both the seen-tile set and the elapsed count:
  `state.in_battle` is written unconditionally into every record, battles
  legitimately freeze position (the 500-turn S3 probes hit 28-54% battle
  turns), and a raw turn-distance count would read combat as stuckness — the
  same trap `ceiling_hit_rate` exists to keep `no_tool_call_rate` out of (see
  `metrics/validity.py`). This ships a raw number, not a verdict: no threshold
  here decides "frozen" vs "progressing" — that judgment is left to the
  reader, same as `idle_rate`.

`reasoning_provenance` (added 2026-08-05) is not a metric either: `reasoning`
alone reports `None` for two structurally different situations that both
matter to a reader — sonnet's legacy rows (`thinking` provably never sent) and
gpt's/gemini's rows (the vendor reasons by default and ignores our flag) — and
`_unanimous` in `metrics/results.py` cannot tell them apart. See
`_reasoning_provenance` below for the derivation.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from pokebench.replay import Run, find_stuck_segments

# Every model in the registry is configured with max_output_tokens=1024, and it
# is a frozen-contract constant (raising it invalidates every published row —
# see HANDOFF). Traces written before 2026-08-04 do not record it in meta.json,
# so scoring falls back to this and reports which value it used via
# `RunMetrics.max_output_tokens`. If a future sweep ever runs a model on a
# different ceiling, meta.json must carry it or the fallback will lie.
DEFAULT_MAX_OUTPUT_TOKENS = 1024

# Only the Anthropic adapter conditions its request on `ModelConfig.reasoning`
# (`agents/anthropic.py`: `if getattr(self.config, "reasoning", False): kwargs
# ["thinking"] = ...`). The OpenAI, Google and Ollama adapters never read the
# attribute at all -- each vendor reasons (or not) by its own default,
# regardless of what the harness happens to record. This set exists so
# `_reasoning_provenance` can tell "the flag was in effect and off" apart from
# "the flag never applied here" when meta.json lacks the key. Verified by grep
# across `agents/`: `self.config.reasoning` appears in exactly one adapter.
_PROVIDERS_WHERE_REASONING_BINDS = frozenset({"anthropic"})


def _reasoning_provenance(provider: str | None, reasoning: bool | None, recorded: bool) -> str:
    """Classify what `reasoning=None` actually means for one run.

    `reasoning` alone collapses two different situations into the same `None`:
    a legacy Anthropic trace that predates the `reasoning` key in meta.json
    (thinking was *provably* never sent -- `getattr(self.config, "reasoning",
    False)`'s default is literally `False`, so an absent attribute cannot have
    produced a `thinking` request) and a non-Anthropic trace where the flag
    never bound in the first place. Both look identical through `reasoning`;
    this is the field that tells them apart without guessing at anything not
    already true of the code.

        "recorded_on" / "recorded_off"  meta.json has the key; trust it
        "off_unrecorded"                key absent, but this provider's
                                         adapter *would* have acted on it, so
                                         its absence proves reasoning was off
        "not_applicable"                key absent, and this provider's
                                         adapter never reads the flag anyway --
                                         its absence says nothing
        "unknown"                       not even the provider is known
    """
    # Bindingness FIRST. `runner/loop.py` writes `meta["reasoning"]` for every
    # provider unconditionally (`ModelConfig.reasoning` defaults to `True`, and
    # nothing gates the write on provider), so `recorded` is true for
    # essentially every trace regardless of provider -- checking it first would
    # report `recorded_on`/`recorded_off` for providers whose adapter never
    # reads the flag at all, asserting a causal claim about deliberation the
    # trace does not support. Only a provider where the flag actually binds may
    # have its recorded value trusted.
    if provider not in _PROVIDERS_WHERE_REASONING_BINDS:
        return "not_applicable" if provider else "unknown"
    if recorded:
        return "recorded_on" if reasoning else "recorded_off"
    return "off_unrecorded"


@dataclass(frozen=True)
class RunMetrics:
    scenario: str
    model: str
    tier: int
    success: bool
    stop_reason: str
    turns: int
    steps_to_success: int | None
    input_tokens: int
    output_tokens: int
    cost_usd: float
    stuck_index: float  # 0..1, fraction of turns in a stuck-loop (screen-based)
    invalid_rate: float  # 0..1, fraction of malformed/no-op turns
    idle_rate: float  # 0..1, fraction of turns where the tile did not change
    tiles_explored: int  # distinct (map, x, y) tiles visited
    no_tool_call_rate: float  # 0..1, fraction of turns with no tool call at all
    ceiling_hit_rate: float  # 0..1, fraction of turns whose output hit the ceiling
    # Non-battle turns elapsed since the last genuinely new (map_id, x, y)
    # tile, measured at the final turn. None for a successful run (see
    # `_turns_since_new_tile`). A raw number, not a verdict — see the
    # module docstring for how to read it.
    turns_since_new_tile: int | None
    # --- Provenance -------------------------------------------------------
    # Not metrics: the run conditions a reader needs to know before comparing
    # two rows. `cap_turns` separates M3's turn-matched sweep (100 for every
    # model) from M2's fixed-budget baseline (300/400/600, dollar-bound).
    # Comparing across a difference in it is comparing harnesses, not models.
    #
    # `reasoning` is the ModelConfig flag as recorded in meta.json, and it is
    # narrower than it looks: it only binds on the Anthropic adapter (OpenAI
    # and Google reason by default and ignore it). **None means "not recorded"**
    # — traces predating the 2026-07-21 policy have no such key — so a null here
    # is not a claim that the model did not deliberate. Read it with the
    # provider in mind, never on its own.
    cap_turns: int | None = None
    reasoning: bool | None = None
    # The ceiling `ceiling_hit_rate` was measured against, and whether it came
    # from the trace or from the fallback. `assumed` means meta.json did not
    # record it — the rate is still computed, but a validity verdict that turns
    # on it is resting on an assumption, so it is reported rather than hidden.
    max_output_tokens: int | None = None
    max_output_tokens_source: str = "assumed"  # "meta" | "assumed"
    # What `reasoning=None` means for this specific run -- see
    # `_reasoning_provenance`. Additive (2026-08-05): existing consumers of
    # `reasoning` are unaffected; this only adds information for the ones that
    # need to tell "off" apart from "not applicable".
    reasoning_provenance: str = "unknown"

    def to_dict(self) -> dict:
        return asdict(self)


def _is_invalid_turn(record: dict) -> bool:
    if record.get("tool_call") is None:
        return True  # the model failed to act at all
    action = record.get("action")
    return bool(action and (action.get("is_noop") or action.get("invalid")))


def _has_no_tool_call(record: dict) -> bool:
    """The model spoke but never acted. Deliberately narrower than
    `_is_invalid_turn`: a no-op or malformed press is a *failed* action, this is
    the *absence* of one."""
    return record.get("tool_call") is None


def _positions(records: list[dict]) -> list[tuple]:
    return [
        (r.get("state", {}).get("map_id"), r.get("state", {}).get("x"), r.get("state", {}).get("y"))
        for r in records
    ]


def _turns_since_new_tile(records: list[dict], stop_reason: str) -> int | None:
    """Non-battle turns elapsed since the last (map_id, x, y) tile that had
    never been visited before, measured at the final turn.

    `None` when `stop_reason == "success"` — mirrors `steps_to_success`'s
    handling of the inverse case: "was it still discovering ground at the
    cap" is not a question a successful run answers.

    Battle turns are dropped BEFORE either the seen-tile set or the elapsed
    count is built, not just filtered out of the set. Dropping them only from
    the set and still counting them toward "how long ago" would silently
    reintroduce the bug this metric exists to avoid: a run that found new
    ground right before a 45-turn battle and right after it ends would have
    its elapsed count inflated by the battle's length, reading as frozen when
    it was mid-fight. Counting only non-battle turns on both sides is what
    keeps a battle-heavy-but-progressing run from misreading as stuck.
    """
    if stop_reason == "success":
        return None
    non_battle = [r for r in records if not r.get("state", {}).get("in_battle")]
    if not non_battle:
        return None
    seen: set[tuple] = set()
    last_new_index = 0
    for i, r in enumerate(non_battle):
        st = r.get("state", {})
        pos = (st.get("map_id"), st.get("x"), st.get("y"))
        if pos not in seen:
            seen.add(pos)
            last_new_index = i
    return len(non_battle) - 1 - last_new_index


def score_run(run: Run, stuck_min_len: int = 3) -> RunMetrics:
    records = run.records
    turns = len(records)
    summary = run.summary or {}
    meta = run.meta or {}

    success = bool(summary["success"]) if "success" in summary else any(
        r.get("success") for r in records
    )
    stop_reason = summary.get("stop_reason", "?")

    stuck_turns = sum(count for _, _, count, _ in find_stuck_segments(records, stuck_min_len))
    invalid_turns = sum(1 for r in records if _is_invalid_turn(r))

    input_tokens = sum(r.get("usage", {}).get("input_tokens", 0) for r in records)
    output_tokens = sum(r.get("usage", {}).get("output_tokens", 0) for r in records)
    cost = sum(r.get("cost_usd", 0.0) for r in records)

    positions = _positions(records)
    moves = sum(1 for a, b in zip(positions, positions[1:], strict=False) if a != b)
    transitions = len(positions) - 1
    idle_rate = round(1 - moves / transitions, 4) if transitions > 0 else 0.0

    recorded_ceiling = meta.get("max_output_tokens")
    ceiling = recorded_ceiling or DEFAULT_MAX_OUTPUT_TOKENS
    ceiling_hits = sum(
        1 for r in records if r.get("usage", {}).get("output_tokens", 0) >= ceiling
    )
    no_tool_calls = sum(1 for r in records if _has_no_tool_call(r))

    recorded_reasoning = "reasoning" in meta

    return RunMetrics(
        scenario=meta.get("scenario") or "?",
        model=meta.get("model") or "?",
        tier=meta.get("tier", 0),
        success=success,
        stop_reason=stop_reason,
        turns=turns,
        steps_to_success=turns if success else None,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=round(cost, 6),
        stuck_index=round(stuck_turns / turns, 4) if turns else 0.0,
        invalid_rate=round(invalid_turns / turns, 4) if turns else 0.0,
        idle_rate=idle_rate,
        tiles_explored=len(set(positions)),
        no_tool_call_rate=round(no_tool_calls / turns, 4) if turns else 0.0,
        ceiling_hit_rate=round(ceiling_hits / turns, 4) if turns else 0.0,
        turns_since_new_tile=_turns_since_new_tile(records, stop_reason),
        cap_turns=(meta.get("caps") or {}).get("max_turns"),
        reasoning=meta.get("reasoning"),
        max_output_tokens=ceiling,
        max_output_tokens_source="meta" if recorded_ceiling else "assumed",
        reasoning_provenance=_reasoning_provenance(
            meta.get("provider"), meta.get("reasoning"), recorded_reasoning
        ),
    )
