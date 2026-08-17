"""Deterministic navigation primitives for the M0 spike (no LLM involved).

All functions take a `Walker` — anything with .state()/.dpad()/.settle() —
so the logic is fully unit-testable against a fake grid world.

Strategies:
- walk_until: walk one direction until blocked / map change / predicate / cap.
- greedy_until_map_change: alternate two directions wall-hugging toward a
  corner warp (stairs, door mats) until the map changes.
- run_route: execute a data-driven route (from a scenario YAML) across maps.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from pokebench.harness.state import GameState

Logger = Callable[[str], None]


class Walker(Protocol):
    def state(self) -> GameState: ...
    def dpad(self, direction: str) -> None: ...
    def settle(self, frames: int) -> None: ...
    def tap(self, button: str) -> None: ...


class RouteError(RuntimeError):
    def __init__(self, message: str, state: GameState):
        super().__init__(f"{message} [at {state.summary()}]")
        self.state = state


@dataclass
class StepResult:
    moved: bool
    map_changed: bool
    state: GameState


def step(w: Walker, direction: str) -> StepResult:
    """Attempt one tile step; detect blocked vs moved vs map transition."""
    before = w.state()
    w.dpad(direction)
    after = w.state()
    map_changed = after.map_id != before.map_id
    if map_changed:
        w.settle(90)  # let the warp/connection fade-in finish
        after = w.state()
    moved = map_changed or after.pos != before.pos
    return StepResult(moved=moved, map_changed=map_changed, state=after)


def walk_until(
    w: Walker,
    direction: str,
    stop: Callable[[GameState], bool] | None = None,
    max_steps: int = 64,
    log: Logger | None = None,
) -> tuple[str, GameState]:
    """Walk one direction until: predicate ("stop"), map change ("map"),
    blocked ("blocked"), or step cap ("max")."""
    state = w.state()
    if stop and stop(state):
        return ("stop", state)
    for _ in range(max_steps):
        r = step(w, direction)
        if log:
            log(f"  {direction:<5} -> {r.state.map_name} {r.state.pos} moved={r.moved}")
        if r.map_changed:
            return ("map", r.state)
        if stop and stop(r.state):
            return ("stop", r.state)
        if not r.moved:
            return ("blocked", r.state)
        state = r.state
    return ("max", state)


def greedy_until_map_change(
    w: Walker,
    dirs: tuple[str, str],
    max_cycles: int = 12,
    log: Logger | None = None,
) -> GameState:
    """Wall-hug toward a corner by alternating two directions until the map
    changes (walk-on warps: stairs, door mats, map edges). Raises if stuck."""
    for _ in range(max_cycles):
        progress = 0
        for d in dirs:
            start = w.state().pos
            reason, state = walk_until(w, d, log=log)
            if reason == "map":
                return state
            progress += abs(state.x - start[0]) + abs(state.y - start[1])
        if progress == 0:
            raise RouteError(f"greedy {dirs} made no progress", w.state())
    raise RouteError(f"greedy {dirs} hit the cycle cap", w.state())


def _make_stop(until: dict) -> Callable[[GameState], bool]:
    def check(s: GameState) -> bool:
        ok = True
        if "map" in until:
            ok = ok and s.map_name == until["map"]
        if "x_gte" in until:
            ok = ok and s.x >= until["x_gte"]
        if "x_lte" in until:
            ok = ok and s.x <= until["x_lte"]
        if "y_gte" in until:
            ok = ok and s.y >= until["y_gte"]
        if "y_lte" in until:
            ok = ok and s.y <= until["y_lte"]
        return ok

    return check


def run_leg(
    w: Walker,
    direction: str,
    until: dict,
    slide: str | None = None,
    max_steps: int = 64,
    max_slides: int = 6,
    log: Logger | None = None,
) -> GameState:
    """Walk toward a coordinate/map target; on obstacles, optionally sidestep
    once in the `slide` direction and resume (handles signs/NPCs in the path)."""
    stop = _make_stop(until)
    slides = 0
    while True:
        reason, state = walk_until(w, direction, stop=stop, max_steps=max_steps, log=log)
        if reason in ("stop", "map"):
            return state
        if reason == "blocked" and slide and slides < max_slides:
            slides += 1
            if log:
                log(f"  blocked; sliding {slide} ({slides}/{max_slides})")
            r = step(w, slide)
            if not r.moved:
                raise RouteError(f"leg {direction} blocked and slide {slide} blocked", r.state)
            continue
        raise RouteError(f"leg {direction} until {until} failed ({reason})", state)


class EncounterAwareWalker:
    """Wraps a `Walker` (+ `tap`) so mid-route encounters and bump-text are
    transparent to route logic. Sits ENTIRELY OUTSIDE `step`/`walk_until`/
    `run_leg`/`greedy_until_map_change`/`run_route` -- none of those pinned
    functions (or their existing tests) change at all; this only wraps
    `.dpad()`, which is the one seam they all call through.

    Two encounter hazards found on the 2026-08-05 live probe, encoded here so
    they are not rediscovered:

    1. **Always prefer RUN, never FIGHT** for a wild encounter (`in_battle ==
       1`). FIGHT is what produced the observed "0 PP, can't select this
       move" stall after the party wore down to 6/26 HP. RUN is
       probabilistic and can fail, so escaping is a bounded retry loop that
       raises `RouteError` loudly on exhaustion rather than looping forever.
       The real, ROM-verified "select RUN" button macro belongs in the
       `escape` callback (injected at the Stage C/D call site) -- this class
       never hardcodes a macro, and the default below is a placeholder that
       only needs to satisfy the fake world's abstract "one tap = one
       attempt" contract.
    2. **Trainer battles (`in_battle == 2`) cannot be fled at all** -- gen-1
       has no "run from trainer" option. This is deliberately NOT
       special-cased: the same bounded retry loop below simply exhausts and
       raises `RouteError`. Recorded as a known, intentionally-unresolved
       edge case (forest trainers are rare but possible) rather than
       pretended away.

    A third, unrelated hazard lives here because it surfaces at the same
    seam (a `dpad()` call that produced no movement): sign/NPC bump-text is
    dismissed with **B, never A** -- A re-triggers the same text from the
    top, so a naive "retry with A" loop never terminates. Since "text box
    open" and "genuinely blocked" look identical from position+facing alone,
    every unmoved `dpad()` retries (tap B, `dpad()` again) up to
    `max_unmoved_retries` times before being reported to the caller as a
    real block.

    **Fourth hazard, found and CALIBRATED live against the ROM (2026-08-06,
    Task 1 of the resumed session), refining a "race" the prior session
    flagged but couldn't pin down.** A trainer's sight-trigger is NOT
    instantaneous: the "!" spotted indicator appears immediately, but the
    trainer's walk-up + forced dialogue ("Hey, wait up! What's the hurry?")
    takes several MORE `dpad()`-sized time slices to play out before
    `in_battle` actually sets -- and every one of those slices reads as
    `pos unchanged, in_battle == 0`, i.e. indistinguishable from a wall,
    exactly like hazard #2's freeze but stretched across MULTIPLE calls
    instead of one. Calibrated once, deterministically, against
    `scenarios/states/s3_forest.state` at `VIRIDIAN_FOREST (1,18)`: **exactly
    5 raw `dpad()` calls** (each followed by a `tap("b")`, matching this
    class's own retry pattern) before `in_battle` flips to `2` (trainer).
    The OLD single-retry design (2 raw `dpad()` calls total) stops looking
    two calls short and reports a false wall -- which is exactly what
    happened: the stale `tools/_debug_edges.json` this session inherited
    recorded `(1,18)`'s `up` AND `down` as `blocked`, and a live re-test
    proved both are actually this same inescapable trainer, not a wall (see
    HANDOFF.md, "Task 1/2 — the race, recalibrated" for the full trace).
    **Only ONE such case has ever been found** (not a general race across
    ~4,500 probed `dpad()` calls covering ~40 wild encounters, all of which
    resolved within a single call -- see HANDOFF for that evidence too), but
    since it silently produces a WRONG geometry fact rather than a loud
    failure, the default below is deliberately calibrated to that one
    measured case plus a one-call safety margin, not a guess.

    Widening the retry bound costs real wall-clock ONLY on the "unmoved"
    branch (a successful move is completely unaffected) -- but that branch
    includes every genuine wall bump, which dominates maze exploration.
    Measured trade-off: a genuine wall now costs up to 6 raw `dpad()` calls
    instead of 2 (~3x), which is a real but small cost against a one-off,
    $0, offline research probe -- not a per-agent-turn cost (this class is
    still never wired into `harness/tools.py`; see the class docstring's
    opening paragraph).
    """

    def __init__(
        self,
        inner,
        max_escape_attempts: int = 10,
        max_unmoved_retries: int = 5,
        escape: Callable[[object], None] | None = None,
    ):
        self.inner = inner
        self.max_escape_attempts = max_escape_attempts
        self.max_unmoved_retries = max_unmoved_retries
        # Placeholder for the fake world / until Stage D injects the real,
        # ROM-verified RUN macro -- never wire this default to FIGHT.
        self.escape = escape or (lambda w: w.tap("a"))

    def state(self) -> GameState:
        return self.inner.state()

    def settle(self, frames: int) -> None:
        self.inner.settle(frames)

    def tap(self, button: str) -> None:
        self.inner.tap(button)

    def _resolve_battle(self) -> None:
        attempts = 0
        while self.inner.state().in_battle:
            if attempts >= self.max_escape_attempts:
                raise RouteError(
                    "could not clear the encounter (RUN exhausted, or a trainer "
                    "battle -- gen-1 trainers cannot be fled)",
                    self.inner.state(),
                )
            self.escape(self.inner)
            attempts += 1

    def dpad(self, direction: str) -> None:
        self._resolve_battle()  # defensive: should already be clear on entry
        before = self.inner.state()
        self.inner.dpad(direction)
        self._resolve_battle()  # an encounter may have started INSTEAD of moving
        after = self.inner.state()
        retries = 0
        while (
            after.pos == before.pos
            and after.map_id == before.map_id
            and retries < self.max_unmoved_retries
        ):
            # Unmoved: a genuine wall, a battle just absorbed above (this
            # retry recovers the tile press the caller actually asked for),
            # unread bump-text still open, or (calibrated 2026-08-06, see the
            # class docstring) a trainer sight-trigger still mid-approach --
            # `in_battle` can lag several calls behind the freeze, not just
            # one. B dismisses text/dialogue without re-triggering it from
            # the top -- never A. Looping bounds the cost of a genuine wall
            # (which never resolves and always spends the full budget)
            # instead of guessing a single retry is always enough.
            self.inner.tap("b")
            self.inner.dpad(direction)
            self._resolve_battle()
            after = self.inner.state()
            retries += 1


class WildRunEscaper:
    """The real, ROM-verified "select RUN" battle macro -- promoted here
    (Task C, 2026-08-11) from `tools/probe_forest_route.py`'s `ForestEscaper`,
    where it was originally observed live (2026-08-06) against a Viridian
    Forest wild encounter. It is a property of the gen-1 battle engine's
    FIGHT/PKMN/ITEM/RUN menu, not of any one location, so it applies
    unchanged to any scenario whose route crosses tall grass.

    **Why this lives in `harness/navigate.py` and not only in `tools/`:**
    `EncounterAwareWalker`'s own docstring says the macro "belongs in the
    escape callback (injected at the Stage C/D call site)" -- that call site
    is `cmd_spike_s1` (`cli.py`), which is shipped package code and cannot
    import from `tools/` (a non-package directory of one-off research
    scripts). Its default `escape` is a placeholder (`tap("a")`, which on the
    default FIGHT-selected cursor would repeatedly attempt to FIGHT, not
    RUN) that was never exercised end-to-end before this, because S1's route
    is entirely indoors/town with no wild-encounter grass. **S2's Route 1 leg
    does cross grass**, discovered live 2026-08-11 when the FIRST real
    `pokebench spike-s1 --scenario s2_viridian_pokecenter.yaml` run hit an
    encounter three steps in and failed with the placeholder -- see
    `tools/probe_s2_route.py`'s module docstring for the full story. Same
    macro, same observed frame timings as `ForestEscaper`; not re-derived.

    Not general gen-1 battle AI -- exactly like `ForestEscaper`, it only
    handles what was actually observed: a wild-encounter intro followed by
    the standard 2x2 cursor menu (FIGHT top-left, RUN bottom-right), and it
    always chooses RUN, never FIGHT (`EncounterAwareWalker`'s documented
    hazard #1: FIGHT is what produced an earlier live HP/PP stall). Trainer
    battles (`in_battle == 2`) cannot be fled at all in gen-1 and are not
    special-cased here either -- `EncounterAwareWalker._resolve_battle`
    exhausts its retry budget and raises `RouteError` loudly, which is the
    correct outcome for M0-tier oracle code (no combat-solving logic, per
    CLAUDE.md's non-goals).
    """

    def __init__(self) -> None:
        self._mid_battle = False

    def __call__(self, emu) -> None:
        if not self._mid_battle:
            emu.settle(600)  # let "Wild X appeared!" finish printing
            emu.tap("a")  # dismiss it -> auto send-out animation plays
            emu.settle(480)  # ...finishes at the FIGHT/PKMN/ITEM/RUN menu
            self._mid_battle = True
        emu.tap("down")  # FIGHT -> ITEM
        emu.tap("right")  # ITEM -> RUN (idempotent if already there)
        emu.tap("a")  # confirm RUN
        emu.settle(180)  # "Got away safely!" / "Can't escape!" text
        if emu.state().in_battle:
            emu.tap("a")  # dismiss the outcome text
            emu.settle(120)
        if not emu.state().in_battle:
            self._mid_battle = False  # ready to prime fresh on the next battle


# Minimum settle between menu taps in a "buttons" macro (S4, 2026-08-15).
# Verified live against the real ROM: gen-1's per-character text-typing
# animation drops an unsettled tap outright -- the identical 7-tap Viridian
# Mart purchase macro (see s4_viridian_mart.yaml) with NO settle between taps
# never completed the purchase at all (money unchanged through all 7 taps);
# with 45 frames between each, it completed identically on two independent
# runs. Not the frozen per-turn agent-loop settle (`POST_ACTION_SETTLE_
# FRAMES` in harness/tools.py, 24 frames) -- this is oracle-only scripted
# replay, a different call site with a different (larger, measured) need.
BUTTON_MACRO_SETTLE_FRAMES = 45


def run_route(w: Walker, route: list[dict], log: Logger | None = None) -> GameState:
    """Execute a scenario route: a list of per-map phases.

    Phases whose map doesn't match the current map are skipped, so a route can
    be resumed from any point along the way (e.g. a state saved outdoors).
    """
    state = w.state()
    for phase in route:
        if state.map_name != phase["phase"]:
            if log:
                log(f"skipping phase {phase['phase']} (currently in {state.map_name})")
            continue
        if log:
            log(f"phase {phase['phase']} [{phase['strategy']}] from {state.pos}")
        if phase["strategy"] == "greedy":
            state = greedy_until_map_change(w, tuple(phase["dirs"]), log=log)
        elif phase["strategy"] == "legs":
            for leg in phase["legs"]:
                state = run_leg(
                    w,
                    leg["dir"],
                    leg["until"],
                    slide=leg.get("slide"),
                    log=log,
                )
                if state.map_name != phase["phase"]:
                    break  # left this map; move on to the next phase
        elif phase["strategy"] == "steps":
            # Literal direction-list replay: for a route that is one exact
            # (e.g. BFS-shortest) path through a maze, neither "greedy"
            # (wall-hugging) nor "legs" (predicate-until) can reproduce it --
            # this just plays the recorded steps back, failing loud and
            # immediately (matching this file's existing fail-loud
            # convention) the instant one doesn't move as expected, rather
            # than silently drifting off the verified path.
            for direction in phase["steps"]:
                r = step(w, direction)
                if log:
                    log(f"  {direction:<5} -> {r.state.map_name} {r.state.pos} moved={r.moved}")
                if not r.moved:
                    raise RouteError(f"step {direction} did not move", r.state)
                state = r.state
                if state.map_name != phase["phase"]:
                    break  # left this map; move on to the next phase
        elif phase["strategy"] == "buttons":
            # A raw menu-button macro (S4's Mart purchase), NOT movement --
            # `step()`'s "moved" contract doesn't apply to a/b/start/select,
            # so this deliberately does NOT use step() or raise on an
            # unmoved player (every tap in a menu macro leaves position
            # unchanged; that would make "steps"'s fail-loud check reject
            # every real menu interaction). Each tap gets its own settle:
            # see BUTTON_MACRO_SETTLE_FRAMES for why an unsettled tap is
            # silently dropped by gen-1's text-typing animation.
            settle_frames = phase.get("settle_frames", BUTTON_MACRO_SETTLE_FRAMES)
            for button in phase["buttons"]:
                w.tap(button)
                w.settle(settle_frames)
                state = w.state()
                if log:
                    log(f"  tap({button!r}) -> {state.summary()}")
                if state.map_name != phase["phase"]:
                    break  # left this map; move on to the next phase
        else:
            raise ValueError(f"unknown strategy {phase['strategy']!r}")
    return state
