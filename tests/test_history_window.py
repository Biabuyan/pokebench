"""The rolling-history window is a frozen fairness policy AND must produce a
valid tool-using request. These tests pin both."""

from pokebench.harness.observation import (
    ImageBlock,
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from pokebench.runner.history import OMITTED_IMAGE_PLACEHOLDER, build_window


def _transcript():
    """A 5-message transcript: initial obs, then two act/observe exchanges."""
    up = {"buttons": ["up"]}
    return [
        Message("user", [ImageBlock(b"img0"), TextBlock("obs0")]),
        Message("assistant", [TextBlock("t1"), ToolUseBlock("id1", "press_buttons", up)]),
        Message("user", [ToolResultBlock("id1", [ImageBlock(b"img1"), TextBlock("obs1")])]),
        Message("assistant", [ToolUseBlock("id2", "press_buttons", up)]),
        Message("user", [ToolResultBlock("id2", [ImageBlock(b"img2"), TextBlock("obs2")])]),
    ]


def _count_images(messages):
    n = 0

    def walk(blocks):
        nonlocal n
        for b in blocks:
            if isinstance(b, ImageBlock):
                n += 1
            elif isinstance(b, ToolResultBlock):
                walk(b.content)

    for m in messages:
        walk(m.content)
    return n


def test_empty_transcript_returns_empty():
    assert build_window([], 10) == []


def test_only_the_current_turn_keeps_its_screenshot():
    window = build_window(_transcript(), max_turns=10)
    assert _count_images(window) == 1  # exactly the most recent observation
    # the old image is replaced by a placeholder, not dropped silently
    assert any(
        isinstance(b, TextBlock) and b.text == OMITTED_IMAGE_PLACEHOLDER
        for b in window[0].content
    )


def test_window_always_starts_with_a_user_message():
    # max_turns=2 -> keep last 4 -> [assistant, user, assistant, user];
    # leading assistant is dropped so the request starts on a user turn.
    window = build_window(_transcript(), max_turns=2)
    assert window[0].role == "user"


def test_dangling_tool_result_is_unwrapped():
    # Keep 4 -> starts at m2 (a tool_result whose tool_use m1 was truncated).
    window = build_window(_transcript(), max_turns=2)
    first = window[0]
    assert not any(isinstance(b, ToolResultBlock) for b in first.content)
    # its inner text survives the unwrap
    assert any(isinstance(b, TextBlock) and "obs1" in b.text for b in first.content)


def test_surviving_tool_results_keep_their_pairing():
    window = build_window(_transcript(), max_turns=2)
    # the last user message is still a tool_result referencing the assistant
    # (id2) that IS in the window
    last = window[-1]
    tr = [b for b in last.content if isinstance(b, ToolResultBlock)]
    assert len(tr) == 1 and tr[0].tool_use_id == "id2"
    prior_tool_use_ids = {
        b.id
        for m in window
        if m.role == "assistant"
        for b in m.content
        if isinstance(b, ToolUseBlock)
    }
    assert tr[0].tool_use_id in prior_tool_use_ids


def test_tiny_window_unwraps_the_single_tool_result_and_keeps_the_image():
    # max_turns=1 -> keep last 2 -> [assistant, user] -> drop assistant -> [user].
    window = build_window(_transcript(), max_turns=1)
    assert len(window) == 1 and window[0].role == "user"
    assert not any(isinstance(b, ToolResultBlock) for b in window[0].content)
    assert _count_images(window) == 1  # it is the current turn, so its image stays


def test_full_transcript_preserves_message_count():
    window = build_window(_transcript(), max_turns=10)
    assert len(window) == 5
    assert window[0].role == "user" and window[-1].role == "user"
