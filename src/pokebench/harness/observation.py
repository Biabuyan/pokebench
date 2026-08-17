"""The observation contract — PokéBench's fairness spine.

Every model, every provider, sees the game through *exactly* this: a
fixed-upscale screenshot of the current Game Boy screen plus a short
previous-action result line. Nothing here is model-specific, and nothing may
be — harness neutrality is the benchmark (see CLAUDE.md / SECURITY.md). If the
observation a model receives is not built by this module, the comparison is
invalid.

Two tool tiers (reported separately, never mixed):

- **Tier 0** — screenshot + previous-action result only (pure vision agency).
- **Tier 1** — additionally a structured state block read from RAM (map,
  coordinates, party, badges). The formatter lives here; the bounded notes
  tool that completes Tier-1 lands in week 3.

This module also defines the provider-neutral *conversation* types (message +
content blocks). The harness assembles these; each agent adapter translates
them to its provider's wire format. Keeping the conversation model here — not
in any one adapter — is what makes the history policy identical across models.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from io import BytesIO
from typing import TYPE_CHECKING

from PIL import Image

from pokebench.harness.state import GameState
from pokebench.harness.tools import MAX_NOTES_CHARS

if TYPE_CHECKING:
    from pokebench.harness.tools import PressResult

# The Game Boy screen is 160x144. Upscale by a fixed integer factor with
# nearest-neighbour so pixels stay crisp and every model sees an identical
# image. This is part of the frozen contract — changing it invalidates
# cross-run comparisons.
SCREENSHOT_SCALE = 3
SCREENSHOT_MEDIA_TYPE = "image/png"


# --- Provider-neutral conversation content blocks -------------------------
# Modelled closely on the Anthropic content shape (the only M1 provider), but
# kept as plain dataclasses so OpenAI/Google/Ollama adapters (M3) can translate
# them without the harness knowing any provider details.


@dataclass(frozen=True)
class TextBlock:
    text: str


@dataclass(frozen=True)
class ImageBlock:
    png: bytes  # PNG-encoded image bytes


@dataclass(frozen=True)
class ToolUseBlock:
    id: str
    name: str
    input: dict
    # Opaque provider state echoed back on replay (see agents.base.ToolCall).
    provider_state: dict | None = None


@dataclass(frozen=True)
class ToolResultBlock:
    tool_use_id: str
    content: list  # of TextBlock | ImageBlock
    is_error: bool = False


Block = TextBlock | ImageBlock | ToolUseBlock | ToolResultBlock


@dataclass
class Message:
    role: str  # "user" | "assistant"
    content: list = field(default_factory=list)


# --- Screenshot encoding --------------------------------------------------


def encode_screenshot(image: Image.Image, scale: int = SCREENSHOT_SCALE) -> bytes:
    """Fixed-upscale a PIL screenshot and return PNG bytes (the frozen format)."""
    if scale != 1:
        image = image.resize((image.width * scale, image.height * scale), Image.NEAREST)
    buf = BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


# --- Text half of the observation ----------------------------------------


def describe_action(result: PressResult) -> str:
    """One neutral line reporting what the previous press_buttons call did.

    Tier-0-safe: reports only the agent's own action (which buttons ran, which
    were ignored) — never game state. It is procedural feedback, not a hint.
    """
    if result.executed:
        line = "You pressed: " + ", ".join(result.executed) + "."
    else:
        line = "No valid buttons were pressed."
    if result.invalid:
        line += " Ignored invalid input: " + ", ".join(str(b) for b in result.invalid) + "."
    if result.over_limit:
        line += " (At most 10 buttons run per turn; extras were dropped.)"
    return line


def format_state_block(state: GameState) -> str:
    """Tier-1 structured state block, read from RAM (see harness/state.py).

    Deterministic key: value lines so the model — and the trace reader — get a
    stable, parseable view. Not shown at Tier 0.
    """
    lines = [
        "STATE:",
        f"  map: {state.map_name} (0x{state.map_id:02X})",
        f"  position: x={state.x} y={state.y}",
        f"  facing: {state.facing}",
        f"  in_battle: {state.in_battle}",
        f"  badges: {len(state.badges)} ({', '.join(state.badges) or 'none'})",
        f"  party: {state.party_count} pokemon",
    ]
    for i, mon in enumerate(state.party):
        lines.append(
            f"    #{i + 1} species=0x{mon.species_id:02X} "
            f"lv{mon.level} hp={mon.hp}/{mon.max_hp}"
        )
    return "\n".join(lines)


def observation_fingerprint(screenshot: Image.Image | None, state: GameState, tier: int) -> str:
    """A stable hash of *what the model perceives of the world* this turn.

    Hashes the screenshot pixels (and, at Tier 1, the RAM state block) — but NOT
    the volatile previous-action line — so two turns with the same on-screen
    situation share a fingerprint. That is the raw signal the M2 stuck-loop index
    is built on (plan #7: "observation hash"), and it makes runs replay-verifiable.
    """
    h = hashlib.sha1()
    if screenshot is not None:
        h.update(screenshot.tobytes())
    if tier >= 1:
        h.update(format_state_block(state).encode("utf-8"))
    return h.hexdigest()


def build_observation(
    state: GameState,
    screenshot: Image.Image | None,
    prev_action: PressResult | None = None,
    tier: int = 0,
    notes: str = "",
    intro: str | None = None,
) -> list[Block]:
    """Build the per-turn observation as neutral content blocks.

    Returns ``[ImageBlock?, TextBlock]`` — the image first (when available),
    then the text. The opening line reports what just happened: ``intro`` if the
    caller supplies one (first turn / a notes turn / a nudge), else the previous
    press result. At Tier 1 the text also carries the structured state block and
    the agent's current notes (both survive history truncation, since they are
    rebuilt fresh every turn). The loop wraps this in a plain user message on the
    first turn and in a ``ToolResultBlock`` thereafter.
    """
    blocks: list[Block] = []
    if screenshot is not None:
        blocks.append(ImageBlock(encode_screenshot(screenshot)))

    if intro is not None:
        line = intro
    elif prev_action is not None:
        line = describe_action(prev_action)
    else:
        line = "Current screen."
    text = line + "\nThe image above is the current Game Boy screen."
    if tier >= 1:
        text += "\n\n" + format_state_block(state)
        text += (
            f"\n\nNOTES (your persistent memory, max {MAX_NOTES_CHARS} chars):\n"
            + (notes.strip() or "(empty)")
        )
    blocks.append(TextBlock(text))
    return blocks
