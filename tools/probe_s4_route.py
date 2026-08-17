"""Discover the S4 anchor -> VIRIDIAN_MART route, the clerk's tile, and the
button macro that completes a purchase (Improvement 3, 2026-08-15) -- live
against the real ROM, the same way `probe_s2_route.py` did for the Pokécenter
and `probe_forest_route.py` did for S3's north gate. "Reconstructed from
memory of the game" is explicitly not good enough for this project (see those
scripts' own docstrings), so this walks the actual emulator.

**$0, ROM-only, no LLM, no network, no API key.** Drives `Emulator` +
`EncounterAwareWalker` directly, exactly like `pokebench spike-s1` does, but
as a one-off research tool (not agent-facing) -- see `tools/README.md`.

Reuses `bfs_phase` from `probe_s2_route.py` UNMODIFIED for the two outdoor
legs (ROUTE_1 -> VIRIDIAN_CITY -> VIRIDIAN_MART door) -- same map-scoped-BFS
reasoning as that script's own docstring: an unconstrained single BFS would
happily wander into the Gym, the Pokécenter, or Route 22 and call that
progress.

Once inside VIRIDIAN_MART, a third phase (`explore_room`, new here -- the
interior BFS is deliberately NOT via `bfs_phase`, since there is no
`target_map` to stop at; the goal is mapping the walkable interior, not
leaving it) does a bounded frontier exploration of the small interior to find
every walkable tile, which locates the clerk's counter (an NPC tile reads as
"blocked" -- unmoved dpad -- exactly like a wall from `step()`'s point of
view, so the clerk is identified as the one blocked-from-the-south tile
directly under the counter sprite, not by name).

Usage:

  python tools/probe_s4_route.py locate [--out PATH]
      Runs the ROUTE_1 -> VIRIDIAN_CITY -> VIRIDIAN_MART phases, explores the
      Mart interior, and writes the combined route + interior map as JSON.
      Also saves a screenshot at the landing tile and at the tile believed to
      be facing the clerk, for visual confirmation (this tool has no OCR --
      screenshots are inspected by a human/agent afterward, same as any
      other PokéBench dev screenshot).

  python tools/probe_s4_route.py buy --macro up,a,a,a [--out PATH]
      From the tile facing the clerk (walks the `locate` route first), plays
      a candidate button macro (comma-separated `tap()` names) and reports
      money before/after plus a screenshot after every button -- the
      trial-and-error loop for finding the real BUY macro. Screenshots let a
      human/agent read the on-screen menu text without any RAM-level dialogue
      decoder (none exists in this project -- `memory_map.py` maps no VRAM
      tile addresses).

Bounds: generous, tunable -- see PHASES/ROOM_BOUNDS below. BFS/exploration
logs a warning if it ever dead-ends against a bound, exactly like the other
probes, so a future run can widen deliberately rather than trusting a guess.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import time
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from probe_forest_route import ForestEscaper  # noqa: E402
from probe_s2_route import bfs_phase  # noqa: E402

from pokebench.harness.emulator import Emulator  # noqa: E402
from pokebench.harness.navigate import EncounterAwareWalker  # noqa: E402

DEFAULT_ROM = Path("roms/pokemon_red.gb")
DEFAULT_STATE = Path("scenarios/states/s2_route1.state")
DIRECTIONS = ("up", "down", "left", "right")

OUTDOOR_PHASES = [
    {"search_map": "ROUTE_1", "target_map": "VIRIDIAN_CITY",
     "bounds": {"x_min": 0, "x_max": 40, "y_min": 0, "y_max": 40}},
    {"search_map": "VIRIDIAN_CITY", "target_map": "VIRIDIAN_MART",
     "bounds": {"x_min": 0, "x_max": 80, "y_min": 0, "y_max": 80}},
]

# Small interior; generous relative to any real gen-1 Mart (they're one
# screen). Widen with a printed warning, never silently, if a real run
# dead-ends here.
ROOM_BOUNDS = {"x_min": 0, "x_max": 20, "y_min": 0, "y_max": 20}


def _snapshot(emu: Emulator) -> bytes:
    buf = io.BytesIO()
    emu.pyboy.save_state(buf)
    return buf.getvalue()


def _restore(emu: Emulator, blob: bytes) -> None:
    emu.pyboy.load_state(io.BytesIO(blob))


def explore_room(emu: Emulator, walker: EncounterAwareWalker, room_map: str, log=print) -> dict:
    """Bounded frontier BFS of the CURRENT interior map -- maps every
    directly-reachable walkable tile plus every tile a direction bumped into
    without moving (a wall OR an NPC; this function does not distinguish
    them, matching `step()`'s own "unmoved = blocked" convention elsewhere in
    this project)."""
    start_state = emu.state()
    if start_state.map_name != room_map:
        raise RuntimeError(f"expected to be in {room_map}, at {start_state.map_name}")
    start_pos = start_state.pos
    log(f"explore start: {room_map} {start_pos}")

    visited: dict[tuple[int, int], bytes] = {start_pos: _snapshot(emu)}
    parent: dict[tuple[int, int], tuple[tuple[int, int], str]] = {}
    blocked: dict[tuple[int, int], list[str]] = {}
    queue: deque[tuple[int, int]] = deque([start_pos])
    expanded = 0
    hit_bound = False
    t0 = time.time()

    while queue:
        pos = queue.popleft()
        _restore(emu, visited[pos])
        expanded += 1

        for direction in DIRECTIONS:
            _restore(emu, visited[pos])
            before = emu.state()
            walker.dpad(direction)
            after = emu.state()

            if after.map_name != room_map:
                log(f"  {direction:<5} from {pos}: LEFT the room to {after.map_name} "
                    f"{after.pos} -- not modeled, treating as a bound")
                continue

            if after.pos == before.pos:
                blocked.setdefault(pos, []).append(direction)
                continue

            if after.pos in visited:
                continue
            x, y = after.pos
            in_bounds = (
                ROOM_BOUNDS["x_min"] <= x <= ROOM_BOUNDS["x_max"]
                and ROOM_BOUNDS["y_min"] <= y <= ROOM_BOUNDS["y_max"]
            )
            if not in_bounds:
                if not hit_bound:
                    log(f"WARNING: dead-ended against ROOM_BOUNDS at {after.pos} -- widen it")
                    hit_bound = True
                continue
            visited[after.pos] = _snapshot(emu)
            parent[after.pos] = (pos, direction)
            queue.append(after.pos)

    log(f"explore done: {expanded} tiles expanded, {len(visited)} walkable, "
        f"{len(blocked)} tiles with a blocked direction, {time.time() - t0:.1f}s")
    return {
        "room_map": room_map,
        "start": list(start_pos),
        "walkable": [list(p) for p in visited],
        "blocked": {f"{p[0]},{p[1]}": dirs for p, dirs in blocked.items()},
    }


def locate(rom: Path, state: Path, out: Path, log=print) -> dict:
    emu = Emulator(rom, window=False)
    emu.load_state(state)
    emu.settle(120)
    log(f"anchor: {emu.state().summary()}")
    walker = EncounterAwareWalker(emu, max_escape_attempts=20, escape=ForestEscaper())

    result: dict = {"phases": [], "combined_steps": []}
    try:
        for phase in OUTDOOR_PHASES:
            r = bfs_phase(
                emu, walker, phase["search_map"], phase["target_map"], phase["bounds"], log=log
            )
            result["phases"].append(r)
            result["combined_steps"].extend(r["steps"])

        # `bfs_phase`'s own FOUND branch reads state immediately after the
        # warp-firing `dpad()`, no settle -- fine for a COLLISION warp (both
        # outdoor legs above), but an ENTRY warp into a building (this one)
        # reports the STALE pre-warp exterior coordinate, exactly the bug
        # `tests/fake_world.py::build_route1_to_pokecenter_world()` already
        # pins for S2's Pokécenter door. Settle before trusting `emu.state()`.
        emu.settle(90)
        landing = emu.state()
        log(f"landed in VIRIDIAN_MART at {landing.pos}, money=${landing.money}")
        Path("tools/s4_mart_landing.png").parent.mkdir(parents=True, exist_ok=True)
        emu.screenshot().save("tools/s4_mart_landing.png")

        interior = explore_room(emu, walker, "VIRIDIAN_MART", log=log)
        result["interior"] = interior
        result["landing_money"] = landing.money
    finally:
        emu.close()

    out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    log(f"wrote {out}")
    return result


def buy(rom: Path, state: Path, walk_steps: list[str], to_clerk: list[str], macro: list[str],
        log=print) -> dict:
    """Walk the outdoor route + into position facing the clerk, then play a
    candidate purchase macro (`tap()` names, comma-separated at the CLI),
    screenshotting after every button and reporting money before/after."""
    emu = Emulator(rom, window=False)
    emu.load_state(state)
    emu.settle(120)
    walker = EncounterAwareWalker(emu, max_escape_attempts=20, escape=ForestEscaper())
    shots_dir = Path("tools/s4_buy_shots")
    shots_dir.mkdir(parents=True, exist_ok=True)

    try:
        for direction in walk_steps:
            before = emu.state()
            walker.dpad(direction)
            after = emu.state()
            log(f"  route {direction:<5} -> {after.map_name} {after.pos} "
                f"(moved={after.pos != before.pos or after.map_id != before.map_id})")
        for i, direction in enumerate(to_clerk):
            walker.dpad(direction)
            log(f"  to-clerk [{i+1}/{len(to_clerk)}] {direction:<5} -> {emu.state().summary()}")

        before_money = emu.state().money
        log(f"facing clerk at {emu.state().pos}, money=${before_money}")
        for i, button in enumerate(macro):
            emu.tap(button.strip())
            emu.settle(60)
            s = emu.state()
            shot = shots_dir / f"{i:02d}_{button.strip()}.png"
            emu.screenshot().save(shot)
            log(f"  [{i+1}/{len(macro)}] tap({button.strip()!r}) -> money=${s.money} "
                f"screenshot={shot}")
        after_money = emu.state().money
        log(f"\nRESULT: money ${before_money} -> ${after_money} "
            f"(delta {after_money - before_money})")
        return {"before_money": before_money, "after_money": after_money}
    finally:
        emu.close()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("mode", choices=("locate", "buy"))
    p.add_argument("--rom", type=Path, default=DEFAULT_ROM)
    p.add_argument("--state", type=Path, default=DEFAULT_STATE)
    p.add_argument("--out", type=Path, default=Path("tools/s4_route_probe_result.json"))
    p.add_argument("--walk-steps", type=str, default="", help="[buy] comma-separated outdoor route")
    p.add_argument("--to-clerk", type=str, default="", help="[buy] comma-separated dpad directions")
    p.add_argument("--macro", type=str, default="", help="[buy] comma-separated tap() buttons")
    args = p.parse_args(argv)

    if not args.rom.exists():
        print(f"ROM not found at {args.rom}", file=sys.stderr)
        return 2
    if not args.state.exists():
        print(f"anchor state not found at {args.state}", file=sys.stderr)
        return 2

    if args.mode == "locate":
        locate(args.rom, args.state, args.out)
        return 0

    walk_steps = [s for s in args.walk_steps.split(",") if s]
    to_clerk = [s for s in args.to_clerk.split(",") if s]
    macro = [s for s in args.macro.split(",") if s]
    buy(args.rom, args.state, walk_steps, to_clerk, macro)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
