import json
from pathlib import Path

from pokebench.agents.base import AgentResponse, TokenUsage, ToolCall, get_model
from pokebench.metrics import merge_rows, result_row, score_run, write_results
from pokebench.metrics.results import RESULTS_SCHEMA, aggregate, median
from pokebench.metrics.score import RunMetrics, _reasoning_provenance
from pokebench.replay import Run
from pokebench.runner.bench import run_seeds
from pokebench.runner.loop import Caps, make_success_check
from tests.test_loop import ScriptedAgent, _corridor_world


def _rec(
    turn, hashv, x, y, *, executed=None, invalid=None, is_noop=False, tool=True, success=False,
    map_id=38, in_battle=0,
):
    action = None
    if tool:
        action = {
            "executed": executed or [],
            "invalid": invalid or [],
            "is_noop": is_noop,
            "over_limit": False,
        }
    return {
        "turn": turn,
        "usage": {"input_tokens": 100, "output_tokens": 10},
        "cost_usd": 0.001,
        "cumulative": {"usd": round(0.001 * turn, 6)},
        "tool_call": {"name": "press_buttons"} if tool else None,
        "action": action,
        "observation_hash": hashv,
        "state": {
            "map_name": "REDS_HOUSE_2F", "map_id": map_id, "x": x, "y": y, "facing": "down",
            "in_battle": in_battle,
        },
        "success": success,
    }


def _sample_run():
    records = [
        _rec(1, "h1", 1, 0, executed=["right"]),
        _rec(2, "h2", 1, 7, executed=["left"]),  # stuck turns 2-4
        _rec(3, "h2", 1, 7, executed=["down"]),
        _rec(4, "h2", 1, 7, executed=["down"]),
        _rec(5, "h3", 0, 5, tool=False),  # no tool call -> invalid
        _rec(6, "h4", 0, 0, executed=["up"], success=True),
    ]
    return Run(
        dir=Path("x"),
        meta={"scenario": "s1", "model": "haiku", "tier": 1},
        records=records,
        summary={"success": True, "stop_reason": "success"},
    )


def test_score_run_computes_the_five_metrics():
    m = score_run(_sample_run())
    assert (m.scenario, m.model, m.tier) == ("s1", "haiku", 1)
    assert m.success and m.steps_to_success == 6 and m.turns == 6
    assert m.input_tokens == 600 and m.output_tokens == 60
    assert m.cost_usd == 0.006
    assert m.stuck_index == 0.5  # 3 of 6 turns stuck at (1,7)
    assert m.invalid_rate == round(1 / 6, 4)  # one no-tool turn
    assert m.idle_rate == 0.4  # 2 of 5 transitions kept the same tile
    assert m.tiles_explored == 4  # (1,0), (1,7), (0,5), (0,0)


def test_score_run_carries_run_provenance():
    """cap_turns/reasoning come off meta.json — they say what a row may be
    compared against (M3 turn-matched vs M2 fixed-budget)."""
    run = _sample_run()
    run.meta.update({"caps": {"max_turns": 100}, "reasoning": True})
    m = score_run(run)
    assert m.cap_turns == 100 and m.reasoning is True

    # Older traces predate both keys; absent is None, never a fabricated value.
    bare = score_run(_sample_run())
    assert bare.cap_turns is None and bare.reasoning is None


def test_score_run_reasoning_provenance_distinguishes_off_from_not_applicable():
    """meta.json lacking `reasoning` means two different things depending on
    provider. Only the Anthropic adapter conditions its request on the flag
    (`agents/anthropic.py`: `if getattr(self.config, "reasoning", False):` --
    the getattr default is literally False), so an anthropic-provider trace
    missing the key proves thinking was never sent, not merely "unknown".
    Every other adapter never reads the flag at all (grep confirms only
    anthropic.py references `self.config.reasoning`), so its absence there
    says nothing about deliberation -- the vendor's own default governs. Both
    look identical through `reasoning` alone; `reasoning_provenance` is what
    tells them apart."""
    sonnet_style = _sample_run()
    sonnet_style.meta.update({"provider": "anthropic"})  # no "reasoning" key
    vendor_default_style = _sample_run()
    vendor_default_style.meta.update({"provider": "openai"})  # no "reasoning" key either

    m_sonnet = score_run(sonnet_style)
    m_vendor = score_run(vendor_default_style)
    assert m_sonnet.reasoning is None and m_vendor.reasoning is None  # identical today
    assert m_sonnet.reasoning_provenance == "off_unrecorded"
    assert m_vendor.reasoning_provenance == "not_applicable"
    assert m_sonnet.reasoning_provenance != m_vendor.reasoning_provenance

    # An explicitly recorded flag is passed through unchanged.
    recorded_on = _sample_run()
    recorded_on.meta.update({"provider": "anthropic", "reasoning": True})
    assert score_run(recorded_on).reasoning_provenance == "recorded_on"

    recorded_off = _sample_run()
    recorded_off.meta.update({"provider": "anthropic", "reasoning": False})
    assert score_run(recorded_off).reasoning_provenance == "recorded_off"


def test_reasoning_provenance_checks_provider_bindingness_before_recorded():
    """`runner/loop.py` writes `meta["reasoning"]` for EVERY provider (it reads
    `getattr(agent.config, "reasoning", None)`, and `ModelConfig.reasoning`
    defaults to `True`), so `recorded` is true for essentially every current
    trace regardless of provider. Only the Anthropic adapter ever conditions
    its request on the flag (verified by grep: `getattr(self.config,
    "reasoning"` appears in exactly one adapter, `agents/anthropic.py`) --
    every other provider reasons or not by its own vendor default and never
    reads it. So a `recorded=True` from a non-Anthropic trace says nothing
    about deliberation and must NOT be trusted as `recorded_on`; provider
    bindingness has to be checked FIRST, before `recorded` is even consulted.
    This was live: `_reasoning_provenance('ollama', True, True)` returned
    `'recorded_on'`, asserting deliberation for a model whose adapter never
    reads the flag at all."""
    for provider in ("ollama", "openai", "google"):
        assert _reasoning_provenance(provider, True, True) == "not_applicable"
        assert _reasoning_provenance(provider, False, True) == "not_applicable"

    # The one provider where the flag genuinely binds is unaffected.
    assert _reasoning_provenance("anthropic", True, True) == "recorded_on"
    assert _reasoning_provenance("anthropic", False, False) == "off_unrecorded"

    # Unknown provider, nothing recorded: still "unknown", not a guess.
    assert _reasoning_provenance("", None, False) == "unknown"


def test_score_run_failure_has_no_steps():
    run = _sample_run()
    run.records[-1]["success"] = False
    run.summary["success"] = False
    m = score_run(run)
    assert not m.success and m.steps_to_success is None


def _diag_run(records, stop_reason="max_turns", success=False):
    return Run(
        dir=Path("x"),
        meta={"scenario": "s1", "model": "haiku", "tier": 0},
        records=records,
        summary={"success": success, "stop_reason": stop_reason},
    )


def test_turns_since_new_tile_distinguishes_frozen_from_progressing_from_battle():
    """The 'was it turn-limited?' diagnostic (HANDOFF: sonnet S1 seed0 sat at
    REDS_HOUSE_2F (5,1) turns 10-100 vs gemini S2 seed2 still finding new
    ground on turn 99/100). Four shapes, and they must order correctly:
    frozen >> battle-heavy-but-progressing > still-progressing == 0, success
    always None.
    """
    # 1. Frozen at the cap: 10 distinct tiles, then 90 turns parked on the last one.
    frozen_records = [_rec(t, f"h{t}", t, 0) for t in range(1, 11)]
    frozen_records += [_rec(t, f"h{t}", 10, 0) for t in range(11, 101)]
    frozen = score_run(_diag_run(frozen_records))
    assert frozen.turns_since_new_tile == 90

    # 2. Progressing at the cap: a fresh tile every single turn, including a
    # map transition at turn 81 (new map_id makes even overlapping x/y "new").
    progressing_records = [_rec(t, f"h{t}", t, 0, map_id=38) for t in range(1, 81)]
    progressing_records += [
        _rec(t, f"h{t}", t - 80, 0, map_id=1) for t in range(81, 101)
    ]
    progressing = score_run(_diag_run(progressing_records))
    assert progressing.turns_since_new_tile == 0

    # 3. Battle-heavy but actually progressing: 5 new tiles, then 45 turns of
    # battle frozen on the last one (in_battle must NOT count toward the
    # elapsed total -- a naive raw-turn-distance calc would report 50, not 5),
    # then 5 more non-battle turns revisiting already-seen ground.
    battle_records = [_rec(t, f"h{t}", t, 0) for t in range(1, 6)]
    battle_records += [_rec(t, f"h{t}", 5, 0, in_battle=1) for t in range(6, 51)]
    post_battle_x = [3, 4, 3, 4, 3]  # all already seen at turns 3/4
    battle_records += [
        _rec(50 + i, f"pb{i}", post_battle_x[i - 1], 0) for i in range(1, 6)
    ]
    battle_heavy = score_run(_diag_run(battle_records))
    assert battle_heavy.turns_since_new_tile == 5  # not 50 -- battle turns excluded

    # 4. Success: the question does not apply.
    success_records = [_rec(t, f"h{t}", t, 0, success=(t == 10)) for t in range(1, 11)]
    succeeded = score_run(_diag_run(success_records, stop_reason="success", success=True))
    assert succeeded.turns_since_new_tile is None

    # Order: still-progressing < battle-heavy < frozen. The battle-heavy run
    # must read close to "progressing", never anywhere near "frozen" -- that
    # confusion is exactly the trap this metric exists to avoid.
    assert progressing.turns_since_new_tile < battle_heavy.turns_since_new_tile
    assert battle_heavy.turns_since_new_tile < frozen.turns_since_new_tile


def test_median():
    assert median([3, 1, 2]) == 2
    assert median([1, 2, 3, 4]) == 2.5
    assert median([]) is None
    assert median([None, 2, 4]) == 3.0  # None filtered out


def _m(success, steps, turns=10, cost=0.01, stuck=0.2, invalid=0.1, idle=0.5, tiles=5,
       stop="x", cap_turns=100, reasoning=True, dead=0.0, ceiling=0.0, tsnt=0,
       reasoning_provenance="recorded_on"):
    return RunMetrics(
        scenario="s1", model="haiku", tier=0, success=success, stop_reason=stop,
        turns=turns, steps_to_success=steps, input_tokens=1000, output_tokens=50,
        cost_usd=cost, stuck_index=stuck, invalid_rate=invalid, idle_rate=idle,
        tiles_explored=tiles, no_tool_call_rate=dead, ceiling_hit_rate=ceiling,
        turns_since_new_tile=tsnt,
        cap_turns=cap_turns, reasoning=reasoning, reasoning_provenance=reasoning_provenance,
    )


def test_aggregate_medians_over_seeds():
    agg = aggregate([_m(True, 10, turns=10), _m(True, 20, turns=20), _m(False, None, turns=30)])
    assert agg["seeds"] == 3
    assert agg["success_rate"] == round(2 / 3, 3)
    assert agg["success_at_cap"] is True  # 2 of 3 = majority
    assert agg["median_steps_to_success"] == 15.0  # median of [10, 20], failure excluded
    assert agg["median_turns"] == 20  # median of [10, 20, 30]
    assert "median_idle_rate" in agg and "median_tiles_explored" in agg


def test_aggregate_reports_provenance_and_stop_reasons():
    agg = aggregate([_m(False, None, stop="max_turns"), _m(False, None, stop="max_turns"),
                     _m(False, None, stop="max_usd")])
    assert agg["cap_turns"] == 100 and agg["reasoning"] is True
    assert agg["stop_reasons"] == {"max_turns": 2, "max_usd": 1}


def test_aggregate_refuses_to_claim_provenance_seeds_disagree_on():
    """Mixed conditions must read as unknown, not as one seed's value —
    otherwise a row silently claims a comparability it does not have."""
    agg = aggregate([_m(False, None, cap_turns=100, reasoning=True),
                     _m(False, None, cap_turns=300, reasoning=False)])
    assert agg["cap_turns"] is None and agg["reasoning"] is None


def test_reasoning_field_distinguishes_off_from_not_recorded():
    """results.json's sonnet rows carry `reasoning: null` -- identical to
    gpt's/gemini's rows, which are null for the OPPOSITE reason (those vendors
    reason by default and ignore the flag). `_unanimous` alone cannot tell
    them apart (`aggregate([_m(reasoning=None)]*3)["reasoning"]` is `None`
    either way); `reasoning_provenance` is the additive field that recovers
    the distinction in the artifact itself instead of only in prose."""
    sonnet_row = aggregate(
        [_m(False, None, reasoning=None, reasoning_provenance="off_unrecorded")] * 3
    )
    vendor_row = aggregate(
        [_m(False, None, reasoning=None, reasoning_provenance="not_applicable")] * 3
    )
    # The old field is unchanged and still can't tell them apart.
    assert sonnet_row["reasoning"] is None and vendor_row["reasoning"] is None
    # The new field can.
    assert sonnet_row["reasoning_provenance"] == "off_unrecorded"
    assert vendor_row["reasoning_provenance"] == "not_applicable"
    assert sonnet_row["reasoning_provenance"] != vendor_row["reasoning_provenance"]


def test_result_row_and_merge_by_key():
    row = result_row([_m(True, 10), _m(False, None)])
    assert row["model"] == "haiku" and row["scenario"] == "s1" and row["tier"] == 0

    other = {**row, "model": "sonnet"}
    merged = merge_rows([row], [other])
    assert len(merged) == 2  # different model -> new row
    replaced = merge_rows([row], [{**row, "success_rate": 0.999}])
    assert len(replaced) == 1 and replaced[0]["success_rate"] == 0.999  # same key -> replaced


def test_row_records_its_own_exclusions(tmp_path):
    """Schema 3: a rejected seed must leave a durable record *in the published
    file*. Exclusions that live only in a hand-written comment block are how the
    provenance file ended up being maintained by a human."""
    from pokebench.metrics.validity import Validity

    dropped = (_m(False, None, stop="max_turns"),
               Validity(False, "output_ceiling_truncation", "39% of turns clipped", {}))
    row = result_row([_m(True, 10), _m(False, None)], [dropped])
    assert row["seeds"] == 2 and row["seeds_valid"] == 2 and row["seeds_excluded"] == 1
    assert row["exclusions"][0]["reason"] == "output_ceiling_truncation"
    assert row["exclusions"][0]["detail"]
    # The excluded seed must NOT move the medians it was rejected from.
    assert row["median_turns"] == 10


def test_row_carries_per_seed_values_not_just_medians():
    """The sonnet S1 row is a median over 4/18/2 tiles — honest, but hiding a
    bimodal distribution. M4 must be able to see the spread without opening the
    traces."""
    row = result_row([_m(False, None, tiles=4), _m(False, None, tiles=18),
                      _m(False, None, tiles=2)])
    assert row["median_tiles_explored"] == 4
    assert row["seed_values"]["tiles_explored"] == [4, 18, 2]


def test_row_carries_turns_since_new_tile_median_and_seed_values():
    """Schema 5: same additive pattern as tiles_explored -- a median plus the
    raw per-seed array, so a bimodal spread (one frozen seed, two still
    progressing) doesn't collapse into a single misleading number."""
    row = result_row([_m(False, None, tsnt=90), _m(False, None, tsnt=0),
                      _m(False, None, tsnt=5)])
    assert row["median_turns_since_new_tile"] == 5
    assert row["seed_values"]["turns_since_new_tile"] == [90, 0, 5]


def test_result_row_rejects_the_whole_group_on_cap_turns_disagreement():
    """A 100-turn seed and a 500-turn seed are not repeats of the same
    experiment under any philosophy -- the row must not blend their medians
    and quietly publish as fully valid. `_unanimous` already computes this
    signal in `aggregate()`; `result_row` must act on it, not just report it."""
    row = result_row([
        _m(False, None, turns=100, cap_turns=100),
        _m(False, None, turns=100, cap_turns=100),
        _m(False, None, turns=500, cap_turns=500),
    ])
    assert row["seeds_valid"] == 0
    assert row["seeds_excluded"] == 3
    assert all(e["reason"] == "cap_turns_disagreement" for e in row["exclusions"])
    assert row["median_turns"] is None


def test_result_row_treats_unknown_cap_turns_as_not_a_conflict():
    """`cap_turns: None` means "the trace predates the caps.max_turns field",
    not a third disagreeing value -- legacy traces must not be flagged just
    for lacking the provenance key."""
    row = result_row([
        _m(False, None, turns=100, cap_turns=100),
        _m(False, None, turns=110, cap_turns=None),
    ])
    assert row["seeds_valid"] == 2
    assert row["median_turns"] == 105


def test_row_survives_every_seed_being_excluded():
    """Publishing `seeds_valid: 0` plus the reasons beats omitting the row and
    leaving a silent hole in the table."""
    from pokebench.metrics.validity import Validity

    dropped = (_m(False, None), Validity(False, "transport_failure", "0.15s/turn", {}))
    row = result_row([], [dropped])
    assert row["seeds_valid"] == 0 and row["seeds_excluded"] == 1
    assert row["model"] == "haiku"  # key still recoverable from the excluded seed
    assert row["median_turns"] is None


def test_results_schema_bumped_to_5_for_turns_since_new_tile():
    """Schema was bumped 3->4 (2026-08-06) for `reasoning_provenance`, for the same reason
    this bump takes it 4->5: `aggregate()` now emits `median_turns_since_new_tile` and a
    `turns_since_new_tile` seed_values array (the "was it turn-limited?" diagnostic, see
    `metrics/score.py::_turns_since_new_tile`), and the schema constant is the only
    machine-readable signal a reader of `results.json` has that a new key started
    appearing."""
    assert RESULTS_SCHEMA == 5


def test_write_results_merges_on_disk(tmp_path):
    path = tmp_path / "results.json"
    write_results(path, [result_row([_m(True, 5)])], generated="t0")
    write_results(path, [result_row([_m(True, 5, turns=99)])], generated="t1")  # same key
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["schema"] == RESULTS_SCHEMA and doc["generated"] == "t1"
    assert len(doc["rows"]) == 1  # merged, not appended


def test_run_seeds_executes_and_scores(tmp_path):
    metrics = run_seeds(
        _corridor_world,
        lambda: ScriptedAgent(get_model("haiku"), [["right"]]),
        objective="reach Route 1",
        success=make_success_check({"map": "ROUTE_1"}),
        caps=Caps(max_turns=10),
        out_base=tmp_path,
        seeds=2,
        tier=0,
        scenario_id="s1",
    )
    assert len(metrics) == 2 and all(m.success for m in metrics)
    assert (tmp_path / "seed0" / "run.jsonl").exists()
    assert (tmp_path / "seed1" / "summary.json").exists()
    row = result_row(metrics)
    assert row["success_rate"] == 1.0 and row["scenario"] == "s1"


class _RawScriptedAgent:
    """Like ScriptedAgent but every turn carries a raw provider body, to check
    `run_seeds` threads `save_raw` per seed (Task A, 2026-08-11)."""

    def __init__(self, config, plan):
        self.config = config
        self.plan = plan
        self.calls = 0

    def act(self, system, messages, tools, allow_parallel=False):
        step = self.plan[min(self.calls, len(self.plan) - 1)]
        self.calls += 1
        return AgentResponse(
            text="",
            tool_calls=[ToolCall(f"t{self.calls}", "press_buttons", {"buttons": step})],
            usage=TokenUsage(10, 5),
            stop_reason="tool_use",
            raw={"call": self.calls},
        )


def test_run_seeds_writes_raw_side_files_per_seed_when_enabled(tmp_path):
    metrics = run_seeds(
        _corridor_world,
        lambda: _RawScriptedAgent(get_model("haiku"), [["right"]]),
        objective="reach Route 1",
        success=make_success_check({"map": "ROUTE_1"}),
        caps=Caps(max_turns=10),
        out_base=tmp_path,
        seeds=2,
        tier=0,
        scenario_id="s1",
        save_raw=True,
    )
    assert all(m.success for m in metrics)
    assert (tmp_path / "seed0" / "raw" / "turn_0001.json").exists()
    assert (tmp_path / "seed1" / "raw" / "turn_0001.json").exists()


def test_run_seeds_defaults_to_no_raw_side_files(tmp_path):
    run_seeds(
        _corridor_world,
        lambda: _RawScriptedAgent(get_model("haiku"), [["right"]]),
        objective="reach Route 1",
        success=make_success_check({"map": "ROUTE_1"}),
        caps=Caps(max_turns=10),
        out_base=tmp_path,
        seeds=1,
        tier=0,
        scenario_id="s1",
    )
    assert not (tmp_path / "seed0" / "raw").exists()
