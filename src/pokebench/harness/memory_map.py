"""Pokémon Red/Blue RAM map — the subset PokéBench reads.

Addresses come from the pret/pokered disassembly (wram.asm symbols) and are
cross-checked against PWhiddy's Pokemon Red Experiments RAM utilities.
Gen 1 Red and Blue share this layout; Yellow does NOT.

Only documented, stable addresses are used. If you add an address, cite the
pret symbol name in a comment.
"""

# --- Player / party (pret symbols in comments) ---
PLAYER_NAME = 0xD158  # wPlayerName, 11 bytes, gen-1 text encoding, 0x50-terminated
PARTY_COUNT = 0xD163  # wPartyCount
PARTY_SPECIES = 0xD164  # wPartySpecies, 6 bytes + 0xFF terminator (internal index, not dex no.)
PARTY_MON_BASE = 0xD16B  # wPartyMons, 6 x 44-byte structs
PARTY_MON_SIZE = 44
# Offsets within each 44-byte party_struct:
MON_OFF_CUR_HP = 1  # 2 bytes, big-endian
MON_OFF_STATUS = 4
MON_OFF_LEVEL = 33
MON_OFF_MAX_HP = 34  # 2 bytes, big-endian

MONEY = 0xD347  # wPlayerMoney, 3 bytes, binary-coded decimal
BADGES = 0xD356  # wObtainedBadges, bitmask, bit 0 = Boulder

# --- Location ---
CUR_MAP = 0xD35E  # wCurMap
Y_COORD = 0xD361  # wYCoord (increases southward)
X_COORD = 0xD362  # wXCoord (increases eastward)

# Player facing. wSpriteStateData1 (0xC100) is 16 sprites x 16 bytes; the player
# is sprite 0, and offset SPRITESTATEDATA1_FACINGDIRECTION ($09) holds its facing.
PLAYER_FACING = 0xC109  # wSpriteStateData1 + SPRITESTATEDATA1_FACINGDIRECTION

# --- Battle ---
IS_IN_BATTLE = 0xD057  # wIsInBattle: 0 = no, 1 = wild, 2 = trainer

# constants/sprite_data_constants.asm: SPRITE_FACING_{DOWN,UP,LEFT,RIGHT}
FACING_NAMES = {0x00: "down", 0x04: "up", 0x08: "left", 0x0C: "right"}

BADGE_NAMES = (
    "Boulder",
    "Cascade",
    "Thunder",
    "Rainbow",
    "Soul",
    "Marsh",
    "Volcano",
    "Earth",
)

# Map IDs from pret/pokered constants/map_constants.asm.
# Curated to the S1–S4 corridor (Pallet → Pewter). Entries past VIRIDIAN_FOREST
# should be re-verified against pret before a scenario relies on them.
MAP_IDS: dict[str, int] = {
    "PALLET_TOWN": 0x00,
    "VIRIDIAN_CITY": 0x01,
    "PEWTER_CITY": 0x02,
    "CERULEAN_CITY": 0x03,
    "ROUTE_1": 0x0C,
    "ROUTE_2": 0x0D,
    "ROUTE_22": 0x21,
    "REDS_HOUSE_1F": 0x25,
    "REDS_HOUSE_2F": 0x26,
    "BLUES_HOUSE": 0x27,
    "OAKS_LAB": 0x28,
    "VIRIDIAN_POKECENTER": 0x29,
    "VIRIDIAN_MART": 0x2A,
    "VIRIDIAN_SCHOOL_HOUSE": 0x2B,
    "VIRIDIAN_NICKNAME_HOUSE": 0x2C,
    "VIRIDIAN_GYM": 0x2D,
    "DIGLETTS_CAVE_ROUTE_2": 0x2E,
    "VIRIDIAN_FOREST_NORTH_GATE": 0x2F,
    "ROUTE_2_TRADE_HOUSE": 0x30,
    "ROUTE_2_GATE": 0x31,
    "VIRIDIAN_FOREST_SOUTH_GATE": 0x32,
    "VIRIDIAN_FOREST": 0x33,
    "MUSEUM_1F": 0x34,
    "MUSEUM_2F": 0x35,
    "PEWTER_GYM": 0x36,
}

MAP_NAMES: dict[int, str] = {v: k for k, v in MAP_IDS.items()}


def map_name(map_id: int) -> str:
    """Human-readable name for a map ID, or UNKNOWN_0xNN for unmapped ones."""
    return MAP_NAMES.get(map_id, f"UNKNOWN_0x{map_id:02X}")


def decode_badges(byte: int) -> tuple[str, ...]:
    """wObtainedBadges bitmask -> badge names (bit 0 = Boulder)."""
    return tuple(name for i, name in enumerate(BADGE_NAMES) if byte & (1 << i))


def decode_facing(byte: int) -> str:
    """Player-sprite facing byte -> direction name (down/up/left/right)."""
    return FACING_NAMES.get(byte, f"unknown_0x{byte:02X}")


def decode_bcd(data: list[int] | bytes) -> int:
    """Binary-coded-decimal bytes (most significant first) -> int. Used for money."""
    value = 0
    for b in data:
        value = value * 100 + (b >> 4) * 10 + (b & 0x0F)
    return value


def decode_text(data: list[int] | bytes) -> str:
    """Minimal gen-1 text decoder (A-Z, a-z, 0-9, space); stops at 0x50 terminator."""
    out = []
    for b in data:
        if b == 0x50:
            break
        if 0x80 <= b <= 0x99:
            out.append(chr(ord("A") + b - 0x80))
        elif 0xA0 <= b <= 0xB9:
            out.append(chr(ord("a") + b - 0xA0))
        elif 0xF6 <= b <= 0xFF:
            out.append(chr(ord("0") + b - 0xF6))
        elif b == 0x7F:
            out.append(" ")
        else:
            out.append("?")
    return "".join(out)
