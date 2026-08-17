"""Re-derive the Viridian Forest south-anchor -> north-gate route (Task D,
2026-08-06), and observe the gen-1 "select RUN" battle macro live.

**Why this script exists.** A previous session claimed to have found a
133-step route with a throwaway frontier-explorer and deleted the script
afterwards -- only the *claim* survived, hand-encoded into
`tests/fake_world.py::build_forest_north_gate_world()` and HANDOFF.md. That is
exactly the failure this project's own working rules warn about ("a finding
must be reproducible by a future session, not just asserted by a fixture").
This script is the replacement -- and re-running it (2026-08-06, same
session that added the encounter-detection fix below) **could NOT reproduce
that claim**: a corrected, exhaustive BFS over the full 668-tile region
reachable from the anchor finds NO exit from VIRIDIAN_FOREST at all, only
three confirmed (unfleeable) trainer encounters that a naive single-shot
"blocked" check had previously misrecorded as walls. See HANDOFF.md, "Task
1/2/3 — the race, recalibrated, and the north-gate claim retracted" for the
full chain of evidence. **Keep this script anyway** -- the point stands even
though the specific prior claim did not: an unreproducible finding is worse
than no finding, and the only way anyone will ever re-open this question
honestly is if the tool to re-check it still exists.

**$0, ROM-only, no LLM, no network, no API key** -- it drives the real
emulator directly, the same way `pokebench spike-s1` does, but as a one-off
research tool rather than an agent-facing command (`navigate.py`'s
`EncounterAwareWalker`, which this reuses unmodified, is NOT agent-facing
either -- see its module docstring / CLAUDE.md's non-goals section).

Two modes:

  python tools/probe_forest_route.py run-macro
      Walks the anchor state until a wild encounter fires, then observes
      (does not assume) the button sequence that selects and confirms RUN,
      printing screenshots' worth of state transitions to stdout. This is
      how the macro baked into `ForestEscaper` below was actually found --
      re-run this if a ROM revision or region ever changes the battle menu
      layout.

  python tools/probe_forest_route.py bfs [--max-nodes N] [--out PATH]
      Breadth-first search over `Emulator.dpad()` moves from the S3 anchor
      (VIRIDIAN_FOREST (14,42)) to the first map change away from
      VIRIDIAN_FOREST, which -- per the anchor's geography -- is the north
      gate. BFS visit order guarantees the first path found is shortest
      (tile count, not wall-clock: an absorbed encounter costs real frames
      but not a "step"). Each frontier node is a saved in-memory PyBoy state
      (`pyboy.save_state`/`load_state` against an `io.BytesIO`, NOT the
      Emulator's file-based save/load -- this is the one place in the
      project that reaches past that wrapper, because BFS backtracking needs
      many cheap, disposable checkpoints, not the durable named states under
      `scenarios/states/`). Encounters mid-search are absorbed transparently
      by `EncounterAwareWalker` wrapping the emulator, exactly like the real
      spike route will use it. A direction that leads into an unfleeable
      trainer battle raises `RouteError` (gen-1 trainers cannot be fled, and
      this is a scripted, battle-avoiding probe by design); `bfs_route`
      catches that specifically, logs it as a TRAINER TRAP distinct from a
      wall, and moves on to the next candidate direction rather than crashing
      or silently recording a false wall (see `EncounterAwareWalker`'s
      `max_unmoved_retries`, fixed 2026-08-06 -- without it, a slow
      trainer sight-trigger reads exactly like a wall for several calls; see
      its docstring in `harness/navigate.py`). Writes the discovered
      direction list as JSON (`{"steps": [...], "bounds": {...}, "visited":
      N}`) so it can be pasted directly into a scenario YAML's `spike_route:`
      "steps" phase -- **but as of 2026-08-06 this has never happened**: both
      runs (before and after the encounter-detection fix) raise
      `RuntimeError` ("BFS exhausted... without leaving VIRIDIAN_FOREST"),
      meaning no exit was found within the established bounds. Treat that
      exception, from this specific anchor/state, as a real negative result,
      not a bug to silently work around.

Bounding box: this script defaults to the box already established by the
lost script's own findings (x in [1,32], y in [1,45]) purely as a runaway
guard -- BFS will refuse to expand a tile outside it and will print a
warning if the search dead-ends against that wall, so a future run can widen
it deliberately rather than silently trusting a decade-old bound.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import time
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pokebench.harness.emulator import Emulator  # noqa: E402
from pokebench.harness.navigate import EncounterAwareWalker, RouteError  # noqa: E402

DEFAULT_ROM = Path("roms/pokemon_red.gb")
DEFAULT_STATE = Path("scenarios/states/s3_forest.state")
DEFAULT_BOUNDS = {"x_min": 1, "x_max": 32, "y_min": 1, "y_max": 45}
DIRECTIONS = ("up", "down", "left", "right")


class ForestEscaper:
    """The observed, ROM-verified RUN macro (2026-08-06 probe against
    `roms/pokemon_red.gb`, sha1 in `Emulator.KNOWN_ROM_SHA1`).

    NOT a general gen-1 battle solver -- it only handles what this probe
    actually observed: a wild-encounter intro ("Wild X appeared!" + the
    auto send-out animation) followed by the standard FIGHT/PKMN/ITEM/RUN
    2x2 cursor menu (FIGHT top-left, RUN bottom-right). Verified live,
    screenshot by screenshot (see `observe_run_macro` below) -- not
    reconstructed from memory of the games, per this task's own instruction.

    Findings:
      - From the moment `state().in_battle` first turns nonzero, the
        "Wild X appeared!" text needs ~600 frames to finish printing before
        an A press reliably dismisses it (a premature A can no-op against
        partially-printed text). One A tap then auto-plays the "Go! <mon>!"
        send-out animation; ~480 more frames lands cleanly on the menu with
        the cursor defaulted to FIGHT.
      - Cursor navigation: DOWN (FIGHT -> ITEM) then RIGHT (ITEM -> RUN)
        reaches RUN from the default position. Gen-1 grid-menu cursors clamp
        at the edge rather than wrapping, so replaying the same two taps
        when the cursor is ALREADY on RUN (a mid-battle retry after a failed
        run) is a no-op each time, not a further move -- i.e. the macro is
        idempotent and does not need to know the current cursor position.
        This half of the claim (idempotency across a FAILED run + retry) is
        inferred from standard gen-1 menu clamping, not itself observed: our
        test encounter (a level-3 Weedle vs. our level-8+ party) ran away
        successfully on 8/8 tries via `emu.tap()`'s default 8-frame hold, so
        no genuine "Can't escape!" outcome was ever produced to replay a
        retry against. Flagged here rather than silently assumed solid.
      - A successful RUN prints "Got away safely!"; ~180 frames then one
        more A dismisses it and `in_battle` clears. A failed run
        presumably prints "Can't escape!" and returns to the same menu
        instead (per pret/pokered's battle engine) -- again inferred, not
        observed here.

    Stateful across an arbitrarily long BFS run: `_mid_battle` tracks
    whether THIS battle's intro has already been primed, so a multi-attempt
    RUN (called repeatedly by `EncounterAwareWalker._resolve_battle`) only
    replays the intro once, and the flag clears the moment `in_battle` goes
    back to 0 so the NEXT, unrelated encounter primes fresh.
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


def observe_run_macro(rom: Path, state: Path, log=print) -> None:
    """Interactive-ish observation mode: walk the anchor until a wild
    encounter fires, then print state transitions through the escape macro
    so a human can eyeball that RUN (not FIGHT) was actually selected.
    Screenshots land in `tools/_probe_screens/` for visual confirmation --
    not committed (see tools/README note), just a debugging aid."""
    shots = Path(__file__).parent / "_probe_screens"
    shots.mkdir(exist_ok=True)

    emu = Emulator(rom, window=False)
    emu.load_state(state)
    emu.settle(120)
    log(f"anchor: {emu.state().summary()}")

    found = False
    for i in range(300):
        emu.dpad(DIRECTIONS[i % 2])  # alternate up/down; grass is dense near the anchor
        if emu.state().in_battle:
            log(f"encounter after {i + 1} dpad() calls: {emu.state().summary()}")
            found = True
            break
    if not found:
        log("no encounter in 300 calls -- try a different tile or more calls")
        emu.close()
        return

    emu.screenshot().save(shots / "01_encounter_start.png")
    escaper = ForestEscaper()
    log("running the observed RUN macro...")
    escaper(emu)
    emu.screenshot().save(shots / "02_after_escape.png")
    log(f"in_battle after macro: {emu.state().in_battle} (0 == escaped)")
    log(f"screenshots -> {shots}")
    emu.close()


def _snapshot(emu: Emulator) -> bytes:
    buf = io.BytesIO()
    emu.pyboy.save_state(buf)
    return buf.getvalue()


def _restore(emu: Emulator, blob: bytes) -> None:
    emu.pyboy.load_state(io.BytesIO(blob))


def bfs_route(
    rom: Path,
    state: Path,
    bounds: dict = DEFAULT_BOUNDS,
    max_nodes: int | None = None,
    log=print,
) -> dict:
    """BFS from the S3 anchor to the first map change out of VIRIDIAN_FOREST.

    Returns a dict: {"steps": [...], "target_map": ..., "visited": N,
    "start": [x,y], "end": [x,y]}. Raises RuntimeError if the queue empties
    (search exhausted the reachable, in-bounds tiles) without ever leaving
    the map.
    """
    emu = Emulator(rom, window=False)
    emu.load_state(state)
    emu.settle(120)
    start_state = emu.state()
    start_map = start_state.map_name
    start_pos = start_state.pos
    log(f"anchor: {start_state.summary()}")

    walker = EncounterAwareWalker(emu, max_escape_attempts=20, escape=ForestEscaper())

    visited: dict[tuple[int, int], bytes] = {start_pos: _snapshot(emu)}
    parent: dict[tuple[int, int], tuple[tuple[int, int], str]] = {}
    queue: deque[tuple[int, int]] = deque([start_pos])
    expanded = 0
    t0 = time.time()

    while queue:
        if max_nodes is not None and expanded >= max_nodes:
            log(f"stopping: max_nodes={max_nodes} reached without leaving {start_map}")
            break
        pos = queue.popleft()
        _restore(emu, visited[pos])
        expanded += 1
        if expanded % 25 == 0:
            log(
                f"...expanded {expanded}, queue={len(queue)}, "
                f"{time.time() - t0:.1f}s elapsed, at {pos}"
            )

        for direction in DIRECTIONS:
            _restore(emu, visited[pos])
            before = emu.state()
            try:
                walker.dpad(direction)  # absorbs any encounter transparently
            except RouteError as e:
                # A trainer whose sight-trigger fired mid-test (calibrated
                # 2026-08-06: gen-1 trainers cannot be fled, and their
                # walk-up + forced dialogue take several dpad() calls to
                # resolve into in_battle -- see EncounterAwareWalker's
                # docstring). This is NOT the same as a wall: it is a real,
                # confirmed obstacle this BFS cannot pass (fighting is out of
                # scope for a dpad()-only probe). Log it distinctly so the
                # resulting graph/route never silently reports it as
                # "blocked" -- restore happens on the next loop iteration
                # (or the next `pos` pop) regardless, so it's safe to just
                # skip this direction and keep exploring elsewhere.
                log(f"  {direction:<5} from {pos}: TRAINER TRAP (not a wall) -- {e}")
                continue
            after = emu.state()

            if after.map_name != start_map:
                # Left the forest -- BFS visit order guarantees this is the
                # shortest such path found so far. Reconstruct and return.
                path = [direction]
                node = pos
                while node in parent:
                    node, d = parent[node]
                    path.append(d)
                path.reverse()
                log(
                    f"FOUND: {start_map} {start_pos} -> {after.map_name} {after.pos} "
                    f"in {len(path)} steps ({expanded} nodes expanded, "
                    f"{time.time() - t0:.1f}s)"
                )
                emu.close()
                return {
                    "steps": path,
                    "target_map": after.map_name,
                    "start": list(start_pos),
                    "end": list(after.pos),
                    "visited": expanded,
                }

            if after.pos == before.pos:
                continue  # blocked (wall or an un-clearable, logged encounter)
            if after.pos in visited:
                continue  # already queued via a shorter-or-equal path
            x, y = after.pos
            in_bounds = (
                bounds["x_min"] <= x <= bounds["x_max"] and bounds["y_min"] <= y <= bounds["y_max"]
            )
            if not in_bounds:
                continue  # outside the known-safe search box; see module docstring
            visited[after.pos] = _snapshot(emu)
            parent[after.pos] = (pos, direction)
            queue.append(after.pos)

    emu.close()
    raise RuntimeError(
        f"BFS exhausted ({expanded} nodes expanded, {len(visited)} tiles known) "
        f"without leaving {start_map} -- widen `bounds` or raise --max-nodes"
    )


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("mode", choices=("run-macro", "bfs"))
    p.add_argument("--rom", type=Path, default=DEFAULT_ROM)
    p.add_argument("--state", type=Path, default=DEFAULT_STATE)
    p.add_argument("--max-nodes", type=int, default=None)
    p.add_argument("--out", type=Path, default=Path("tools/forest_route_probe_result.json"))
    args = p.parse_args(argv)

    if not args.rom.exists():
        print(f"ROM not found at {args.rom}", file=sys.stderr)
        return 2
    if not args.state.exists():
        print(f"anchor state not found at {args.state}", file=sys.stderr)
        return 2

    if args.mode == "run-macro":
        observe_run_macro(args.rom, args.state)
        return 0

    result = bfs_route(args.rom, args.state, max_nodes=args.max_nodes)
    result["bounds"] = DEFAULT_BOUNDS
    args.out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"wrote {args.out}")
    print(f"steps ({len(result['steps'])}): {result['steps']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
