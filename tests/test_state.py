from pokebench.harness import memory_map as mm
from pokebench.harness.state import read_game_state


class FakeMemory:
    """Indexable like pyboy.memory: int -> int, slice -> list of ints."""

    def __init__(self, data: dict[int, int]):
        self.data = data

    def __getitem__(self, key):
        if isinstance(key, slice):
            return [self.data.get(a, 0) for a in range(key.start, key.stop)]
        return self.data.get(key, 0)


def make_memory() -> FakeMemory:
    data = {
        mm.PARTY_COUNT: 2,
        mm.CUR_MAP: mm.MAP_IDS["PALLET_TOWN"],
        mm.X_COORD: 10,
        mm.Y_COORD: 5,
        mm.BADGES: 0b00000011,
        mm.PLAYER_FACING: 0x04,  # facing up
        mm.IS_IN_BATTLE: 0,
        mm.MONEY: 0x00,
        mm.MONEY + 1: 0x30,
        mm.MONEY + 2: 0x00,
        # "RED" in gen-1 text encoding
        mm.PLAYER_NAME: 0x91,
        mm.PLAYER_NAME + 1: 0x84,
        mm.PLAYER_NAME + 2: 0x83,
        mm.PLAYER_NAME + 3: 0x50,
    }
    # mon 0: species 0xB1, level 8, hp 300/300 (big-endian 0x012C), status 0
    base = mm.PARTY_MON_BASE
    data[base] = 0xB1
    data[base + mm.MON_OFF_LEVEL] = 8
    data[base + mm.MON_OFF_CUR_HP] = 0x01
    data[base + mm.MON_OFF_CUR_HP + 1] = 0x2C
    data[base + mm.MON_OFF_MAX_HP] = 0x01
    data[base + mm.MON_OFF_MAX_HP + 1] = 0x2C
    # mon 1: species 0x05, level 3, hp 9/15, status 0x08 (poisoned bit example)
    base += mm.PARTY_MON_SIZE
    data[base] = 0x05
    data[base + mm.MON_OFF_LEVEL] = 3
    data[base + mm.MON_OFF_CUR_HP + 1] = 9
    data[base + mm.MON_OFF_MAX_HP + 1] = 15
    data[base + mm.MON_OFF_STATUS] = 0x08
    return FakeMemory(data)


def test_read_game_state_full():
    s = read_game_state(make_memory())
    assert s.map_name == "PALLET_TOWN"
    assert s.pos == (10, 5)
    assert s.facing == "up"
    assert s.badges == ("Boulder", "Cascade")
    assert s.money == 3000
    assert s.player_name == "RED"
    assert s.in_battle == 0
    assert s.party_count == 2
    assert s.party[0].hp == 300 and s.party[0].max_hp == 300  # big-endian pair
    assert s.party[0].level == 8
    assert s.party[1].hp == 9 and s.party[1].max_hp == 15
    assert s.party[1].status == 0x08


def test_party_count_clamped():
    mem = make_memory()
    mem.data[mm.PARTY_COUNT] = 255  # corrupt/uninitialized RAM must not explode
    s = read_game_state(mem)
    assert s.party_count == 6


def test_to_dict_roundtrips_to_json():
    import json

    s = read_game_state(make_memory())
    assert json.loads(json.dumps(s.to_dict()))["map_name"] == "PALLET_TOWN"
