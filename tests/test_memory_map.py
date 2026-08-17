from pokebench.harness import memory_map as mm


def test_decode_badges_empty():
    assert mm.decode_badges(0) == ()


def test_decode_badges_bits():
    assert mm.decode_badges(0b00000101) == ("Boulder", "Thunder")
    assert mm.decode_badges(0xFF) == mm.BADGE_NAMES


def test_decode_bcd_money():
    assert mm.decode_bcd([0x01, 0x23, 0x45]) == 12345
    assert mm.decode_bcd([0x00, 0x30, 0x00]) == 3000
    assert mm.decode_bcd([0x00, 0x00, 0x00]) == 0


def test_map_name_known_and_unknown():
    assert mm.map_name(0x00) == "PALLET_TOWN"
    assert mm.map_name(0x0C) == "ROUTE_1"
    assert mm.map_name(0xEE) == "UNKNOWN_0xEE"


def test_decode_text_red():
    # R, E, D, terminator, junk beyond the terminator is ignored
    assert mm.decode_text([0x91, 0x84, 0x83, 0x50, 0x91]) == "RED"


def test_decode_text_digits_and_space():
    assert mm.decode_text([0xF6, 0x7F, 0xFF, 0x50]) == "0 9"


def test_decode_facing_known_and_unknown():
    assert mm.decode_facing(0x00) == "down"
    assert mm.decode_facing(0x04) == "up"
    assert mm.decode_facing(0x08) == "left"
    assert mm.decode_facing(0x0C) == "right"
    assert mm.decode_facing(0x02) == "unknown_0x02"
