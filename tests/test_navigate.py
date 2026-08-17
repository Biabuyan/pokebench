from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from pokebench.harness.navigate import (
    EncounterAwareWalker,
    RouteError,
    WildRunEscaper,
    greedy_until_map_change,
    run_leg,
    run_route,
    step,
    walk_until,
)
from pokebench.runner.loop import make_success_check
from tests.fake_world import (
    FakeWalker,
    Room,
    build_forest_north_gate_world,
    build_forest_world,
    build_pallet_world,
    build_route1_to_pokecenter_world,
    build_s4_mart_world,
)

SCENARIO = Path(__file__).parent.parent / "scenarios" / "s1_exit_pallet.yaml"
S2_SCENARIO = Path(__file__).parent.parent / "scenarios" / "s2_viridian_pokecenter.yaml"


def test_step_moves_and_blocks():
    w = build_pallet_world()
    r = step(w, "up")  # bed directly above the start tile
    assert not r.moved
    r = step(w, "right")
    assert r.moved and not r.map_changed


def test_walk_until_blocked_at_wall():
    w = build_pallet_world()
    reason, state = walk_until(w, "right")  # TV/SNES at (6,6)
    assert reason == "blocked"
    assert state.pos == (5, 6)


def test_walk_until_stop_predicate():
    w = build_pallet_world()
    reason, state = walk_until(w, "down", stop=lambda s: s.y >= 7)
    assert reason == "stop"
    assert state.y == 7


def test_walk_until_max_steps():
    rooms = {"ROUTE_1": Room(grid=["#" * 40, "#" + "." * 38 + "#", "#" * 40])}
    w = FakeWalker(rooms, "ROUTE_1", (1, 1))
    reason, _ = walk_until(w, "right", max_steps=5)
    assert reason == "max"


def test_greedy_reaches_stairs_warp():
    w = build_pallet_world()
    state = greedy_until_map_change(w, ("up", "right"))
    assert state.map_name == "REDS_HOUSE_1F"
    assert state.pos == (7, 1)


def test_greedy_stuck_raises():
    sealed = Room(grid=["###", "#.#", "###"])
    w = FakeWalker({"PALLET_TOWN": sealed}, "PALLET_TOWN", (1, 1))
    with pytest.raises(RouteError, match="no progress"):
        greedy_until_map_change(w, ("up", "right"))


def test_door_mats_do_not_warp_on_crossing():
    """Regression for the first real run: gen-1 door mats are collision warps.
    Walking sideways across them must NOT exit; pressing down off-mat must not
    exit either; pressing down ON the mat must."""
    w = build_pallet_world()
    w.map, w.pos = "REDS_HOUSE_1F", (5, 7)
    reason, state = walk_until(w, "left")  # crosses both mats to the west wall
    assert reason == "blocked"
    assert state.map_name == "REDS_HOUSE_1F"
    assert state.pos == (0, 7)
    r = step(w, "down")  # the corner the real run wedged into
    assert not r.moved and not r.map_changed
    w.pos = (2, 7)
    r = step(w, "down")  # on the mat: collision warp fires
    assert r.map_changed
    assert r.state.map_name == "PALLET_TOWN"


def _obstacle_corridor() -> FakeWalker:
    # a sign at (4,1) blocks the direct row; sliding up detours around it
    room = Room(
        grid=[
            "..........",
            "....#.....",
            "..........",
        ]
    )
    return FakeWalker({"ROUTE_1": room}, "ROUTE_1", (1, 1))


def test_run_leg_slides_around_obstacle():
    w = _obstacle_corridor()
    state = run_leg(w, "right", {"x_gte": 8}, slide="up")
    assert state.x >= 8


def test_run_leg_fails_without_slide():
    w = _obstacle_corridor()
    with pytest.raises(RouteError):
        run_leg(w, "right", {"x_gte": 8}, slide=None)


def test_slide_left_recovers_from_overshoot():
    """Regression for run 2: overshooting to (11,6) put us against Blue's west
    wall; sliding LEFT regains the corridor and still reaches Route 1.
    (Sliding right walked into Blue's front door.)"""
    w = build_pallet_world()
    w.map, w.pos = "PALLET_TOWN", (11, 6)
    state = run_leg(w, "up", {"map": "ROUTE_1"}, slide="left")
    assert state.map_name == "ROUTE_1"


def test_blues_door_is_entry_warp():
    """Documents the trap from run 2: stepping onto (13,5) enters Blue's house."""
    w = build_pallet_world()
    w.map, w.pos = "PALLET_TOWN", (13, 6)
    r = step(w, "up")
    assert r.map_changed
    assert r.state.map_name == "BLUES_HOUSE"


def test_full_s1_route_from_shipped_yaml():
    """End-to-end: the actual scenarios/s1_exit_pallet.yaml route exits
    Pallet Town in the fake world (gen-1 warp semantics included). Only
    real-game tile geometry beyond Red's house remains untested."""
    scenario = yaml.safe_load(SCENARIO.read_text(encoding="utf-8"))
    w = build_pallet_world()
    final = run_route(w, scenario["spike_route"])
    assert final.map_name == scenario["success"]["map"] == "ROUTE_1"


def test_full_s2_route_from_shipped_yaml():
    """End-to-end: the actual scenarios/s2_viridian_pokecenter.yaml route
    (Task C, 2026-08-11) reaches the Pokécenter in the fake world, mirroring
    `build_route1_to_pokecenter_world()`'s ROM-verified geometry -- the
    collision warp at ROUTE_1 (11,0) and the entry warp at
    VIRIDIAN_CITY (23,25). The fake world models no wild-encounter grass, so
    this exercises the pure navigation geometry, not `WildRunEscaper` (see
    the dedicated tests for that below -- the real route DID need it live)."""
    scenario = yaml.safe_load(S2_SCENARIO.read_text(encoding="utf-8"))
    w = build_route1_to_pokecenter_world()
    final = run_route(w, scenario["spike_route"])
    assert final.map_name == scenario["success"]["map"] == "VIRIDIAN_POKECENTER"
    assert final.pos == (3, 7)  # the interior's own local landing coordinate


# --- WildRunEscaper: the real RUN macro cmd_spike_s1 now injects ----------
#
# S1 never needed this (its route is entirely indoors/town, no wild-encounter
# grass); S2's Route 1 leg does, discovered live 2026-08-11 when the first
# real `pokebench spike-s1` run against S2 hit an encounter and failed with
# the class's `tap("a")` placeholder (see `WildRunEscaper`'s docstring and
# `cmd_spike_s1`'s call site in cli.py). These pin the exact button/frame
# sequence against a spy target, not a real emulator, so no ROM is needed;
# the sequence itself IS ROM-verified (this macro is unchanged from the one
# `tools/probe_forest_route.py`'s `observe_run_macro` observed live 2026-08-06,
# and re-confirmed live for S2 by the two `pokebench spike-s1` PASS runs
# whose trace.jsonl shows this exact tap sequence at both encounters).


class _RunSpy:
    """Records every tap/settle call. Models a SUCCESSFUL run: `in_battle`
    clears only after the SECOND settle (120 frames, following the outcome-
    text dismiss tap) -- matching the observed mechanic (`WildRunEscaper`'s
    docstring: "one more A dismisses it and in_battle clears"), not the
    settle(180) right after confirming RUN."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.in_battle = 1

    def tap(self, button):
        self.calls.append(("tap", button))

    def settle(self, frames):
        self.calls.append(("settle", frames))
        if frames == 120:
            self.in_battle = 0

    def state(self):
        return SimpleNamespace(in_battle=self.in_battle)


def test_wild_run_escaper_plays_the_observed_menu_macro_and_clears_a_successful_run():
    spy = _RunSpy()
    escaper = WildRunEscaper()
    escaper(spy)
    assert spy.calls == [
        ("settle", 600),  # "Wild X appeared!" finishes printing
        ("tap", "a"),  # dismiss -> send-out animation
        ("settle", 480),  # ...lands on the FIGHT/PKMN/ITEM/RUN menu
        ("tap", "down"),  # FIGHT -> ITEM
        ("tap", "right"),  # ITEM -> RUN
        ("tap", "a"),  # confirm RUN
        ("settle", 180),  # "Got away safely!" text
        ("tap", "a"),  # dismiss the outcome text
        ("settle", 120),
    ]
    assert spy.in_battle == 0
    assert escaper._mid_battle is False  # ready to prime fresh on the next battle


class _StubbornRunSpy:
    """Never clears `in_battle` -- simulates a failed RUN attempt, exactly
    what drives `EncounterAwareWalker._resolve_battle`'s retry loop."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.in_battle = 1

    def tap(self, button):
        self.calls.append(("tap", button))

    def settle(self, frames):
        self.calls.append(("settle", frames))

    def state(self):
        return SimpleNamespace(in_battle=self.in_battle)


def test_wild_run_escaper_does_not_replay_the_intro_on_a_retry_within_one_battle():
    spy = _StubbornRunSpy()
    escaper = WildRunEscaper()
    escaper(spy)  # first attempt: primes the intro
    n = len(spy.calls)
    escaper(spy)  # second attempt, same battle (failed run) -- no re-priming
    second_batch = spy.calls[n:]
    assert ("settle", 600) not in second_batch  # the intro wait is not replayed
    assert second_batch[0] == ("tap", "down")  # straight to cursor navigation
    assert escaper._mid_battle is True  # still mid-battle; caller will retry


def test_route2_up_is_blocked_at_forest_approach():
    """Regression for the 2026-08-05 500-turn probe (gemini trace, turns
    394-395): 9 consecutive "up" presses from ROUTE_2 (2,44), across two
    turns, produced ZERO net y movement and no warp -- contradicting this
    fixture's original claim that a second "up" from here fires the Viridian
    Forest south-gate warp. See `build_forest_world()`'s docstring for
    exactly what the trace does, and does not, establish."""
    w = build_forest_world()
    for _ in range(9):
        r = step(w, "up")
        assert not r.moved and not r.map_changed
    assert w.pos == (2, 44)


def test_south_gate_interior_corridor_is_open_and_does_not_warp_on_entry():
    """Regression for the same probe, turn 397: the gate's interior corridor
    from (4,7) to (4,1) is unobstructed floor (RAM-verified: x held at 4, y
    dropped by exactly 6 over 6 "up" presses), and landing on (4,1) does NOT
    itself fire a warp -- turn 397 ends still on VIRIDIAN_FOREST_SOUTH_GATE,
    contradicting this fixture's original "fires on entry" claim for that
    tile. The real exit warp's tile/mechanism is unverified and deliberately
    not modeled -- see `build_forest_world()`."""
    w = build_forest_world()
    w.map, w.pos = "VIRIDIAN_FOREST_SOUTH_GATE", (4, 7)
    for _ in range(6):
        r = step(w, "up")
        assert r.moved and not r.map_changed
    assert w.pos == (4, 1)
    assert w.map == "VIRIDIAN_FOREST_SOUTH_GATE"  # landing here does not warp


def test_north_gate_exists_and_connects_forest_to_route2():
    """RETRACTED 2026-08-06 (same day, a later pass) -- this only pins
    `build_forest_north_gate_world()`'s OWN internal (hand-authored, no
    longer trusted) fixture logic, not a live-verified fact about the real
    ROM. See that function's docstring (READ ITS RETRACTION NOTE FIRST) and
    HANDOFF.md, "Task 1/2/3 -- the race, recalibrated, and the north-gate
    claim retracted", for why. Kept passing deliberately (it still correctly
    describes what the fixture below does) -- it is the FIXTURE's claim, not
    this test, that turned out to be wrong.

    Original (now-retracted) docstring, kept for the record: "The project's
    biggest open question until 2026-08-06 (Task B): does
    VIRIDIAN_FOREST_NORTH_GATE exist and is it reachable? Answered live via a
    644-tile `Emulator.dpad()` frontier probe from the S3 anchor; this pins
    the discovered corridor + both warps end to end... (this is NOT the S3
    scenario's own full route -- the real anchor is (14,42), 133 dpad()
    calls south of this corridor's start)." -- a corrected, exhaustive
    re-probe could not reproduce any of that."""
    w = build_forest_north_gate_world()
    assert w.map == "VIRIDIAN_FOREST"
    assert w.pos == (0, 15)  # local x=0 == real verified x=1

    for _ in range(14):  # walk the real, verified-open (1, 15)..(1, 2) stretch
        r = step(w, "up")
        assert r.moved and not r.map_changed
    assert w.pos == (0, 1)  # real (1, 1): the warp tile itself

    r = step(w, "up")  # pressing further fires the collision warp
    assert r.map_changed
    assert w.map == "VIRIDIAN_FOREST_NORTH_GATE"
    assert w.pos == (1, 0)  # real, RAM-verified landing tile

    # The (1,0) -> (1,1) connection was never itself dpad()-tested (the real
    # warp appears to auto-walk the entrant a few tiles -- see the fixture's
    # docstring), so jump to the corridor rather than claim a step that
    # wasn't verified, matching `build_forest_world`'s identical convention
    # for its own south-gate interior test above.
    w.pos = (1, 1)
    for _ in range(4):  # the gate's own top corridor, (1,1) -> (5,1)
        r = step(w, "right")
        assert r.moved and not r.map_changed
    assert w.pos == (5, 1)

    r = step(w, "up")  # the gate's OWN second warp, onward to Route 2
    assert r.map_changed
    assert w.map == "ROUTE_2"
    assert w.pos == (5, 0)  # real, RAM-verified landing tile


def test_route_resumes_mid_way():
    scenario = yaml.safe_load(SCENARIO.read_text(encoding="utf-8"))
    w = build_pallet_world()
    w.map, w.pos = "PALLET_TOWN", (5, 6)  # state saved outdoors: earlier phases skip
    final = run_route(w, scenario["spike_route"])
    assert final.map_name == "ROUTE_1"


# --- Task B (2026-08-06): EncounterAwareWalker + the "steps" strategy -------
#
# EncounterAwareWalker wraps ONLY `.dpad()`; step()/walk_until()/run_leg()/
# greedy_until_map_change()/run_route() above are untouched by these tests
# (they still take a plain FakeWalker throughout this file) -- exactly the
# "sits outside" contract navigate.py's docstring requires.


def test_encounter_aware_walker_absorbs_mid_route_battle():
    """A wild encounter firing mid-route must be invisible to run_leg's
    slide-recovery bookkeeping: not misread as a wall, not treated as a
    failed slide, and the route still lands on Route 1.

    (11,6) -- not (8,6) -- is the starting position: it's the same overshoot
    position `test_slide_left_recovers_from_overshoot` above already
    establishes as reachable via `slide="left"` (Blue's west wall is x=11;
    sliding left regains the x=8-10 corridor). x=8 sits WEST of the actual
    x=10/11 gap in Pallet's north wall, so "slide left" from there walks
    further away from the exit and cannot reach Route 1 under ANY encounter
    handling -- confirmed empirically against this fixture before writing
    this test (`run_leg` from (8,6) raises RouteError at (2,1) even with a
    plain, un-battled FakeWalker). Using (8,6) would test a route-geometry
    bug, not the encounter-absorption behavior this test exists to cover.
    """
    w = build_pallet_world()
    w.map, w.pos = "PALLET_TOWN", (11, 6)
    w.encounter_schedule = {2}  # the 2nd raw dpad() call starts a battle
    w.escape_attempts_left = 3  # RUN fails twice, succeeds on the 3rd tap
    aware = EncounterAwareWalker(w, max_escape_attempts=10)
    state = run_leg(aware, "up", {"map": "ROUTE_1"}, slide="left")
    assert state.map_name == "ROUTE_1"
    assert w.escape_attempts_left <= 0


def test_battle_looks_like_a_wall_without_encounter_handling():
    """Documents the bug being fixed, not just the fix: to plain walk_until/
    step (no EncounterAwareWalker), an unhandled mid-route battle is
    INDISTINGUISHABLE from a wall -- same 'blocked' reason, same unchanged
    position, no exception, nothing that says "this was a battle, not a
    wall". (10,6) is open floor (unlike (11,6) above, "up" would normally
    succeed here), isolating the battle as the sole cause of the block."""
    w = build_pallet_world()
    w.map, w.pos = "PALLET_TOWN", (10, 6)
    w.encounter_schedule = {1}
    reason, state = walk_until(w, "up")
    assert reason == "blocked"
    assert state.pos == (10, 6)
    assert w.state().in_battle == 1  # the real cause, invisible to the caller


def test_trainer_battle_cannot_be_fled_and_raises_after_bounded_attempts():
    """Known, intentionally-unresolved edge case (hazard #2): gen-1 has no
    'run from trainer' option, so a trainer battle (in_battle == 2) never
    clears no matter how many escape attempts are spent. EncounterAwareWalker
    must not spin forever -- it fails loud once max_escape_attempts is
    exhausted, exactly like a wild RUN that never succeeds."""
    w = build_pallet_world()
    w.map, w.pos = "PALLET_TOWN", (11, 6)
    w.in_battle = 2  # a trainer battle, already in progress
    w.escape_attempts_left = 999  # "unlimited" attempts still can't flee a trainer
    aware = EncounterAwareWalker(w, max_escape_attempts=3)
    with pytest.raises(RouteError, match="trainer"):
        aware.dpad("up")


def test_encounter_aware_walker_catches_a_lagged_wild_encounter():
    """The literal case flagged at the start of Task 1: 'battle flag set one
    observation after the frozen step'. `encounter_lag=1` means the dpad()
    call that starts the sighting freezes position with in_battle STILL 0;
    only the NEXT dpad()-shaped observation sets it. A single wrapped
    `.dpad()` call must still end with the encounter fully resolved (escaped),
    matching the design's existing 'retry, don't give up after one check'
    safety net -- this was already true before the 2026-08-06 fix (a wild
    encounter only ever needed the ORIGINAL one retry), and stays true after.
    """
    w = build_pallet_world()
    w.map, w.pos = "PALLET_TOWN", (10, 6)
    w.encounter_schedule = {1}
    w.encounter_lag = 1
    w.pending_in_battle_value = 1  # wild
    w.escape_attempts_left = 1
    aware = EncounterAwareWalker(w, max_escape_attempts=10)
    aware.dpad("up")
    assert w.state().in_battle == 0  # fully resolved, not left hanging


def test_encounter_aware_walker_catches_a_multi_cycle_lagged_trainer_encounter():
    """The CALIBRATED live finding (2026-08-06, see navigate.py's
    EncounterAwareWalker docstring): a trainer's sight-trigger at
    VIRIDIAN_FOREST (1,18) froze position for FIVE raw dpad() calls, each
    reading in_battle==0, before the flag finally flipped to 2 (trainer) --
    not one call, several. `encounter_lag=4` models this exactly: the
    sighting call plus 4 more frozen calls before the flag is visible = 5
    total, matching the live measurement. A single wrapped `.dpad()` call
    (default `max_unmoved_retries=5`, i.e. up to 6 raw dpad() calls) must
    still catch it and raise RouteError -- NOT report a false 'wall' the way
    the pre-fix single-retry design did (see the next test)."""
    w = build_pallet_world()
    w.map, w.pos = "PALLET_TOWN", (10, 6)
    w.encounter_schedule = {1}
    w.encounter_lag = 4
    w.pending_in_battle_value = 2  # trainer -- unfleeable, must raise loudly
    aware = EncounterAwareWalker(w, max_escape_attempts=3)
    with pytest.raises(RouteError, match="trainer"):
        aware.dpad("up")


def test_a_narrower_retry_budget_would_have_missed_the_same_lagged_trainer():
    """Documents the bug the previous test's fix actually closes: with the
    OLD design's effective budget (max_unmoved_retries=1 -- one retry, i.e.
    2 raw dpad() calls total, exactly what shipped before 2026-08-06), the
    same 5-call-lagged trainer from the test above is NOT caught within one
    wrapped call -- it silently reports "no exception, position unchanged"
    exactly like a genuine wall. This is the live (1,18) bug reproduced in
    the fake world: `tools/_debug_edges.json` recorded this exact tile as
    'blocked' on all four sides for precisely this reason."""
    w = build_pallet_world()
    w.map, w.pos = "PALLET_TOWN", (10, 6)
    w.encounter_schedule = {1}
    w.encounter_lag = 4
    w.pending_in_battle_value = 2
    aware = EncounterAwareWalker(w, max_escape_attempts=3, max_unmoved_retries=1)
    aware.dpad("up")  # does NOT raise -- misreported as a wall, the bug
    assert w.state().pos == (10, 6)  # looks exactly like "blocked"
    assert w.state().in_battle == 0  # the flag hadn't lagged into view yet


def test_steps_strategy_replays_literal_directions():
    """The "steps" strategy: literal direction-list replay. This exact
    sequence was captured by replaying `test_greedy_reaches_stairs_warp`'s
    own alternating (up, right) walk and recording only the moves that
    succeeded -- so a "steps" route through the bedroom is exactly the
    "greedy" route through the same room, proving the two strategies agree
    on this stretch (steps is for mazes greedy/legs can't express, not a
    replacement)."""
    w = build_pallet_world()
    route = [
        {
            "phase": "REDS_HOUSE_2F",
            "strategy": "steps",
            "steps": ["right", "up", "right", "up", "right", "up", "right", "up", "up"],
        },
    ]
    state = run_route(w, route)
    assert state.map_name == "REDS_HOUSE_1F"
    assert state.pos == (7, 1)


def test_steps_strategy_raises_immediately_on_unexpected_block():
    """Fail-loud, matching this file's existing convention: "up" from the
    bedroom start is blocked by the bed (test_step_moves_and_blocks pins the
    same fact) -- a "steps" route must not silently swallow that and drift
    off the verified path."""
    w = build_pallet_world()
    route = [{"phase": "REDS_HOUSE_2F", "strategy": "steps", "steps": ["up"]}]
    with pytest.raises(RouteError, match="did not move"):
        run_route(w, route)


def test_buttons_strategy_replays_taps_without_requiring_movement():
    """The "buttons" strategy (S4, 2026-08-15): unlike "steps", a menu tap
    never moves the player, so it must NOT apply "steps"'s fail-loud
    "did not move" check -- that would make every menu macro raise
    immediately. `build_pallet_world()`'s bedroom has no `shop`, so `tap("a")`
    there is a harmless no-op; position staying put is the point of this
    test, not an incidental side effect."""
    w = build_pallet_world()
    route = [{"phase": "REDS_HOUSE_2F", "strategy": "buttons", "buttons": ["a", "a", "a"]}]
    state = run_route(w, route)
    assert state.pos == (3, 6)  # unchanged -- taps don't move the player
    assert state.map_name == "REDS_HOUSE_2F"


def test_s4_mart_route_completes_the_purchase():
    """The exact real-ROM sequence -- see `build_s4_mart_world()`'s
    docstring: walk to the clerk, then the 7-tap purchase macro, and the
    money-based success predicate fires."""
    w = build_s4_mart_world()
    route = [
        {"phase": "VIRIDIAN_MART", "strategy": "steps", "steps": ["up", "up", "left"]},
        {"phase": "VIRIDIAN_MART", "strategy": "buttons", "buttons": ["a"] * 7},
    ]
    state = run_route(w, route)
    assert state.pos == (2, 5)
    assert state.money == 2800
    check = make_success_check({"map": "VIRIDIAN_MART", "money_lte": 2900})
    assert check(state) is True


def test_s4_mart_macro_must_complete_all_seven_taps():
    """Pins the real-ROM finding that the macro is exactly 7 taps, not fewer
    -- one short and nothing is charged yet."""
    w = build_s4_mart_world()
    route = [
        {"phase": "VIRIDIAN_MART", "strategy": "steps", "steps": ["up", "up", "left"]},
        {"phase": "VIRIDIAN_MART", "strategy": "buttons", "buttons": ["a"] * 6},
    ]
    state = run_route(w, route)
    assert state.money == 3000
    check = make_success_check({"map": "VIRIDIAN_MART", "money_lte": 2900})
    assert check(state) is False
