"""`pokebench spike-s1` CLI wiring that doesn't need a ROM: the out-dir default.

Task B (2026-08-06) generalised `cmd_spike_s1` to any scenario with a
`spike_route` block (it already read `--scenario` generically). One thing
still hardcoded `runs/spike-s1`: the `--out-dir` default, which would silently
dump an S3 (or any other) spike's trace into the S1-named directory. This
pins the fix at the pure-function seam so it's testable without the ROM.
"""

from __future__ import annotations

from pathlib import Path

from pokebench.cli import _spike_out_base, _spike_verdict
from pokebench.harness.state import GameState


def _state(*, map_name="VIRIDIAN_MART", money=3000) -> GameState:
    return GameState(
        map_id=0x2A,
        map_name=map_name,
        x=2,
        y=5,
        facing="left",
        badges=(),
        party=(),
        in_battle=0,
        money=money,
        player_name="RED",
    )


def test_spike_verdict_map_only_scenario_unaffected():
    """S1/S2/S3's `success:` block is `map` only -- `_spike_verdict` must
    keep reporting exactly what the old inline `final.map_name ==
    success_map` comparison did (no regression on the existing scenarios)."""
    scenario = {"success": {"map": "VIRIDIAN_POKECENTER"}}
    assert _spike_verdict(scenario, _state(map_name="VIRIDIAN_POKECENTER")) is True
    assert _spike_verdict(scenario, _state(map_name="ROUTE_1")) is False


def test_spike_verdict_checks_money_not_just_map():
    """The bug this closes (S4, 2026-08-15): the old inline check was
    `final.map_name == success_map` ONLY -- for a scenario whose `success:`
    also has a `money_lte` clause, that would report PASS the instant the
    map matched, even if no purchase had actually happened. A real oracle
    run must not be able to lie about this (CLAUDE.md: "an unverified route
    is worse than none" applies just as much to an unverified PREDICATE)."""
    scenario = {"success": {"map": "VIRIDIAN_MART", "money_lte": 2900}}
    assert _spike_verdict(scenario, _state(money=3000)) is False  # nothing bought yet
    assert _spike_verdict(scenario, _state(money=2800)) is True  # purchase completed


def test_out_dir_defaults_from_scenario_id_when_not_given():
    out = _spike_out_base(None, "s3_viridian_forest", "20260806-101500")
    assert out == Path("runs/spike-s3_viridian_forest/20260806-101500")


def test_out_dir_defaults_differently_per_scenario():
    """The bug this closes: two different scenarios must not collide on the
    same default directory."""
    s1 = _spike_out_base(None, "s1_exit_pallet", "20260806-101500")
    s3 = _spike_out_base(None, "s3_viridian_forest", "20260806-101500")
    assert s1 != s3


def test_explicit_out_dir_overrides_the_scenario_default():
    out = _spike_out_base(Path("custom/dir"), "s3_viridian_forest", "20260806-101500")
    assert out == Path("custom/dir/20260806-101500")
