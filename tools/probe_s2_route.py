"""Discover the S2 anchor (`ROUTE_1 (10,35)`) -> `VIRIDIAN_POKECENTER` route
live against the real ROM (Task C, 2026-08-11), the same way
`probe_forest_route.py` did for S3's north gate -- and for the same reason:
"reconstructed from memory of the game" is explicitly not good enough for
this project (see that script's own `ForestEscaper` docstring), so this
walks the actual emulator rather than encoding a remembered gen-1 map layout.

**$0, ROM-only, no LLM, no network, no API key.** Drives `Emulator` +
`EncounterAwareWalker` directly, exactly like `pokebench spike-s1` does, but
as a one-off research tool (not agent-facing) -- see `tools/README.md`.

Unlike S3's single-map BFS (any exit out of VIRIDIAN_FOREST counts), S2 needs
a *specific* two-map destination: ROUTE_1 -> VIRIDIAN_CITY -> the
VIRIDIAN_POKECENTER door. A single unconstrained BFS across both maps would
also happily wander into ROUTE_2, ROUTE_22, the Mart, the Gym, or any other
Viridian City building and call that "progress" if it doesn't discriminate,
so this runs BFS in explicit, map-scoped PHASES, mirroring `run_route`'s own
"phases dispatched by current map" design in `harness/navigate.py`:

  phase 1: ROUTE_1        -> stop the instant the map becomes VIRIDIAN_CITY.
           Any other map reached (there should not be one from this anchor)
           is treated as a dead end for this phase, not a route.
  phase 2: VIRIDIAN_CITY  -> stop the instant the map becomes
           VIRIDIAN_POKECENTER. Any other map reached (ROUTE_2, ROUTE_22,
           another building's interior, ...) is a dead end for this phase.

Both phases run in ONE emulator session (phase 2 starts from wherever phase 1
ended, no state file round-trip needed) and reuse `ForestEscaper` from
`probe_forest_route.py` unmodified for wild-encounter escapes -- Route 1 has
tall grass, so a wild encounter is expected en route, and the FIGHT/PKMN/
ITEM/RUN menu macro that script observed live is a property of the gen-1
battle engine, not of Viridian Forest specifically.

Usage:

  python tools/probe_s2_route.py bfs [--max-nodes N] [--out PATH]
      Runs both phases and writes the combined step list + per-phase
      metadata as JSON (`{"phases": [{"target_map":..., "steps": [...],
      ...}, ...], "combined_steps": [...]}`), pasteable into a scenario
      YAML's `spike_route:` "steps" phases (one `phase:`/`steps:` block per
      BFS phase above -- see `s2_viridian_pokecenter.yaml` after this script
      confirmed a route).

  python tools/probe_s2_route.py replay --steps-json PATH
      Replays a previously-discovered `combined_steps` list from the anchor
      state and reports pass/fail -- the verification gate before promoting
      a discovered route into the scenario YAML (the project's working rule:
      "an unverified route in the YAML is worse than none").

Bounds: generous, tunable defaults (see PHASES below) -- wide enough that a
real Route 1 + Viridian City crossing should never hit them, narrow enough to
still be a runaway guard. BFS logs a warning if it ever dead-ends against a
bound, exactly like `probe_forest_route.py`, so a future run can widen
deliberately rather than trusting a guess.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import time
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # for ForestEscaper below
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from probe_forest_route import ForestEscaper  # noqa: E402

from pokebench.harness.emulator import Emulator  # noqa: E402
from pokebench.harness.navigate import EncounterAwareWalker, RouteError  # noqa: E402

DEFAULT_ROM = Path("roms/pokemon_red.gb")
DEFAULT_STATE = Path("scenarios/states/s2_route1.state")
DIRECTIONS = ("up", "down", "left", "right")

# One bounded box per phase, keyed by the map the phase searches WITHIN.
# Generous relative to the anchor (10,35) and gen-1's screen-sized maps;
# widen with a printed warning, never silently, if a real run dead-ends here.
PHASES = [
    {"search_map": "ROUTE_1", "target_map": "VIRIDIAN_CITY",
     "bounds": {"x_min": 0, "x_max": 40, "y_min": 0, "y_max": 40}},
    {"search_map": "VIRIDIAN_CITY", "target_map": "VIRIDIAN_POKECENTER",
     "bounds": {"x_min": 0, "x_max": 80, "y_min": 0, "y_max": 80}},
]


def _snapshot(emu: Emulator) -> bytes:
    buf = io.BytesIO()
    emu.pyboy.save_state(buf)
    return buf.getvalue()


def _restore(emu: Emulator, blob: bytes) -> None:
    emu.pyboy.load_state(io.BytesIO(blob))


def bfs_phase(
    emu: Emulator,
    walker: EncounterAwareWalker,
    search_map: str,
    target_map: str,
    bounds: dict,
    max_nodes: int | None = None,
    log=print,
) -> dict:
    """BFS within `search_map` for the first entry into `target_map`.

    A direction that lands on a THIRD map (neither `search_map` nor
    `target_map`) is a dead end for this phase -- logged and skipped, never
    enqueued -- so the search cannot wander out of Viridian City through the
    wrong exit and call it progress. Mirrors `bfs_route` in
    `probe_forest_route.py`; kept separate (not imported) because the stop
    condition is fundamentally different (a specific target map, not "any
    change"), not a copy-paste of unrelated logic.
    """
    start_state = emu.state()
    if start_state.map_name != search_map:
        raise RuntimeError(f"expected to start phase in {search_map}, at {start_state.map_name}")
    start_pos = start_state.pos
    log(f"phase start: {search_map} {start_pos} -> target {target_map}")

    visited: dict[tuple[int, int], bytes] = {start_pos: _snapshot(emu)}
    parent: dict[tuple[int, int], tuple[tuple[int, int], str]] = {}
    queue: deque[tuple[int, int]] = deque([start_pos])
    expanded = 0
    hit_bound = False
    t0 = time.time()

    while queue:
        if max_nodes is not None and expanded >= max_nodes:
            log(f"stopping: max_nodes={max_nodes} reached without reaching {target_map}")
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
                walker.dpad(direction)
            except RouteError as e:
                log(f"  {direction:<5} from {pos}: TRAINER TRAP (not a wall) -- {e}")
                continue
            after = emu.state()

            if after.map_name == target_map:
                path = [direction]
                node = pos
                while node in parent:
                    node, d = parent[node]
                    path.append(d)
                path.reverse()
                log(
                    f"FOUND: {search_map} {start_pos} -> {after.map_name} {after.pos} "
                    f"in {len(path)} steps ({expanded} nodes expanded, "
                    f"{time.time() - t0:.1f}s)"
                )
                return {
                    "search_map": search_map,
                    "target_map": target_map,
                    "steps": path,
                    "start": list(start_pos),
                    "end": list(after.pos),
                    "visited": expanded,
                }

            if after.map_name != search_map:
                # A real exit, just not the one this phase wants -- e.g. Route
                # 2 or Route 22 from Viridian City. Dead end for THIS phase.
                log(f"  {direction:<5} from {pos}: reached {after.map_name}, not "
                    f"{target_map} -- dead end for this phase")
                continue

            if after.pos == before.pos:
                continue  # blocked
            if after.pos in visited:
                continue
            x, y = after.pos
            in_bounds = (
                bounds["x_min"] <= x <= bounds["x_max"] and bounds["y_min"] <= y <= bounds["y_max"]
            )
            if not in_bounds:
                if not hit_bound:
                    log(f"WARNING: dead-ended against the bounds box at {after.pos} -- widen it")
                    hit_bound = True
                continue
            visited[after.pos] = _snapshot(emu)
            parent[after.pos] = (pos, direction)
            queue.append(after.pos)

    raise RuntimeError(
        f"BFS exhausted ({expanded} nodes expanded, {len(visited)} tiles known) "
        f"without reaching {target_map} from {search_map} -- widen bounds or raise --max-nodes"
    )


def bfs_route(rom: Path, state: Path, max_nodes: int | None, log=print) -> dict:
    emu = Emulator(rom, window=False)
    emu.load_state(state)
    emu.settle(120)
    log(f"anchor: {emu.state().summary()}")
    walker = EncounterAwareWalker(emu, max_escape_attempts=20, escape=ForestEscaper())

    phases_out = []
    combined_steps: list[str] = []
    try:
        for phase in PHASES:
            result = bfs_phase(
                emu, walker,
                phase["search_map"], phase["target_map"], phase["bounds"],
                max_nodes=max_nodes, log=log,
            )
            phases_out.append(result)
            combined_steps.extend(result["steps"])
    finally:
        emu.close()

    return {"phases": phases_out, "combined_steps": combined_steps}


def replay(rom: Path, state: Path, steps: list[str], log=print) -> bool:
    """Replay a discovered step list from the anchor and report pass/fail --
    the verification gate before a route is promoted into the scenario YAML.

    KNOWN LIMITATION, found running this exact function (2026-08-11): the
    per-step log line below reads `emu.state()` immediately after
    `walker.dpad()` with no post-warp settle, so the position printed for the
    turn that crosses a warp is the PRE-warp coordinate, not where you
    actually land (`harness/navigate.py`'s `step()` calls `w.settle(90)` and
    re-reads state after a detected map change; this function does not).
    Confirmed live: this printed `VIRIDIAN_POKECENTER (23,25)` -- the
    exterior door tile -- while the authoritative `step()`-based path used by
    `cmd_spike_s1` reported the true interior landing tile, `(3,7)`, on two
    independent runs. The final PASS/FAIL verdict below is still correct
    (`final.map_name` is read fresh after the loop, once things have
    settled) -- only the PER-STEP transcript can show a stale coordinate on
    the exact step that warps. See `tests/fake_world.py::
    build_route1_to_pokecenter_world()` for where the authoritative `(3,7)`
    is pinned as a regression fact instead.
    """
    emu = Emulator(rom, window=False)
    emu.load_state(state)
    emu.settle(120)
    log(f"anchor: {emu.state().summary()}")
    walker = EncounterAwareWalker(emu, max_escape_attempts=20, escape=ForestEscaper())
    try:
        for i, direction in enumerate(steps):
            before = emu.state()
            walker.dpad(direction)
            after = emu.state()
            log(f"  [{i + 1}/{len(steps)}] {direction:<5} -> {after.map_name} {after.pos} "
                f"(moved={after.pos != before.pos or after.map_id != before.map_id})")
        final = emu.state()
        ok = final.map_name == "VIRIDIAN_POKECENTER"
        log(f"\n{'PASS' if ok else 'FAIL'}: ended at {final.summary()}")
        return ok
    except RouteError as e:
        log(f"\nFAIL: {e}")
        return False
    finally:
        emu.close()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("mode", choices=("bfs", "replay"))
    p.add_argument("--rom", type=Path, default=DEFAULT_ROM)
    p.add_argument("--state", type=Path, default=DEFAULT_STATE)
    p.add_argument("--max-nodes", type=int, default=None)
    p.add_argument("--out", type=Path, default=Path("tools/s2_route_probe_result.json"))
    p.add_argument("--steps-json", type=Path, help="[replay] JSON file with a combined_steps list")
    args = p.parse_args(argv)

    if not args.rom.exists():
        print(f"ROM not found at {args.rom}", file=sys.stderr)
        return 2
    if not args.state.exists():
        print(f"anchor state not found at {args.state}", file=sys.stderr)
        return 2

    if args.mode == "bfs":
        result = bfs_route(args.rom, args.state, max_nodes=args.max_nodes)
        args.out.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(f"wrote {args.out}")
        print(f"combined steps ({len(result['combined_steps'])}): {result['combined_steps']}")
        return 0

    if not args.steps_json or not args.steps_json.exists():
        print("replay needs --steps-json pointing at a bfs result file", file=sys.stderr)
        return 2
    steps = json.loads(args.steps_json.read_text(encoding="utf-8"))["combined_steps"]
    ok = replay(args.rom, args.state, steps)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
