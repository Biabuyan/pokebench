"""Thin PyBoy wrapper — the only module that touches the emulator directly.

Everything above this (navigation, agent loop, runner) talks to the small
surface defined here, so it can be tested against fakes.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from pyboy import PyBoy

from pokebench.harness import memory_map as mm
from pokebench.harness.state import GameState, read_game_state

log = logging.getLogger(__name__)

# sha1 hashes as declared by the pret/pokered + pret/pokeblue disassemblies.
KNOWN_ROM_SHA1 = {
    "ea9bcae617fdf159b045185467ae58b2e4a48b9a": "Pokemon Red (UE)",
    "d7037c83e1ae5b39bde3c30787637ba1d4c48ce2": "Pokemon Blue (UE)",
}

# Gen-1 walk timings: turning takes ~8 frames, walking one tile ~16, and
# wXCoord/wYCoord latch at step COMPLETION. A fixed-length hold therefore
# sometimes walks two tiles and returns while a step is still animating,
# which desynchronizes coordinate sampling (observed in the first S1 runs).
# Instead: hold until the coords actually change (or a cap proves us blocked),
# release immediately, then wait for standstill before returning.
DPAD_HOLD_CAP = 40  # covers turn (8) + one full step (16) with margin
DPAD_STANDSTILL = 20  # frames of unchanged coords = the player is at rest
DPAD_WAIT_CAP = 80  # upper bound on the settle wait (warp fades, late steps)

# Agent-loop button timing: a raw press:release the way a human taps a button.
# Unlike dpad() this does NOT hold-until-move — Tier-0 is pure-vision agency, so
# the agent presses raw buttons and reads the consequence off the next frame
# (including the Gen-1 turn-then-step quirk). Tune against the real ROM if
# movement completion looks unreliable; generous defaults favour clean frames.
BUTTON_HOLD = 12  # frames the button is held down
BUTTON_GAP = 12  # frames after release, before the next button in a sequence


class Emulator:
    """PyBoy lifecycle + buttons + screenshots + save states for Pokémon Red."""

    def __init__(self, rom_path: str | Path, window: bool = False, speed: int = 0):
        rom_path = Path(rom_path)
        if not rom_path.exists():
            raise FileNotFoundError(
                f"ROM not found at {rom_path}. PokéBench never ships a ROM — "
                "place your legally-obtained Pokémon Red ROM there (see roms/README.md)."
            )
        digest = hashlib.sha1(rom_path.read_bytes()).hexdigest()
        if digest in KNOWN_ROM_SHA1:
            log.info("ROM verified: %s", KNOWN_ROM_SHA1[digest])
        else:
            log.warning(
                "ROM sha1 %s is not a known Red/Blue (UE) hash. "
                "RAM addresses may not line up (Yellow and non-UE ROMs differ).",
                digest,
            )
        self.pyboy = PyBoy(str(rom_path), window="SDL2" if window else "null")
        # speed: 0 = unlimited (headless benchmarking), 1 = realtime (interactive play)
        self.pyboy.set_emulation_speed(speed if window else 0)

    # --- core loop ---

    def tick(self, frames: int = 1, render: bool = True) -> bool:
        """Advance the emulator. Returns False when the window was closed."""
        return self.pyboy.tick(frames, render)

    def tick_once(self) -> bool:
        return self.pyboy.tick()

    # --- input ---

    def tap(self, button: str, hold: int = 8, release: int = 8) -> None:
        """Press and release a button (a/b/start/select/up/down/left/right)."""
        self.pyboy.button_press(button)
        self.tick(hold, render=False)
        self.pyboy.button_release(button)
        self.tick(release, render=False)

    def _pos_key(self) -> tuple[int, int, int]:
        m = self.pyboy.memory
        return (m[mm.CUR_MAP], m[mm.X_COORD], m[mm.Y_COORD])

    def dpad(self, direction: str) -> None:
        """One walk attempt that moves EXACTLY one tile (or provably blocks).

        Hold the direction until the map/coords change, release, then wait for
        the player to stand still so the caller never samples mid-animation.
        """
        before = self._pos_key()
        self.pyboy.button_press(direction)
        for _ in range(DPAD_HOLD_CAP):
            self.tick(1, render=False)
            if self._pos_key() != before:
                break
        self.pyboy.button_release(direction)
        # Let any committed motion (late step, warp fade) finish.
        last = self._pos_key()
        stable = 0
        for _ in range(DPAD_WAIT_CAP):
            self.tick(1, render=False)
            cur = self._pos_key()
            if cur == last:
                stable += 1
                if stable >= DPAD_STANDSTILL:
                    break
            else:
                stable = 0
                last = cur

    def press(self, button: str, hold: int = BUTTON_HOLD, gap: int = BUTTON_GAP) -> None:
        """One raw button tap for the agent loop (Environment protocol).

        Not the smart one-tile `dpad` — this is the honest Tier-0 primitive: a
        fixed press:release, no movement guarantee. The tools executor settles
        once after the whole sequence so the observation frame is clean.
        """
        self.pyboy.button_press(button)
        self.tick(hold, render=False)
        self.pyboy.button_release(button)
        self.tick(gap, render=False)

    def settle(self, frames: int) -> None:
        """Tick with no input (e.g. to let a map transition finish)."""
        self.tick(frames, render=False)

    # --- observation ---

    @property
    def memory(self):
        return self.pyboy.memory

    def state(self) -> GameState:
        return read_game_state(self.pyboy.memory)

    def screenshot(self):
        """Current screen as a PIL image (render one frame first if needed)."""
        self.tick(1, render=True)
        return self.pyboy.screen.image.copy()

    # --- save states ---

    def save_state(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            self.pyboy.save_state(f)

    def load_state(self, path: str | Path) -> None:
        with open(path, "rb") as f:
            self.pyboy.load_state(f)

    def close(self) -> None:
        self.pyboy.stop(save=False)
