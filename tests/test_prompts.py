from pokebench.harness.prompts import (
    SYSTEM_PROMPT_VERSION,
    build_system,
    system_prompt,
)


def test_base_prompt_is_tier_neutral():
    sp = system_prompt()
    assert "press_buttons" in sp
    assert "# Objective" not in sp  # objective is appended per scenario, not baked in
    assert "update_notes" not in sp  # notes are Tier-1 only


def test_tier0_system_appends_objective_only():
    s = build_system("reach Route 1", tier=0)
    assert "# Objective" in s and s.rstrip().endswith("reach Route 1")
    assert "update_notes" not in s


def test_tier1_system_adds_the_notes_addendum():
    s = build_system("reach Route 1", tier=1)
    assert "update_notes" in s and "NOTES" in s.upper()
    assert "# Objective" in s
    assert SYSTEM_PROMPT_VERSION >= 2
