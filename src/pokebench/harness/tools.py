"""The agent's action surface — one tool, `press_buttons`.

This is the *whole* of what a Tier-0 agent can do: press a short ordered list
of Game Boy buttons and see the resulting screen. It is deliberately a raw
input primitive, not a smart navigation helper — the harness must not do the
agent's spatial reasoning for it, or the benchmark would measure the harness
instead of the model (CLAUDE.md working rules).

Contract (frozen for M1):
- At most 10 buttons per call. Extra buttons are dropped (`over_limit`).
- Buttons outside the 8-button Game Boy set are ignored (`invalid`); a call
  with no runnable buttons is a counted no-op. Invalid input is never an error
  the model has to recover from — it just wastes the turn, which the
  invalid-action-rate metric (M2) captures.

The executor runs against an `Environment` — anything that can press a button,
read state, screenshot, and settle. The real emulator and the test fake both
satisfy it, so the whole loop is exercised without a ROM.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from PIL import Image

from pokebench.harness.state import GameState

# The eight Game Boy inputs. PyBoy accepts exactly these strings.
VALID_BUTTONS = ("up", "down", "left", "right", "a", "b", "start", "select")
_VALID_SET = frozenset(VALID_BUTTONS)

MAX_BUTTONS = 10

# Tier-1 notes: a bounded scratchpad the agent controls. The cap is a hard
# unbounded-consumption guard (LLM10) enforced in code, not the prompt — a
# runaway notes file would blow the context budget every turn. The notes are
# re-shown to the model each turn, so they survive history-window truncation.
MAX_NOTES_CHARS = 1000

# Frames to advance with no input after a whole button sequence, so the next
# screenshot is a settled frame rather than mid-animation. The per-button press
# timing lives in the emulator; this observe-cadence is a contract-level choice.
POST_ACTION_SETTLE_FRAMES = 24


@runtime_checkable
class Environment(Protocol):
    """The minimal surface the agent loop drives (real emulator or fake)."""

    def press(self, button: str) -> None: ...
    def state(self) -> GameState: ...
    def screenshot(self) -> Image.Image | None: ...
    def settle(self, frames: int) -> None: ...


@dataclass(frozen=True)
class ToolSpec:
    """Provider-neutral tool definition; each adapter formats it for its API."""

    name: str
    description: str
    input_schema: dict


PRESS_BUTTONS = ToolSpec(
    name="press_buttons",
    description=(
        "Press an ordered list of Game Boy buttons, one after another, then "
        "receive a fresh screenshot. Provide up to 10 buttons. Valid buttons: "
        "up, down, left, right, a, b, start, select. Any other value is ignored."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "buttons": {
                "type": "array",
                "items": {"type": "string", "enum": list(VALID_BUTTONS)},
                "maxItems": MAX_BUTTONS,
                "description": "Ordered buttons to press this turn (max 10).",
            }
        },
        "required": ["buttons"],
    },
)


UPDATE_NOTES = ToolSpec(
    name="update_notes",
    description=(
        "Replace your persistent notes with the given text (kept across turns and "
        f"shown to you every turn, up to {MAX_NOTES_CHARS} characters). Use them to "
        "remember the map layout, what you have already tried, and your current plan — "
        "older turns scroll out of view, but your notes do not. Provide the FULL "
        "updated notes each time; they replace the previous notes entirely."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "maxLength": MAX_NOTES_CHARS,
                "description": "The full new notes text (replaces the old notes).",
            }
        },
        "required": ["content"],
    },
)


def tools_for_tier(tier: int) -> list[ToolSpec]:
    """The tool set for a tier. Tier-1 adds the bounded notes tool."""
    return [PRESS_BUTTONS] if tier < 1 else [PRESS_BUTTONS, UPDATE_NOTES]


@dataclass
class Notebook:
    """The agent's bounded notes buffer (Tier-1). Replace-whole-buffer, capped."""

    text: str = ""

    def update(self, content) -> dict:
        raw = content if isinstance(content, str) else ("" if content is None else str(content))
        truncated = len(raw) > MAX_NOTES_CHARS
        self.text = raw[:MAX_NOTES_CHARS]
        return {"chars": len(self.text), "truncated": truncated}


@dataclass
class PressResult:
    """Outcome of one press_buttons call — the record the trace and the next
    observation are built from."""

    requested: list = field(default_factory=list)  # raw input as given by the model
    executed: list[str] = field(default_factory=list)  # normalized buttons actually pressed
    invalid: list = field(default_factory=list)  # items ignored (unknown or over-limit)
    over_limit: bool = False  # more than MAX_BUTTONS requested
    is_noop: bool = False  # nothing runnable -> counted no-op

    def to_dict(self) -> dict:
        return {
            "requested": self.requested,
            "executed": self.executed,
            "invalid": self.invalid,
            "over_limit": self.over_limit,
            "is_noop": self.is_noop,
        }


def _normalize(button) -> str | None:
    """Lower/strip a raw button token; return None if it isn't a valid button."""
    if not isinstance(button, str):
        return None
    b = button.strip().lower()
    return b if b in _VALID_SET else None


def execute_press_buttons(env: Environment, raw_input) -> PressResult:
    """Execute a press_buttons tool call against the environment.

    `raw_input` is the model-supplied `buttons` value (expected: a list). Runs
    each valid button in order, drops anything past the 10-button cap, records
    ignored input, and always settles so the caller can screenshot a clean
    frame — even on a no-op.
    """
    requested = list(raw_input) if isinstance(raw_input, (list, tuple)) else [raw_input]

    considered = requested[:MAX_BUTTONS]
    over_limit = len(requested) > MAX_BUTTONS

    executed: list[str] = []
    invalid: list = []
    for item in considered:
        norm = _normalize(item)
        if norm is None:
            invalid.append(item)
        else:
            executed.append(norm)
            env.press(norm)
    # Buttons past the cap count as ignored input, not silent truncation.
    invalid.extend(requested[MAX_BUTTONS:])

    # Settle regardless (also refreshes the screen after a no-op turn).
    env.settle(POST_ACTION_SETTLE_FRAMES)

    return PressResult(
        requested=requested,
        executed=executed,
        invalid=invalid,
        over_limit=over_limit,
        is_noop=not executed,
    )
