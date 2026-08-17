from io import BytesIO

from PIL import Image

from pokebench.harness.observation import (
    ImageBlock,
    TextBlock,
    build_observation,
    describe_action,
    encode_screenshot,
    format_state_block,
    observation_fingerprint,
)
from pokebench.harness.tools import PressResult
from tests.fake_world import build_pallet_world


def _state():
    return build_pallet_world().state()


def test_encode_screenshot_upscales_by_fixed_factor():
    img = Image.new("RGB", (160, 144), (0, 0, 0))
    png = encode_screenshot(img, scale=3)
    decoded = Image.open(BytesIO(png))
    assert decoded.size == (480, 432)
    assert decoded.format == "PNG"


def test_tier0_observation_is_image_plus_text_no_state_leak():
    blocks = build_observation(_state(), Image.new("RGB", (160, 144)), None, tier=0)
    assert isinstance(blocks[0], ImageBlock)
    assert isinstance(blocks[-1], TextBlock)
    text = blocks[-1].text
    assert "Current screen." in text
    assert "STATE:" not in text  # Tier 0 is pure vision — no coordinates leaked
    assert "NOTES" not in text


def test_intro_overrides_the_opening_line():
    blocks = build_observation(
        _state(), None, None, tier=0, intro="This is the first turn of the scenario."
    )
    assert "first turn" in blocks[-1].text


def test_tier1_observation_shows_notes_empty_and_filled():
    empty = build_observation(_state(), None, None, tier=1)[-1].text
    assert "NOTES" in empty and "(empty)" in empty
    filled = build_observation(_state(), None, None, tier=1, notes="stairs are top-right")[-1].text
    assert "stairs are top-right" in filled


def test_tier1_observation_appends_state_block():
    blocks = build_observation(_state(), Image.new("RGB", (160, 144)), None, tier=1)
    text = blocks[-1].text
    assert "STATE:" in text
    assert "REDS_HOUSE_2F" in text
    assert "facing: down" in text  # fake world starts facing down


def test_tier1_state_block_tracks_facing_after_a_move():
    w = build_pallet_world()
    w.press("right")  # turns to face right (blocked or not)
    blocks = build_observation(w.state(), Image.new("RGB", (160, 144)), None, tier=1)
    assert "facing: right" in blocks[-1].text


def test_describe_action_reports_only_the_agents_own_action():
    r = PressResult(requested=["up", "up"], executed=["up", "up"])
    assert describe_action(r) == "You pressed: up, up."

    r = PressResult(requested=["x", "up"], executed=["up"], invalid=["x"])
    line = describe_action(r)
    assert "You pressed: up." in line and "Ignored invalid input: x." in line

    r = PressResult(executed=[], is_noop=True)
    assert describe_action(r) == "No valid buttons were pressed."

    r = PressResult(executed=["up"], over_limit=True)
    assert "10 buttons" in describe_action(r)


def test_observation_without_screenshot_is_text_only():
    blocks = build_observation(_state(), None, None, tier=0)
    assert len(blocks) == 1 and isinstance(blocks[0], TextBlock)


def test_state_block_lists_party():
    block = format_state_block(_state())
    assert "party: 1 pokemon" in block
    assert "lv8" in block  # the fake world's starter is level 8


def test_observation_fingerprint_is_stable_for_the_same_scene():
    img = Image.new("RGB", (160, 144), (10, 20, 30))
    same = Image.new("RGB", (160, 144), (10, 20, 30))
    diff = Image.new("RGB", (160, 144), (99, 20, 30))
    s = _state()
    assert observation_fingerprint(img, s, 0) == observation_fingerprint(same, s, 0)
    assert observation_fingerprint(img, s, 0) != observation_fingerprint(diff, s, 0)


def test_observation_fingerprint_includes_state_at_tier1():
    from dataclasses import replace

    img = Image.new("RGB", (160, 144), (0, 0, 0))
    s1 = _state()
    s2 = replace(s1, x=s1.x + 1)  # same screenshot, different RAM position
    # Tier 0 ignores state -> same hash; Tier 1 folds it in -> different hash
    assert observation_fingerprint(img, s1, 0) == observation_fingerprint(img, s2, 0)
    assert observation_fingerprint(img, s1, 1) != observation_fingerprint(img, s2, 1)
