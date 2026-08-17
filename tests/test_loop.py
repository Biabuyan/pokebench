"""End-to-end agent-loop tests driven by a ScriptedAgent on the fake world —
the whole observe -> decide -> execute -> trace -> cap machinery, no ROM, no
API key, no network."""

import json
from dataclasses import replace

from pokebench.agents.base import AgentResponse, TokenUsage, ToolCall, get_model
from pokebench.harness.tools import POST_ACTION_SETTLE_FRAMES
from pokebench.harness.trace import RunTracer
from pokebench.runner.loop import WARP_SETTLE_FRAMES, Caps, make_success_check, run_episode
from tests.fake_world import FakeWalker, Room

DEFAULT_USAGE = TokenUsage(50, 10)


class ScriptedAgent:
    """Deterministic stand-in for a model. `plan[i]` is the buttons for turn i
    (a list -> a press_buttons call; None -> no tool call). The last entry
    repeats once the plan is exhausted, so `[["a"]]` means "press a forever"."""

    def __init__(self, config, plan, usage=DEFAULT_USAGE):
        self.config = config
        self.plan = plan
        self.usage = usage
        self.calls = 0

    def act(self, system, messages, tools, allow_parallel=False):
        step = self.plan[min(self.calls, len(self.plan) - 1)]
        self.calls += 1
        if step is None:
            return AgentResponse(
                text="thinking", tool_calls=[], usage=self.usage, stop_reason="end_turn"
            )
        # A list of buttons -> press_buttons. A dict may carry "notes" and/or
        # "buttons" -> update_notes and/or press_buttons (both in one turn).
        calls = []
        if isinstance(step, dict):
            if "notes" in step:
                calls.append(
                    ToolCall(f"toolu_{self.calls}n", "update_notes", {"content": step["notes"]})
                )
            if "buttons" in step:
                calls.append(
                    ToolCall(f"toolu_{self.calls}b", "press_buttons", {"buttons": step["buttons"]})
                )
        else:
            calls.append(ToolCall(f"toolu_{self.calls}", "press_buttons", {"buttons": step}))
        return AgentResponse(text="", tool_calls=calls, usage=self.usage, stop_reason="tool_use")


def _corridor_world():
    """A 1-row corridor; stepping onto (3,0) warps to ROUTE_1. So three `right`
    presses win, and any non-direction press makes no progress."""
    town = Room(grid=["....."], entry_warps={(3, 0): ("ROUTE_1", (0, 0))})
    route1 = Room(grid=["....."])
    return FakeWalker({"PALLET_TOWN": town, "ROUTE_1": route1}, "PALLET_TOWN", (0, 0))


ROUTE1 = make_success_check({"map": "ROUTE_1"})
GO = "reach Route 1"


def _run(agent, env=None, caps=None, **kw):
    return run_episode(
        env or _corridor_world(),
        agent,
        objective=GO,
        success=ROUTE1,
        caps=caps or Caps(max_turns=50),
        **kw,
    )


def test_loop_reaches_success():
    r = _run(ScriptedAgent(get_model("haiku"), [["right"]]))
    assert r.success and r.stop_reason == "success"
    assert r.final_state.map_name == "ROUTE_1"
    assert r.turns == 3  # three eastward steps to the warp


def test_max_turns_cap_stops_a_stuck_agent():
    # pressing `a` never moves in the corridor -> never wins
    r = _run(ScriptedAgent(get_model("haiku"), [["a"]]), caps=Caps(max_turns=5))
    assert not r.success and r.stop_reason == "max_turns" and r.turns == 5


def test_token_cap_stops_before_the_next_call():
    agent = ScriptedAgent(get_model("haiku"), [["a"]], usage=TokenUsage(100_000, 0))
    r = _run(agent, caps=Caps(max_turns=99, max_tokens=250_000))
    assert r.stop_reason == "max_tokens" and r.turns == 3  # 3*100k=300k > 250k


def test_dollar_cap_is_the_kill_switch():
    # haiku output is $5/MTok -> 100k output = $0.50/turn
    agent = ScriptedAgent(get_model("haiku"), [["a"]], usage=TokenUsage(0, 100_000))
    r = _run(agent, caps=Caps(max_turns=99, max_usd=1.2))
    assert r.stop_reason == "max_usd" and r.turns == 3  # $0.50*3=$1.50 > $1.20


def test_wall_clock_cap():
    # a fake monotonic clock that jumps 10s per read
    ticks = iter(range(0, 10_000, 10))
    r = _run(
        ScriptedAgent(get_model("haiku"), [["a"]]),
        caps=Caps(max_turns=99, max_wall_seconds=25),
        clock=lambda: next(ticks),
    )
    assert r.stop_reason == "max_wall_clock"


def test_no_tool_call_is_nudged_not_fatal():
    # turn 1 returns no tool call; the loop nudges and keeps going
    plan = [None, ["right"], ["right"], ["right"]]
    r = _run(ScriptedAgent(get_model("haiku"), plan))
    assert r.success and r.turns == 4  # one wasted turn + three real steps


def test_trace_records_action_usage_and_screenshots(tmp_path):
    tracer = RunTracer(tmp_path)
    agent = ScriptedAgent(get_model("haiku"), [["x", "right"]])  # invalid + valid
    r = _run(agent, caps=Caps(max_turns=3), tracer=tracer)
    tracer.finish(r.summary_dict())
    tracer.close()

    assert (tmp_path / "meta.json").exists()
    assert (tmp_path / "summary.json").exists()
    assert (tmp_path / "turn_0001.png").exists()  # PIL save path exercised

    lines = (tmp_path / "run.jsonl").read_text(encoding="utf-8").strip().splitlines()
    first = json.loads(lines[0])
    assert first["action"]["executed"] == ["right"]
    assert first["action"]["invalid"] == ["x"]  # invalid input recorded, not dropped
    assert first["usage"] == {"input_tokens": 50, "output_tokens": 10}
    assert first["cost_usd"] > 0
    assert first["tool_call"]["name"] == "press_buttons"

    meta = json.loads((tmp_path / "meta.json").read_text(encoding="utf-8"))
    assert meta["model"] == "haiku" and meta["tier"] == 0
    # `thinking_budget_tokens` unset (the registry default): recorded as None,
    # not just absent -- a reader must not have to guess whether it predates
    # the field. Mirrors "reasoning" (also written unconditionally, above).
    assert meta["thinking_budget_tokens"] is None


def test_meta_records_thinking_budget_tokens_when_set(tmp_path):
    # Stage 2, 2026-08-14: `--thinking-budget` threads a value onto
    # ModelConfig via `dataclasses.replace` before `make_agent` -- meta.json
    # must reflect what the config actually carried, the same way it already
    # does for `reasoning`, so a calibration run's provenance is verifiable
    # after the fact without re-deriving it from the CLI invocation.
    tracer = RunTracer(tmp_path)
    config = replace(get_model("sonnet"), thinking_budget_tokens=4096)
    agent = ScriptedAgent(config, [["right"]])
    r = _run(agent, caps=Caps(max_turns=3), tracer=tracer)
    tracer.finish(r.summary_dict())
    tracer.close()

    meta = json.loads((tmp_path / "meta.json").read_text(encoding="utf-8"))
    assert meta["thinking_budget_tokens"] == 4096


class _SettleCountingEnv:
    """Wraps the corridor world and totals the frames it is asked to settle."""

    def __init__(self):
        self.inner = _corridor_world()
        self.settle_frames = 0

    def press(self, b):
        self.inner.press(b)

    def state(self):
        return self.inner.state()

    def screenshot(self):
        return self.inner.screenshot()

    def settle(self, f):
        self.settle_frames += f
        self.inner.settle(f)


def test_map_transition_settles_out_the_warp_fade():
    # Three `right` steps win; the third crosses into ROUTE_1, so that turn must
    # settle the extra warp frames on top of the per-action settle.
    env = _SettleCountingEnv()
    r = _run(ScriptedAgent(get_model("haiku"), [["right"]]), env=env)
    assert r.success and r.turns == 3
    assert env.settle_frames == 3 * POST_ACTION_SETTLE_FRAMES + WARP_SETTLE_FRAMES


def test_tier1_notes_turn_records_and_does_not_move(tmp_path):
    tracer = RunTracer(tmp_path)
    plan = [{"notes": "go east to reach the exit"}, ["right"], ["right"], ["right"]]
    agent = ScriptedAgent(get_model("haiku"), plan)
    r = _run(agent, caps=Caps(max_turns=10), tracer=tracer, tier=1)
    tracer.finish(r.summary_dict())
    tracer.close()

    assert r.success and r.turns == 4  # the notes turn is a whole turn, then 3 steps

    recs = [json.loads(line) for line in (tmp_path / "run.jsonl").read_text().splitlines()]
    first = recs[0]
    assert first["tool_call"]["name"] == "update_notes"
    assert first["action"] is None  # a notes write is not a movement
    assert first["notes"] == "go east to reach the exit"
    assert first["state"]["map_name"] == "PALLET_TOWN"  # player did not move

    meta = json.loads((tmp_path / "meta.json").read_text())
    assert meta["tier"] == 1 and "update_notes" in meta["tools"]
    assert meta["system_prompt_version"] >= 2


def test_tier1_notes_are_capped_in_the_loop(tmp_path):
    tracer = RunTracer(tmp_path)
    plan = [{"notes": "x" * 5000}, ["right"], ["right"], ["right"]]
    agent = ScriptedAgent(get_model("haiku"), plan)
    r = _run(agent, caps=Caps(max_turns=10), tracer=tracer, tier=1)
    tracer.finish(r.summary_dict())
    tracer.close()
    recs = [json.loads(line) for line in (tmp_path / "run.jsonl").read_text().splitlines()]
    assert len(recs[0]["notes"]) == 1000  # MAX_NOTES_CHARS, enforced in code
    assert recs[0]["notes_update"]["truncated"] is True


def test_tier1_notes_and_move_share_one_turn(tmp_path):
    # {"notes","buttons"} -> update_notes AND press_buttons in the SAME turn, so
    # note-taking no longer costs a turn (matches the plan's perceive->note->act).
    tracer = RunTracer(tmp_path)
    plan = [{"notes": "east wins", "buttons": ["right"]}, ["right"], ["right"]]
    agent = ScriptedAgent(get_model("haiku"), plan)
    r = _run(agent, caps=Caps(max_turns=10), tracer=tracer, tier=1)
    tracer.finish(r.summary_dict())
    tracer.close()

    assert r.success and r.turns == 3  # 3 steps to the warp; the note rode along

    recs = [json.loads(line) for line in (tmp_path / "run.jsonl").read_text().splitlines()]
    names = {c["name"] for c in recs[0]["tool_calls"]}
    assert names == {"update_notes", "press_buttons"}  # both ran on turn 1
    assert recs[0]["notes"] == "east wins"
    assert recs[0]["action"]["executed"] == ["right"]  # and the player moved
    assert recs[0]["state"]["x"] == 1  # moved east on the same turn it took notes
    assert "observation_hash" in recs[0]


class _RawAgent:
    """Returns one AgentResponse carrying a raw body, like a real adapter
    would; used to check the loop forwards `resp.raw` into the tracer."""

    def __init__(self, config, raw):
        self.config = config
        self.raw = raw
        self.calls = 0

    def act(self, system, messages, tools, allow_parallel=False):
        self.calls += 1
        return AgentResponse(
            text="",
            tool_calls=[ToolCall("t1", "press_buttons", {"buttons": ["right"]})],
            usage=DEFAULT_USAGE,
            stop_reason="tool_use",
            raw=self.raw,
        )


def test_loop_forwards_the_agent_response_raw_body_to_the_tracer(tmp_path):
    # Task A (2026-08-11): the raw body is only useful for post-hoc parser
    # debugging if the loop actually plumbs it from AgentResponse through to
    # RunTracer.record_turn -- adding the field to AgentResponse alone does
    # nothing without this wire.
    tracer = RunTracer(tmp_path, screenshots=False, save_raw=True)
    agent = _RawAgent(get_model("haiku"), raw={"output": [{"type": "message"}]})
    _run(agent, caps=Caps(max_turns=1), tracer=tracer)
    tracer.close()
    raw_file = tmp_path / "raw" / "turn_0001.json"
    assert raw_file.exists()
    assert json.loads(raw_file.read_text(encoding="utf-8")) == {
        "output": [{"type": "message"}]
    }


def test_loop_does_not_write_a_raw_file_when_the_tracer_has_save_raw_off(tmp_path):
    # Default RunTracer (save_raw=False) must not write anything to raw/, even
    # when the agent supplies a raw body -- opt-in means opt-in.
    tracer = RunTracer(tmp_path, screenshots=False)
    agent = _RawAgent(get_model("haiku"), raw={"output": []})
    _run(agent, caps=Caps(max_turns=1), tracer=tracer)
    tracer.close()
    assert not (tmp_path / "raw").exists()


def test_already_at_goal_returns_immediately():
    env = _corridor_world()
    env.map, env.pos = "ROUTE_1", (0, 0)
    r = _run(ScriptedAgent(get_model("haiku"), [["a"]]), env=env)
    assert r.success and r.turns == 0 and r.stop_reason == "success"
