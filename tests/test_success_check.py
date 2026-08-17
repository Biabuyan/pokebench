"""Unit tests for `make_success_check`'s YAML `success` clause vocabulary.

Coordinate bounds (`x_gte`/`x_lte`/`y_gte`/`y_lte`) already have end-to-end
coverage via `test_loop.py`/`test_integration.py`'s full `run_episode` runs,
but no test isolates `make_success_check` itself or exercises the `money`
clause the S4 scenario (Viridian Mart transaction) needs -- a state
`money=3000` (nothing bought yet) must NOT satisfy `money_lte: 2900`, while
`money=2900` after a purchase must.
"""

from __future__ import annotations

from pokebench.harness.state import GameState
from pokebench.runner.loop import make_success_check


def _state(*, map_name="VIRIDIAN_MART", x=4, y=4, money=3000) -> GameState:
    return GameState(
        map_id=0x2A,
        map_name=map_name,
        x=x,
        y=y,
        facing="down",
        badges=(),
        party=(),
        in_battle=0,
        money=money,
        player_name="RED",
    )


def test_money_lte_not_satisfied_before_a_purchase():
    check = make_success_check({"map": "VIRIDIAN_MART", "money_lte": 2900})
    assert check(_state(money=3000)) is False


def test_money_lte_satisfied_after_a_purchase():
    check = make_success_check({"map": "VIRIDIAN_MART", "money_lte": 2900})
    assert check(_state(money=2900)) is True


def test_money_lte_combines_with_map_clause():
    check = make_success_check({"map": "VIRIDIAN_MART", "money_lte": 2900})
    # Money dropped, but the player already left the Mart -- must not fire.
    assert check(_state(map_name="VIRIDIAN_CITY", money=2900)) is False


def test_money_lte_absent_is_a_noop_like_the_coordinate_clauses():
    check = make_success_check({"map": "VIRIDIAN_MART"})
    assert check(_state(money=3000)) is True
