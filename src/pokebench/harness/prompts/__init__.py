"""The one fixed system prompt, versioned — per tier.

`system.md` is the harness's neutral base prompt — the same bytes for every
model. `build_system(objective, tier)` appends (1) a Tier-1 addendum when the
run exposes the structured state block + notes tool, and (2) the per-scenario
objective. Neither is baked into the file: keeping them appended by the loop
means the objective survives history truncation and stays identical across
models on a given scenario/tier. Bump `SYSTEM_PROMPT_VERSION` whenever the base
file or the addendum changes, so traces record which prompt produced a run.
"""

from __future__ import annotations

from importlib.resources import files

SYSTEM_PROMPT_VERSION = 2

# Appended only at Tier 1, where the observation also carries a text state block
# and the agent's notes, and a second tool (`update_notes`) is available.
TIER1_ADDENDUM = """\
## Extra information each turn (this run)

Below the screenshot you also receive a text STATE block (your current map,
position, facing, party, and badges) and your NOTES. Use them together with the
image.

## Notes

You have a second tool, `update_notes`, that stores persistent notes shown to
you every turn. Older turns scroll out of view, but your notes do not — use them
to remember the map layout, what you have already tried, and your current plan.
Calling `update_notes` replaces your notes and does NOT move your character, so
take notes sparingly and spend most turns acting with `press_buttons`.\
"""


def system_prompt() -> str:
    """The fixed harness base system prompt (no addendum, no objective)."""
    return files(__package__).joinpath("system.md").read_text(encoding="utf-8").strip()


def build_system(objective: str, tier: int = 0) -> str:
    """Base prompt + (Tier-1 addendum) + the scenario's objective section."""
    parts = [system_prompt()]
    if tier >= 1:
        parts.append(TIER1_ADDENDUM.strip())
    parts.append(f"# Objective\n\n{objective.strip()}")
    return "\n\n".join(parts) + "\n"
