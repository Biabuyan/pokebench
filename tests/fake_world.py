"""A fake grid-world Walker for testing navigation without an emulator.

Rooms are ASCII grids: '#' = blocked, '.' = floor; out-of-bounds is blocked.
Two warp kinds, matching gen-1 (pret/pokered) semantics:

- `entry_warps` (stairs, cave holes): teleport the moment you step onto the tile
  (CheckWarpsNoCollision).
- `collision_warps` (door mats, map-edge connections): fire only when you are
  standing ON the warp tile and press into a blocked tile (CheckWarpsCollision).
  Walking across a door mat does NOT warp — the bug the first real run caught.

Coordinates: x = column (east+), y = row (south+), matching the game. Room
geometry mirrors coordinates verified by the real S1 runs: bedroom start
(3,6), stairs (7,1), 1F mats (2,7)/(3,7) dropping you at Pallet (5,6),
Blue's west wall at x=11 and his front door — an entry warp — at (13,5).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pokebench.harness import memory_map as mm
from pokebench.harness.state import GameState, PartyMon

Warp = tuple[str, tuple[int, int]]


@dataclass
class ShopCounter:
    """A minimal shop-counter model (Improvement 3 / S4, 2026-08-15) -- NOT a
    general dialogue/menu system (none exists in this project's fake world,
    same "just enough surface" discipline as the battle modeling above, not
    full mechanics). Charges `cost` exactly once, the instant `macro_len`
    consecutive `tap("a")` calls land while the player stands on `pos`
    facing `facing` -- matching the real, ROM-verified Viridian Mart
    purchase macro (see `build_s4_mart_world()`)."""

    pos: tuple[int, int]
    facing: str
    macro_len: int
    cost: int


@dataclass
class Room:
    grid: list[str]
    entry_warps: dict[tuple[int, int], Warp] = field(default_factory=dict)
    collision_warps: dict[tuple[int, int], Warp] = field(default_factory=dict)

    def blocked(self, x: int, y: int) -> bool:
        if y < 0 or y >= len(self.grid) or x < 0 or x >= len(self.grid[y]):
            return True
        return self.grid[y][x] == "#"


DELTAS = {"up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0)}


class FakeWalker:
    def __init__(
        self,
        rooms: dict[str, Room],
        start_map: str,
        start_pos: tuple[int, int],
        shop: ShopCounter | None = None,
    ):
        self.rooms = rooms
        self.map = start_map
        self.pos = start_pos
        self.facing = "down"
        self.dpad_calls = 0
        self.press_calls = 0
        # Mutable (Improvement 3 / S4, 2026-08-15): was a hardcoded 3000 in
        # state() until the Mart purchase macro needed to actually spend it.
        self.money = 3000
        self.shop = shop
        self._shop_progress = 0
        # Battle modeling (Task B, 2026-08-06): NOT full gen-1 battle mechanics
        # -- just enough surface for EncounterAwareWalker's tests to control a
        # battle's start/end deterministically, mirroring what the two real
        # hazards documented in navigate.py actually need: (1) an encounter
        # can start instead of a step completing, (2) escaping (wild only)
        # takes a bounded, sometimes-failing number of attempts.
        self.in_battle = 0  # 0 = none, 1 = wild, 2 = trainer (GameState convention)
        self.encounter_schedule: set[int] = set()  # dpad()-call-counts that start a battle
        self.escape_attempts_left = 0  # counts down on tap(); wild clears at/below 0
        # Lagged-detection modeling (Task 1, 2026-08-06): calibrated live against
        # the ROM at VIRIDIAN_FOREST (1,18) -- a trainer's sight-trigger freezes
        # position immediately (in_battle stays 0), but `in_battle` itself doesn't
        # flip until several MORE dpad() calls later (measured: exactly 5 raw
        # dpad() calls total, deterministic, reproduced twice). `encounter_lag`
        # models that gap: 0 (default) = today's instant flip, matching every
        # OTHER test in this file; >0 = additional frozen calls, each still
        # in_battle==0, before `pending_in_battle_value` becomes visible. See
        # navigate.py's EncounterAwareWalker docstring for the live evidence and
        # HANDOFF.md's "Task 1/2" section for the full derivation.
        self.encounter_lag = 0
        self.pending_in_battle_value = 1  # 1 = wild, 2 = trainer (set once the lag expires)
        self._sighted = False
        self._lag_remaining = 0

    def state(self) -> GameState:
        map_id = mm.MAP_IDS[self.map]
        return GameState(
            map_id=map_id,
            map_name=self.map,
            x=self.pos[0],
            y=self.pos[1],
            facing=self.facing,
            badges=(),
            party=(PartyMon(species_id=0xB1, level=8, hp=20, max_hp=20, status=0),),
            in_battle=self.in_battle,
            money=self.money,
            player_name="RED",
        )

    def dpad(self, direction: str) -> None:
        self.dpad_calls += 1
        if self.dpad_calls in self.encounter_schedule:
            # A battle starts INSTEAD of the step completing -- no movement,
            # no facing change (the overworld sprite isn't what's on screen).
            if self.encounter_lag > 0:
                # Sighted, but the flag doesn't set yet -- see the docstring
                # in __init__. This call is ALSO frozen (in_battle stays 0)
                # to match what "up"/"down" from VIRIDIAN_FOREST (1,18) both
                # did live: every call from the moment of sighting through
                # the lag window reads pos-unchanged, in_battle==0.
                self._sighted = True
                self._lag_remaining = self.encounter_lag
                return
            self.in_battle = self.pending_in_battle_value
            return
        if self._sighted:
            self._lag_remaining -= 1
            if self._lag_remaining <= 0:
                self.in_battle = self.pending_in_battle_value
                self._sighted = False
            return  # still frozen (or just became visible this call)
        if self.in_battle:
            # A battle already in progress freezes the tile: from step()'s
            # point of view this looks EXACTLY like a wall (no move, no map
            # change) -- see test_battle_looks_like_a_wall_without_encounter_
            # handling in test_navigate.py, which pins this on purpose.
            return
        self.facing = direction  # pressing a direction faces you that way (even into a wall)
        dx, dy = DELTAS[direction]
        target = (self.pos[0] + dx, self.pos[1] + dy)
        room = self.rooms[self.map]
        if room.blocked(*target):
            # pressing into a wall while standing on a mat/edge fires the warp
            if self.pos in room.collision_warps:
                self.map, self.pos = room.collision_warps[self.pos]
            return
        if target in room.entry_warps:
            self.map, self.pos = room.entry_warps[target]
            return
        self.pos = target
        self._shop_progress = 0  # walking away resets any in-progress purchase

    def tap(self, button: str) -> None:
        """Press-and-release a menu button (`Emulator.tap`'s fake-world
        counterpart) -- originally only used to resolve a battle; outside a
        battle it's a harmless no-op UNLESS a `shop` counter is armed and the
        player is standing on its tile facing it (Improvement 3 / S4,
        2026-08-15), in which case consecutive `tap("a")` calls advance the
        purchase (see `ShopCounter`). A wild encounter (1) clears once
        `escape_attempts_left` is spent; a trainer encounter (2) never does
        -- gen-1 trainers cannot be fled, and this fixture does not pretend
        otherwise (see navigate.py's EncounterAwareWalker docstring)."""
        if not self.in_battle:
            if (
                self.shop is not None
                and button == "a"
                and self.pos == self.shop.pos
                and self.facing == self.shop.facing
            ):
                self._shop_progress += 1
                if self._shop_progress == self.shop.macro_len:
                    self.money -= self.shop.cost
            else:
                self._shop_progress = 0
            return
        self.escape_attempts_left -= 1
        if self.in_battle == 1 and self.escape_attempts_left <= 0:
            self.in_battle = 0

    # --- Environment protocol (harness/tools.py): press / screenshot / settle
    # so the agent loop runs end-to-end against the fake world (no ROM, no API).

    def press(self, button: str) -> None:
        self.press_calls += 1
        if button in DELTAS:
            self.dpad(button)
        # a/b/start/select are no-ops in the fake world (no menus or dialogue)

    def screenshot(self):
        from PIL import Image

        # A tiny Game-Boy-shaped image so the loop's screenshot -> encode ->
        # image-block path is exercised; content is irrelevant to the fake.
        return Image.new("RGB", (160, 144), (self.pos[0] * 8 % 256, self.pos[1] * 8 % 256, 0))

    def settle(self, frames: int) -> None:
        pass


def build_pallet_world() -> FakeWalker:
    """Bedroom -> ground floor -> Pallet Town -> Route 1, mirroring the
    real coordinates observed in the first spike run."""
    bedroom = Room(
        grid=[
            "########",
            "##......",  # stairs at (7,1); PC top-left
            "........",
            "........",
            "........",
            "...#....",  # bed at (3,5): real run showed 'up' blocked from (3,6)
            "......#.",  # TV/SNES at (6,6): real run showed 'right' blocked at (5,6)
            "........",
        ],
        entry_warps={(7, 1): ("REDS_HOUSE_1F", (7, 1))},
    )
    ground = Room(
        grid=[
            "########",
            "........",  # arrive from the stairs at (7,1)
            "........",
            "..##....",  # table + mom
            "........",
            "........",
            "........",
            "........",  # door mats at (2,7)/(3,7); south of them is the wall
        ],
        collision_warps={
            (2, 7): ("PALLET_TOWN", (5, 6)),
            (3, 7): ("PALLET_TOWN", (5, 6)),
        },
    )
    # 20 tiles wide like the real map. Houses x=3-7 and x=11-15 (y=1-5);
    # doors at (5,5) / (13,5) are ENTRY warps (step on -> you're inside);
    # the corridor between the houses is x=8-10; the north gap is x=10,11.
    pallet = Room(
        grid=[
            "##########..########",
            "#..................#",
            "#..#####...#####...#",
            "#..#####...#####...#",
            "#..#####...#####...#",
            "#..##.##...##.##...#",  # the '.' at x=5 / x=13 are the door tiles
            "#..................#",
            "####################",
        ],
        entry_warps={
            (5, 5): ("REDS_HOUSE_1F", (3, 6)),
            (13, 5): ("BLUES_HOUSE", (2, 7)),
        },
        collision_warps={
            (10, 0): ("ROUTE_1", (10, 35)),  # pressing up at the map edge
            (11, 0): ("ROUTE_1", (11, 35)),  # crosses the gen-1 connection
        },
    )
    blues_house = Room(
        grid=[
            "########",
            "........",
            "........",
            "........",
            "........",
            "........",
            "........",
            "........",
        ]
    )
    route1 = Room(
        grid=["." * 20 for _ in range(36)],
    )
    rooms = {
        "REDS_HOUSE_2F": bedroom,
        "REDS_HOUSE_1F": ground,
        "PALLET_TOWN": pallet,
        "BLUES_HOUSE": blues_house,
        "ROUTE_1": route1,
    }
    return FakeWalker(rooms, "REDS_HOUSE_2F", (3, 6))


def build_forest_world() -> FakeWalker:
    """ROUTE_2's approach to Viridian Forest South Gate, and the gate's
    interior corridor -- ONLY the geometry the 2026-08-05 500-turn probe
    actually establishes.

    Source: `runs/probe500/s3_viridian_forest-gemini-t0/20260804-180131/
    seed0/run.jsonl` (gemini trace, turns 393-398), read from `record["state"]`
    (RAM-derived ground truth at each turn boundary), NOT the model's
    narration. An adversarial review (2026-08-05) found the first version of
    this fixture asserted `ROUTE_2 (2,44)` + `up,up` -> `VIRIDIAN_FOREST_
    SOUTH_GATE (4,7)`, which the trace directly contradicts. Rebuilt to encode
    only what turn-boundary state supports.

    ESTABLISHED (turn-boundary ground truth):
      - T393 ends at ROUTE_2 (2,44).
      - T394 (4x "up") and T395 (5x "up") both end at ROUTE_2 (2,44) -- 9
        consecutive "up" presses across two turns produced ZERO net y
        movement and no warp. "up" alone does not progress north from here.
      - T396 ("right","up","up") ends at VIRIDIAN_FOREST_SOUTH_GATE (4,7): a
        warp fired somewhere in that 3-button turn.
      - T397 (6x "up"), still inside the gate: (4,7) -> (4,1). x is
        unchanged and y drops by exactly 6 over 6 presses -- an open,
        unobstructed corridor (and, unlike the ROUTE_2 case above, evidence
        that "one press ~= one tile" roughly held here, since nothing was in
        the way).
      - T397's landing tile, (4,1), does NOT itself fire a warp: the map is
        still VIRIDIAN_FOREST_SOUTH_GATE at the end of that turn. This
        directly contradicts this fixture's previous claim that stepping
        onto (4,1) fires an exterior-door-style warp "on entry" -- one step
        onto it, alone, provably does not.
      - T398 ("right","up","up") ends at VIRIDIAN_FOREST (17,47): another
        warp fired somewhere in that turn.

    NOT established, and deliberately NOT modeled (doing so would mean
    inventing a coordinate the trace does not support -- CLAUDE.md: "a
    missing test is better than a test that enshrines a wrong fact"):
      - The exact ROUTE_2 warp tile / mechanism (entry vs. collision). The
        real run drove `Emulator.press()` (a fixed 12-frame hold + 12-frame
        gap per button, harness/emulator.py) rather than `dpad()`'s
        hold-until-moved -- unlike `dpad()`, `press()` carries no one-tile-
        per-button guarantee, so which of "right"/"up"/"up" in T396 actually
        crossed the warp tile is not recoverable from turn-boundary state.
      - The exact south-gate exit tile / mechanism, for the same reason,
        applied to T398.
      - Whether the ROUTE_2 block immediately north of (2,44) is a real wall
        or a `press()`-timing artifact. Modeled here as blocked (the closest
        faithful fake-world abstraction of "9 presses, 0 net tiles"), but the
        game geometry beyond that single tile is unverified and not built out.

    This is only the tail end of how you'd *arrive* at Viridian Forest from
    Route 2 -- it is NOT the S3 scenario's own route (S3's anchor starts
    already inside VIRIDIAN_FOREST, heading for the north gate, and that maze
    is unverified).
    """
    route2_grid = ["." * 5 for _ in range(45)]  # y up to 44
    route2_grid[43] = "..#.."  # (2,43) blocked: 9 "up" presses moved nobody
    route2 = Room(grid=route2_grid)

    # Only the stretch RAM-verified as open floor: x=4, y in 1..7. Width kept
    # at 5 (x=0..4) -- x was never observed above 4 in this stretch, so a
    # wider room would itself be the kind of invented geometry this
    # docstring warns against. No warps: the exit tile/mechanism is unknown.
    south_gate = Room(grid=["....." for _ in range(8)])

    rooms = {
        "ROUTE_2": route2,
        "VIRIDIAN_FOREST_SOUTH_GATE": south_gate,
    }
    return FakeWalker(rooms, "ROUTE_2", (2, 44))


def build_forest_north_gate_world() -> FakeWalker:
    """RETRACTED 2026-08-06 (Task 1/2/3 of the SAME day, a later pass than the
    one that wrote the rest of this docstring) -- READ THIS FIRST. The
    "ESTABLISHED" claims below could NOT be reproduced. A corrected,
    exhaustive BFS re-probe (fixed `EncounterAwareWalker.max_unmoved_retries`,
    which the original probe's methodology needed and did not have -- see
    `harness/navigate.py`'s class docstring) explored the FULL 668-tile
    region reachable from the S3 anchor and found NO exit from
    VIRIDIAN_FOREST at all: every previously-"blocked" edge is either a
    genuine wall (re-verified with a 5-press protocol, not the original's 2)
    or one of three confirmed unfleeable trainer encounters that a
    single-shot check misread as a wall -- (1,18) among them, the exact tile
    this fixture's corridor was built from. **`x=1` never becomes reachable
    down to `y=1`** in the corrected search; the deepest verified point on
    that column is `y=18`, which is an unfleeable trainer's sight-trigger
    tile, not open floor continuing north. This fixture is kept, UNCHANGED
    below, only so the shape of the retracted claim stays legible (per this
    project's "correct loudly, don't delete" convention) -- it must not be
    read as verified geometry, extended, or cited. Full chain of evidence:
    `HANDOFF.md`, "Task 1/2/3 -- the race, recalibrated, and the north-gate
    claim retracted". `tools/probe_forest_route.py`'s module docstring
    carries the same retraction pointer.

    VIRIDIAN_FOREST's north corridor and VIRIDIAN_FOREST_NORTH_GATE's
    interior -- the project's biggest open question until 2026-08-06 (Task
    B): does the north gate exist and can it be reached? Believed at the time
    to be yes on both counts (see the retraction above for why that no longer
    stands).

    Source: a fresh live frontier-exploration probe from the S3 anchor
    (`scenarios/states/s3_forest.state`), using `Emulator.dpad()`
    EXCLUSIVELY (never `press()`), so every edge here carries the
    one-tile-per-call guarantee no LLM trace ever had. 644 VIRIDIAN_FOREST
    tiles were directly tested across the full probe; this fixture keeps
    only the narrow corridor that proves the gate exists and where it is,
    matching `build_forest_world()`'s discipline of encoding no more than
    the evidence supports.

    Real S3 anchor: VIRIDIAN_FOREST (14,42) -- NOT part of this fixture (see
    `build_forest_world()`'s docstring for the same caveat on its own
    corridor). Verified minimum path length from the anchor to the warp
    tile: 132 `dpad()` calls to (1,1) + 1 more to trigger it = 133 total,
    via BFS over the 644-tile graph. Two independent live replays from the
    pristine anchor both completed it, landing within 1 tile of each other
    (a wild encounter can occasionally consume a step non-deterministically
    mid-replay) -- so 133 is a tight, but not bit-exact, bound.

    Coordinate note: this fixture's VIRIDIAN_FOREST room uses a LOCAL x=0 for
    the corridor, which is the REAL, verified x=1 -- real x=0 was never
    visited in this stretch and is deliberately left out of the grid (so
    `Room.blocked()`'s out-of-bounds rule reports it blocked, rather than
    this fixture claiming a wall it never tested; see `south_gate`'s
    identical convention above). Real y is used directly (no offset). The
    VIRIDIAN_FOREST_NORTH_GATE room uses real coordinates throughout (x=0..5
    was all directly tested at y=1).

    ESTABLISHED (RAM-verified, `Emulator.dpad()` only):
      - VIRIDIAN_FOREST (1, y) for y in 2..15 is open floor: walking `up`
        repeatedly from (1,15) reaches (1,1) one tile per press.
      - VIRIDIAN_FOREST (1,1) -- pressing `up` fires a COLLISION warp (per
        CheckWarpsCollision: stand on the coord, press into a blocked tile,
        same physics as Pallet's Route 1 connection in `build_pallet_world`)
        to VIRIDIAN_FOREST_NORTH_GATE, landing at (1,0).
      - Of 26 columns swept along VIRIDIAN_FOREST's y=1 row (x=1 and
        x=6..13,16..32 -- the north edge the project's traces had walked
        along for months without ever testing `up` a second time), x=1 is
        the ONLY warp. All 25 others are genuine walls, not warps.
      - Inside VIRIDIAN_FOREST_NORTH_GATE: a horizontal corridor (0..5, 1),
        fully tested left/right; (0,1) is a dead end (blocked up AND left);
        (5,1) -- pressing `up` -- fires a SECOND collision warp onward to
        ROUTE_2, landing at (5,0). This confirms the gate really is the
        forest's connector to Route 2 / Pewter City, matching real game
        geography, not a stray warp to somewhere unrelated. A vertical
        corridor (4, 1..7) down to the entrance was also verified open but
        is not modeled here (not needed to establish the gate's existence).

    NOT established (do not extend this fixture past what's below without
    new evidence):
      - Real VIRIDIAN_FOREST x=0 (see the coordinate note above).
      - The gate's SOUTH exit warp (how you get from the entrance mat back
        down to VIRIDIAN_FOREST) -- the probe entered via the north warp and
        explored northward/eastward from there; the return warp was never
        walked.
      - The full x=0-5,y=0-15 northwest quadrant: only the x=1 column was
        swept top-to-bottom; x=0,2,3,4,5 at low y remain unvisited.
      - Whether x=32 / y=45 (VIRIDIAN_FOREST's observed collision bounds
        across the full 644-tile probe) are the map's true edges or simply
        the furthest points this probe reached.
      - `navigate.py`'s scripted route DSL has no wild-encounter handling --
        `step()` reads a battle-frozen tile identically to a wall, and a
        wild battle fired roughly every 5-15 `dpad()` calls during this
        probe. A scripted `spike_route` through this corridor would very
        likely raise `RouteError` on the first encounter; no `spike_route:`
        was added to the S3 scenario YAML for exactly this reason.
    """
    forest_corridor = Room(
        grid=["#"] + ["."] * 15,  # row 0 unused (real y=0 doesn't exist here);
        # rows 1..15 == real y=1..15, all directly verified open floor.
        collision_warps={(0, 1): ("VIRIDIAN_FOREST_NORTH_GATE", (1, 0))},
    )
    north_gate = Room(
        grid=[
            "#.####",  # y=0: only (1,0), the landing tile, is verified open
            "......",  # y=1: the fully-tested corridor, real x=0..5
        ],
        collision_warps={(5, 1): ("ROUTE_2", (5, 0))},
    )
    rooms = {
        "VIRIDIAN_FOREST": forest_corridor,
        "VIRIDIAN_FOREST_NORTH_GATE": north_gate,
    }
    return FakeWalker(rooms, "VIRIDIAN_FOREST", (0, 15))


def build_route1_to_pokecenter_world() -> FakeWalker:
    """S2's oracle route: `ROUTE_1 (10,35)` -> `VIRIDIAN_CITY (21,35)` ->
    `VIRIDIAN_POKECENTER (3,7)` (Task C, 2026-08-11).

    Source: `tools/probe_s2_route.py bfs` (two map-scoped BFS phases, real
    ROM, `Emulator.dpad()` exclusively -- one-tile-per-call guaranteed), then
    the exact 69-step result independently confirmed end-to-end via
    `pokebench spike-s1 --scenario scenarios/s2_viridian_pokecenter.yaml`
    (PASS, run twice) — that command is what supplied the two coordinates
    below that a naive same-frame read got wrong (see the warning below).

    Like `build_forest_world()`/`build_forest_north_gate_world()`, this
    fixture encodes ONLY the single verified path as open floor, one tile
    wide, snaking through turns -- not a claim about any other tile in
    either map. Off-path tiles read as walls not because they were tested
    and found blocked, but because they were never tested at all (`Room`'s
    out-of-bounds/`'#'` convention).

    ESTABLISHED (RAM-verified, three independent live runs — one BFS
    discovery run with zero wild encounters, two `spike-s1` replays that
    each escaped at least one wild encounter mid-route via
    `navigate.WildRunEscaper` and still landed correctly):
      - `ROUTE_1 (11,0)` is a COLLISION warp (CLAUDE.md warp-kind #1: stand
        on the map-edge coordinate, press further into the blocked edge) to
        `VIRIDIAN_CITY (21,35)` — the same mechanism as `build_pallet_world`'s
        `PALLET_TOWN (10,0)/(11,0)` -> `ROUTE_1` connection, not re-derived,
        just independently reproduced one map further north.
      - `VIRIDIAN_CITY (23,25)` is an ENTRY warp (CLAUDE.md warp-kind #1:
        exterior doors fire on entry) to `VIRIDIAN_POKECENTER`, landing at
        the interior's own local `(3,7)` — an interior map keeps its own
        coordinate system, unrelated to the exterior door's `(23,25)`.

    **A same-frame state read is unreliable across a warp -- pin this,
    do not regress it.** `tools/probe_s2_route.py`'s first `replay()`
    implementation read `emu.state()` immediately after `walker.dpad()` with
    no settle, exactly the anti-pattern CLAUDE.md item 2 already warns
    about ("never sample coords on a timer after a button press"). It
    reported the Pokécenter landing tile as `(23,25)` -- the PRE-warp
    exterior coordinate, not yet updated. The authoritative read used here
    is `harness/navigate.py`'s own `step()`, which calls `w.settle(90)` and
    re-reads state after any detected map change (mirroring the M0 spike);
    that path reported `(3,7)` on two independent live runs and is what this
    fixture encodes. The probe script was NOT fixed to match (its `replay`
    mode is a secondary convenience path, `run_route`/`step` is the one
    real verification ran through) -- flagged here so a future session
    does not re-trust the probe's own raw print over this fixture.

    NOT established: any tile off this exact path in either map (see
    `build_forest_world()`'s identical discipline), and the interior of
    VIRIDIAN_POKECENTER beyond the single landing tile.

    Coordinate note, same convention as `build_forest_north_gate_world()`:
    `Room.blocked()`/warp dicts index a room's OWN grid directly with no
    offset math, so a room whose verified tiles sit far from (0,0) needs a
    LOCAL grid -- real x/y minus that room's own origin. `ROUTE_1`'s local
    origin is real `(8,0)` (only x needed shifting; y already starts at 0).
    `VIRIDIAN_CITY`'s local origin is real `(19,25)` (both shifted). Comments
    on each warp/start-pos line give the real coordinate a local one stands
    for, so this stays checkable against the probe's own printed transcript.
    """
    route1 = Room(
        grid=[
            "###.###",  # local y=0 (real y=0): local x=3 (real x=11) is the warp tile
            "###.###",
            "###....",
            "######.",
            "######.",
            "######.",
            "######.",
            "######.",
            "######.",
            "######.",
            "######.",
            "######.",
            "######.",
            "######.",
            "#......",
            "#.#####",
            "#.#####",
            "#.#####",
            "#.#####",
            "#.#####",
            "#....##",
            "####.##",
            "####.##",
            "####.##",
            ".....##",
            ".######",
            ".######",
            ".######",
            "...####",
            "##.####",
            "##.####",
            "##.####",
            "##.####",
            "##.####",
            "##.####",
            "##.####",  # local y=35 (real y=35): local x=2 (real x=10) is the S2 anchor
        ],
        # local (3,0) = real (11,0) -- stand there, press up -> collision warp.
        # Target given in VIRIDIAN_CITY's own local system: local (2,10) =
        # real (21,35), its landing/start tile below.
        collision_warps={(3, 0): ("VIRIDIAN_CITY", (2, 10))},
    )
    viridian_city = Room(
        grid=[
            "####.",  # local y=0 (real y=25): local x=4 (real x=23) is the door tile
            ".....",
            ".####",
            "..###",
            "#.###",
            "#..##",
            "##.##",
            "##.##",
            "##.##",
            "##.##",
            "##.##",  # local y=10 (real y=35): local x=2 (real x=21) = ROUTE_1's landing tile
        ],
        # local (4,0) = real (23,25) -- step onto it -> entry warp (fires on
        # entry, unlike the collision warp above). Target is the interior's
        # own coordinate system (small, used directly, as elsewhere).
        entry_warps={(4, 0): ("VIRIDIAN_POKECENTER", (3, 7))},
    )
    # Interior not modeled beyond the landing tile -- see docstring.
    pokecenter = Room(grid=["........" for _ in range(8)])
    rooms = {
        "ROUTE_1": route1,
        "VIRIDIAN_CITY": viridian_city,
        "VIRIDIAN_POKECENTER": pokecenter,
    }
    # local (2,35) = real ROUTE_1 (10,35), the S2 anchor.
    return FakeWalker(rooms, "ROUTE_1", (2, 35))


def build_s4_mart_world() -> FakeWalker:
    """S4's oracle route: inside VIRIDIAN_MART, from the (post-parcel-
    delivery) landing tile to the clerk's counter and through the purchase
    macro (Improvement 3, 2026-08-15).

    Source: `tools/probe_s4_route.py`, live against the real ROM, `dpad()`/
    `tap()` exclusively (one-tile-per-call / one-press-per-call guaranteed).
    The movement leg and the purchase macro were each independently
    reproduced twice before being encoded here, matching this project's
    "an unverified route is worse than none" rule.

    ESTABLISHED (RAM-verified):
      - VIRIDIAN_MART's own interior landing tile is `(3,7)` -- the SAME
        local coordinate as `build_route1_to_pokecenter_world()`'s
        VIRIDIAN_POKECENTER landing tile (gen-1 interiors apparently share
        this door-mat convention; not re-derived, independently observed on
        a second interior). Reading it needs the same warp settle as that
        fixture (`w.settle(90)` before trusting `emu.state()` across an
        entry warp) -- see that fixture's "same-frame read is unreliable"
        warning, reproduced again here.
      - **The shop is LOCKED on a first, un-settled visit.** Entering
        VIRIDIAN_MART for the first time (before Oak's Parcel is delivered)
        force-walks the player from `(3,7)` to `(2,5)` through an
        un-skippable cutscene (the clerk hands over "OAK's PARCEL"), and
        re-talking to the SAME clerk after that -- or on any visit before
        the Parcel is delivered to Professor Oak in Pallet Town -- only
        repeats "Okay! Say hi to PROF. OAK for me!" and never opens the
        BUY/SELL menu. This is a real, documented gen-1 soft lock (funnels
        the player back to Oak's lab first), not a probe bug: confirmed by
        walking the full round trip live (VIRIDIAN_MART -> ROUTE_1 ->
        PALLET_TOWN -> OAKS_LAB, talk to Oak, -> PALLET_TOWN -> ROUTE_1 ->
        VIRIDIAN_CITY -> VIRIDIAN_MART again) and finding the same clerk now
        opens the shop normally. **This is why S4's anchor is captured AFTER
        the delivery**, not on a fresh first-visit save state -- see
        `scenarios/s4_viridian_mart.yaml`'s header for the full chain and
        why that makes the anchor a scripted, not `pokebench play`, capture.
      - From the POST-delivery landing tile `(3,7)` facing up: `dpad("up")`
        x2 then `dpad("left")` x1 lands at `(2,5)` facing left, directly
        against the clerk's counter -- a second `dpad("left")` from THERE is
        blocked (confirmed: the counter, not open floor, is what stops it).
      - The purchase macro, confirmed twice, identically: exactly SEVEN
        `tap("a")` calls from `(2,5)` facing left, each followed by >=45
        frames of settle (gen-1's per-character text-typing animation drops
        an unsettled tap -- confirmed live: the SAME 7-tap sequence with no
        extra settle between taps never completes the purchase at all, money
        stays $3000 through all 7). In order: opens "Hi there! May I help
        you?" + BUY/SELL/QUIT -> selects BUY -> selects the top item (POKé
        BALL, $200) -> confirms qty x01 -> confirms the Y/N "That will be
        P200. OK?" prompt (YES is the default cursor) -> "Here you go!",
        money $3000 -> $2800. This fake world models only the macro's
        LENGTH and its money effect (`ShopCounter`), not gen-1's actual menu
        state machine -- see that class's docstring for why that is enough.

    NOT established: anything about the shop menu beyond a single POKé BALL
    purchase (SELL, QUIT, other items, insufficient-funds handling), or any
    tile off this exact interior path (see `build_route1_to_pokecenter_
    world()`'s identical discipline).
    """
    mart = Room(
        grid=[
            "####",  # y=0..4: unexplored -- see NOT established
            "####",
            "####",
            "####",
            "####",
            "##..",  # y=5: (2,5) the clerk-facing tile, (3,5) the transit tile;
            #             (1,5) is the counter itself -- confirmed blocked
            "###.",  # y=6: (3,6), transit tile up from the landing tile
            "###.",  # y=7: (3,7), the landing tile
        ],
    )
    shop = ShopCounter(pos=(2, 5), facing="left", macro_len=7, cost=200)
    return FakeWalker({"VIRIDIAN_MART": mart}, "VIRIDIAN_MART", (3, 7), shop=shop)
