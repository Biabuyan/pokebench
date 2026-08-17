"""The rolling-history window — a frozen fairness policy, applied identically
to every model.

The full transcript is kept by the loop as the audit record; what the *model*
sees each turn is only the last N exchanges, and only the newest screenshot.
This bounds token cost and makes the memory policy a harness constant rather
than a per-model choice.

Two invariants make the window a valid request to a tool-using API:

1. It must begin with a ``user`` message (leading assistant turns are dropped).
2. A ``tool_result`` must have its matching ``tool_use`` present. When the
   window's first kept message is a ``tool_result`` whose ``tool_use`` was
   truncated away, we *unwrap* it into plain content blocks so nothing dangles.

Older screenshots are replaced with a short placeholder so exactly one image —
the current observation — reaches the model.
"""

from __future__ import annotations

from pokebench.harness.observation import (
    ImageBlock,
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)

OMITTED_IMAGE_PLACEHOLDER = "[screenshot from an earlier turn omitted]"


def _strip_images(blocks: list, keep_images: bool) -> list:
    """Copy content blocks, replacing images with a placeholder unless kept.

    Recurses into tool_result blocks so an old tool_result keeps its text (and
    its wrapper, preserving tool_use/tool_result pairing) but loses its image.
    """
    out: list = []
    for b in blocks:
        if isinstance(b, ImageBlock):
            out.append(b if keep_images else TextBlock(OMITTED_IMAGE_PLACEHOLDER))
        elif isinstance(b, ToolResultBlock):
            out.append(
                ToolResultBlock(
                    tool_use_id=b.tool_use_id,
                    content=_strip_images(b.content, keep_images),
                    is_error=b.is_error,
                )
            )
        else:  # TextBlock, ToolUseBlock — kept as-is (frozen, safe to share)
            out.append(b)
    return out


def _unwrap_tool_results(blocks: list) -> list:
    """Replace any tool_result block with its inner content (plain blocks).

    Used only on the window's first kept message, whose matching tool_use may
    have been truncated away — turning it into an ordinary user observation.
    """
    out: list = []
    for b in blocks:
        if isinstance(b, ToolResultBlock):
            out.extend(b.content)
        else:
            out.append(b)
    return out


def build_window(messages: list[Message], max_turns: int) -> list[Message]:
    """Return the last ``max_turns`` exchanges, image-stripped and API-valid.

    ``messages`` is the full transcript (strictly alternating user/assistant,
    starting with a user observation). The returned list is safe to send: it
    starts with a user message, has no dangling tool_result, and carries at
    most one screenshot (the most recent user turn).
    """
    if not messages:
        return []

    keep = max_turns * 2  # max_turns user + max_turns assistant messages
    window = list(messages[-keep:]) if keep < len(messages) else list(messages)

    # Invariant 1: start on a user message.
    while window and window[0].role != "user":
        window.pop(0)
    if not window:
        return []

    last_user_idx = max(i for i, m in enumerate(window) if m.role == "user")

    out: list[Message] = []
    for i, m in enumerate(window):
        content = m.content
        if i == 0:
            content = _unwrap_tool_results(content)  # Invariant 2
        content = _strip_images(content, keep_images=(i == last_user_idx))
        out.append(Message(m.role, content))
    return out


# Re-exported so the loop can build assistant turns without importing the block
# types from two places.
__all__ = [
    "build_window",
    "Message",
    "TextBlock",
    "ToolUseBlock",
    "ToolResultBlock",
    "OMITTED_IMAGE_PLACEHOLDER",
]
