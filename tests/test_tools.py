from pokebench.harness.tools import (
    MAX_BUTTONS,
    MAX_NOTES_CHARS,
    PRESS_BUTTONS,
    UPDATE_NOTES,
    VALID_BUTTONS,
    Notebook,
    execute_press_buttons,
    tools_for_tier,
)


class RecordEnv:
    """Minimal Environment that records presses/settles (no ROM)."""

    def __init__(self):
        self.pressed = []
        self.settled = 0

    def press(self, button):
        self.pressed.append(button)

    def state(self):
        return None

    def screenshot(self):
        return None

    def settle(self, frames):
        self.settled += frames


def test_valid_buttons_execute_in_order():
    env = RecordEnv()
    r = execute_press_buttons(env, ["up", "up", "a"])
    assert r.executed == ["up", "up", "a"]
    assert env.pressed == ["up", "up", "a"]
    assert not r.invalid and not r.is_noop and not r.over_limit


def test_normalizes_case_and_whitespace():
    env = RecordEnv()
    r = execute_press_buttons(env, ["A", " Up ", "START"])
    assert r.executed == ["a", "up", "start"]
    assert env.pressed == ["a", "up", "start"]


def test_invalid_buttons_are_ignored_not_pressed():
    env = RecordEnv()
    r = execute_press_buttons(env, ["x", "up", "jump"])
    assert r.executed == ["up"]
    assert r.invalid == ["x", "jump"]
    assert env.pressed == ["up"]  # invalid tokens never reach the emulator


def test_empty_input_is_a_counted_noop_that_still_settles():
    env = RecordEnv()
    r = execute_press_buttons(env, [])
    assert r.is_noop and r.executed == []
    assert env.settled > 0  # screen still refreshed so the next frame is current


def test_all_invalid_is_a_noop():
    env = RecordEnv()
    r = execute_press_buttons(env, ["x", "y"])
    assert r.is_noop and r.invalid == ["x", "y"] and env.pressed == []


def test_over_limit_drops_extra_buttons():
    env = RecordEnv()
    r = execute_press_buttons(env, ["up"] * (MAX_BUTTONS + 3))
    assert r.over_limit is True
    assert len(r.executed) == MAX_BUTTONS
    assert len(env.pressed) == MAX_BUTTONS
    assert len(r.invalid) == 3  # the dropped extras are recorded, not silent


def test_non_list_input_is_wrapped():
    env = RecordEnv()
    r = execute_press_buttons(env, "up")  # a bare string, not a list
    assert r.executed == ["up"]


def test_settle_called_after_sequence():
    env = RecordEnv()
    execute_press_buttons(env, ["up", "down"])
    assert env.settled > 0


def test_schema_enumerates_exactly_the_valid_buttons():
    enum = PRESS_BUTTONS.input_schema["properties"]["buttons"]["items"]["enum"]
    assert tuple(enum) == VALID_BUTTONS
    assert PRESS_BUTTONS.input_schema["properties"]["buttons"]["maxItems"] == MAX_BUTTONS


def test_tools_for_tier():
    assert [t.name for t in tools_for_tier(0)] == ["press_buttons"]
    assert [t.name for t in tools_for_tier(1)] == ["press_buttons", "update_notes"]


def test_notebook_replaces_and_reports():
    nb = Notebook()
    assert nb.text == ""
    r = nb.update("stairs are top-right at (7,1)")
    assert nb.text == "stairs are top-right at (7,1)"
    assert r == {"chars": len(nb.text), "truncated": False}
    nb.update("new plan")  # replace-whole, not append
    assert nb.text == "new plan"


def test_notebook_caps_at_max_chars():
    nb = Notebook()
    r = nb.update("x" * (MAX_NOTES_CHARS + 500))
    assert len(nb.text) == MAX_NOTES_CHARS and r["truncated"] is True


def test_notebook_tolerates_non_string():
    nb = Notebook()
    assert nb.update(None)["chars"] == 0
    nb.update(123)
    assert nb.text == "123"


def test_update_notes_schema_caps_length():
    assert UPDATE_NOTES.input_schema["properties"]["content"]["maxLength"] == MAX_NOTES_CHARS
    assert UPDATE_NOTES.input_schema["required"] == ["content"]
