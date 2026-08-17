"""The fixed harness: emulator wrapper, RAM map, game state, navigation primitives.

Everything in this package is model-agnostic. The observation contract and tool
tiers (M1) build on these primitives; no provider adapter may reach around them.

Note: `emulator` (which imports PyBoy) is intentionally NOT re-exported here so
that pure-logic modules stay importable without an emulator installed.
"""

from pokebench.harness.state import GameState, PartyMon, read_game_state

__all__ = ["GameState", "PartyMon", "read_game_state"]
