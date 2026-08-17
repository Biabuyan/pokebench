"""Interactive play + save-state recorder (`pokebench play`).

Opens a PyBoy window so you can play with the keyboard while a console HUD
shows live RAM state. When you stand still for a few seconds on the capture
map (default: your bedroom, REDS_HOUSE_2F) with the required party size, the
current state is written to the scenario anchor file automatically — no
hotkeys to remember.
"""

from __future__ import annotations

from pathlib import Path

from pokebench.harness.emulator import Emulator

KEYBINDS = """
PyBoy window keys:  arrows = move   A = 'a'   B = 's'   Start = Enter
                    Select = Backspace   Space = turbo   Esc = quit
"""

HUD_EVERY_FRAMES = 15
IDLE_SAMPLES_TO_CAPTURE = 20  # 20 samples x 15 frames = 300 frames = ~5s standing still


def play(
    rom: Path,
    state: Path | None = None,
    capture_map: str | None = "REDS_HOUSE_2F",
    min_party: int = 1,
    out: Path = Path("scenarios/states/s1_bedroom.state"),
) -> None:
    emu = Emulator(rom, window=True, speed=1)
    if state:
        emu.load_state(state)
        print(f"Resumed from {state}")
    print(KEYBINDS)
    if capture_map:
        print(
            f"Auto-capture ARMED: stand still ~5s in {capture_map} "
            f"(party >= {min_party}) to save -> {out}"
        )

    last = None
    idle = 0
    captured_at: tuple | None = None
    frames = 0
    try:
        while emu.tick_once():
            frames += 1
            if frames % HUD_EVERY_FRAMES:
                continue
            s = emu.state()
            key = (s.map_id, s.x, s.y)
            if key == last:
                idle += 1
            else:
                idle = 0
                last = key
                print(f"\r{s.summary():<80}", end="", flush=True)
            if (
                capture_map
                and idle >= IDLE_SAMPLES_TO_CAPTURE
                and s.map_name == capture_map
                and s.party_count >= min_party
                and key != captured_at
            ):
                emu.save_state(out)
                captured_at = key
                print(f"\n*** CAPTURED {out} at {s.summary()} ***")
                print("(move to a different tile and stand still again to re-capture)")
    except KeyboardInterrupt:
        pass
    finally:
        print()
        emu.close()
