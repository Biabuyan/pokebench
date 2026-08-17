"""Structured game state read from RAM — the machine-readable half of the observation contract.

`read_game_state` accepts anything indexable like PyBoy's `pyboy.memory`
(int -> int, slice -> list of ints), so it is unit-testable against a fake.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from pokebench.harness import memory_map as mm


@dataclass(frozen=True)
class PartyMon:
    species_id: int  # gen-1 internal index, not Pokédex number
    level: int
    hp: int
    max_hp: int
    status: int


@dataclass(frozen=True)
class GameState:
    map_id: int
    map_name: str
    x: int
    y: int
    facing: str  # down | up | left | right (the way the player sprite faces)
    badges: tuple[str, ...]
    party: tuple[PartyMon, ...]
    in_battle: int  # 0 = no, 1 = wild, 2 = trainer
    money: int
    player_name: str

    @property
    def pos(self) -> tuple[int, int]:
        return (self.x, self.y)

    @property
    def party_count(self) -> int:
        return len(self.party)

    def summary(self) -> str:
        """One-line HUD string."""
        return (
            f"{self.map_name} (0x{self.map_id:02X}) pos=({self.x},{self.y}) "
            f"face={self.facing} party={self.party_count} badges={len(self.badges)} "
            f"battle={self.in_battle} ${self.money}"
        )

    def to_dict(self) -> dict:
        return asdict(self)


def read_game_state(mem) -> GameState:
    """Read the structured state block from gen-1 RAM via a pyboy.memory-like object."""
    party_count = min(mem[mm.PARTY_COUNT], 6)
    party = []
    for i in range(party_count):
        base = mm.PARTY_MON_BASE + i * mm.PARTY_MON_SIZE
        party.append(
            PartyMon(
                species_id=mem[base],
                level=mem[base + mm.MON_OFF_LEVEL],
                hp=(mem[base + mm.MON_OFF_CUR_HP] << 8) | mem[base + mm.MON_OFF_CUR_HP + 1],
                max_hp=(mem[base + mm.MON_OFF_MAX_HP] << 8) | mem[base + mm.MON_OFF_MAX_HP + 1],
                status=mem[base + mm.MON_OFF_STATUS],
            )
        )
    map_id = mem[mm.CUR_MAP]
    return GameState(
        map_id=map_id,
        map_name=mm.map_name(map_id),
        x=mem[mm.X_COORD],
        y=mem[mm.Y_COORD],
        facing=mm.decode_facing(mem[mm.PLAYER_FACING]),
        badges=mm.decode_badges(mem[mm.BADGES]),
        party=tuple(party),
        in_battle=mem[mm.IS_IN_BATTLE],
        money=mm.decode_bcd(mem[mm.MONEY : mm.MONEY + 3]),
        player_name=mm.decode_text(mem[mm.PLAYER_NAME : mm.PLAYER_NAME + 11]),
    )
