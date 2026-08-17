"""The agent loop — observe → decide → execute → trace, under hard caps.

This is the engine of a PokéBench run: build the fixed observation, let the
model decide via `press_buttons`, execute against the emulator, trace the turn,
and repeat until the scenario's success predicate holds or a cap trips. The
caps (turns / tokens / dollars / wall-clock) live *here in code*, never in the
prompt — an unbounded-consumption guard the model cannot argue its way past
(SECURITY.md, LLM10).

The loop is provider-agnostic (it drives an `Agent`) and ROM-agnostic (it
drives an `Environment`), so a ScriptedAgent on the fake world exercises the
whole thing without an API key or a ROM.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from pokebench.agents.base import Agent
from pokebench.harness.observation import (
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    build_observation,
    describe_action,
    observation_fingerprint,
)
from pokebench.harness.prompts import SYSTEM_PROMPT_VERSION, build_system
from pokebench.harness.state import GameState
from pokebench.harness.tools import (
    MAX_NOTES_CHARS,
    PRESS_BUTTONS,
    UPDATE_NOTES,
    Environment,
    Notebook,
    execute_press_buttons,
    tools_for_tier,
)
from pokebench.runner.history import build_window

# How many recent exchanges the model sees (frozen policy; see history.py).
HISTORY_WINDOW_TURNS = 10

# A warp/stairs/door fade takes far longer than the per-action settle, so on a
# turn that changes the map we settle out the fade before screenshotting — else
# the observation delivered to the model is a black transition frame. Mirrors
# the M0 spike's settle(90) on a detected map change (harness/navigate.py).
WARP_SETTLE_FRAMES = 90

SuccessCheck = Callable[[GameState], bool]


@dataclass(frozen=True)
class Caps:
    """Hard per-run limits, enforced in loop code (never the prompt)."""

    max_turns: int
    max_tokens: int | None = None
    max_usd: float | None = None
    max_wall_seconds: float | None = None


@dataclass
class RunResult:
    success: bool
    stop_reason: str  # success | max_turns | max_tokens | max_usd | max_wall_clock
    turns: int
    input_tokens: int
    output_tokens: int
    usd: float
    wall_seconds: float
    final_state: GameState

    def summary_dict(self) -> dict:
        return {
            "success": self.success,
            "stop_reason": self.stop_reason,
            "turns": self.turns,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.input_tokens + self.output_tokens,
            "usd": round(self.usd, 6),
            "wall_seconds": round(self.wall_seconds, 3),
            "final_state": self.final_state.to_dict(),
        }


def make_success_check(spec: dict) -> SuccessCheck:
    """Build the scenario success predicate from its YAML `success` block.

    Supports a target `map` plus optional coordinate bounds (x_gte/x_lte/
    y_gte/y_lte), mirroring the route-leg `until` vocabulary in navigate.py.
    `money_lte` (S4, 2026-08-15) adds a spend-based clause: `GameState.money`
    already exists (`harness/state.py`), so this is cheap money-clause support,
    not new RAM mapping -- see s4_viridian_mart.yaml for why money, not items
    (no item RAM address is mapped anywhere in memory_map.py/state.py).
    """

    def check(s: GameState) -> bool:
        if "map" in spec and s.map_name != spec["map"]:
            return False
        if "x_gte" in spec and not s.x >= spec["x_gte"]:
            return False
        if "x_lte" in spec and not s.x <= spec["x_lte"]:
            return False
        if "y_gte" in spec and not s.y >= spec["y_gte"]:
            return False
        if "y_lte" in spec and not s.y <= spec["y_lte"]:
            return False
        if "money_lte" in spec and not s.money <= spec["money_lte"]:
            return False
        return True

    return check


def _cap_tripped(caps: Caps, turns: int, tokens: int, usd: float, elapsed: float) -> str | None:
    if turns >= caps.max_turns:
        return "max_turns"
    if caps.max_wall_seconds is not None and elapsed >= caps.max_wall_seconds:
        return "max_wall_clock"
    if caps.max_tokens is not None and tokens >= caps.max_tokens:
        return "max_tokens"
    if caps.max_usd is not None and usd >= caps.max_usd:
        return "max_usd"
    return None


def run_episode(
    env: Environment,
    agent: Agent,
    *,
    objective: str,
    success: SuccessCheck,
    caps: Caps,
    tracer=None,
    tier: int = 0,
    scenario_id: str | None = None,
    clock: Callable[[], float] = time.monotonic,
    log: Callable[[str], None] | None = None,
) -> RunResult:
    """Run one episode to success or cap. See module docstring for the contract."""
    system = build_system(objective, tier)
    tools = tools_for_tier(tier)
    notebook = Notebook()  # Tier-1 persistent notes (empty at Tier 0)

    if tracer is not None:
        tracer.write_meta(
            {
                "scenario": scenario_id,
                "model": agent.config.name,
                "model_id": agent.config.model_id,
                "provider": agent.config.provider,
                "temperature": agent.config.temperature,
                "reasoning": getattr(agent.config, "reasoning", None),
                # Provenance only, mirroring "reasoning" above (Stage 2,
                # 2026-08-14): `--thinking-budget` (cli.py) is the only way to
                # set this to anything but the registry default of `None`, and
                # without it here a calibration run's meta.json would be
                # silent about the one thing that distinguishes it from every
                # other Anthropic row — see agents/anthropic.py for how this
                # value changes the actual request (thinking type + max_tokens).
                "thinking_budget_tokens": getattr(agent.config, "thinking_budget_tokens", None),
                # Provenance only — this does not change how the episode runs.
                # Recorded from 2026-08-04 so `metrics/score.py` can measure the
                # ceiling-hit rate against the real ceiling instead of falling
                # back to a hardcoded 1024. The M3 traces predate this and are
                # scored against the fallback, which is flagged as "assumed".
                "max_output_tokens": getattr(agent.config, "max_output_tokens", None),
                "tier": tier,
                "tools": [t.name for t in tools],
                "system_prompt_version": SYSTEM_PROMPT_VERSION,
                "caps": {
                    "max_turns": caps.max_turns,
                    "max_tokens": caps.max_tokens,
                    "max_usd": caps.max_usd,
                    "max_wall_seconds": caps.max_wall_seconds,
                },
                "history_window_turns": HISTORY_WINDOW_TURNS,
            }
        )

    history: list[Message] = []
    in_tokens = out_tokens = 0
    usd = 0.0
    turns = 0
    t0 = clock()

    state = env.state()
    # An anchor should never already satisfy the goal, but guard anyway.
    if success(state):
        return _result(True, "success", turns, in_tokens, out_tokens, usd, clock() - t0, state)

    # First observation is a plain user turn (there is no prior action).
    history.append(
        Message(
            "user",
            build_observation(
                state, env.screenshot(), None, tier, notebook.text,
                intro="This is the first turn of the scenario.",
            ),
        )
    )

    while True:
        elapsed = clock() - t0
        stop = _cap_tripped(caps, turns, in_tokens + out_tokens, usd, elapsed)
        if stop is not None:
            return _result(False, stop, turns, in_tokens, out_tokens, usd, elapsed, state)

        resp = agent.act(
            system,
            build_window(history, HISTORY_WINDOW_TURNS),
            tools,
            allow_parallel=(tier >= 1),  # Tier-1: notes + a move can share a turn
        )
        turns += 1
        in_tokens += resp.usage.input_tokens
        out_tokens += resp.usage.output_tokens
        turn_cost = agent.config.cost(resp.usage)
        usd += turn_cost

        # Reconstruct the assistant turn for the transcript.
        assistant_blocks: list = []
        if resp.text:
            assistant_blocks.append(TextBlock(resp.text))
        for tc in resp.tool_calls:
            assistant_blocks.append(ToolUseBlock(tc.id, tc.name, tc.input, tc.provider_state))
        if not assistant_blocks:
            assistant_blocks.append(TextBlock("(no output)"))
        history.append(Message("assistant", assistant_blocks))

        record = {
            "model": {"text": resp.text, "stop_reason": resp.stop_reason},
            "usage": resp.usage.to_dict(),
            "cost_usd": round(turn_cost, 6),
            "cumulative": {
                "input_tokens": in_tokens,
                "output_tokens": out_tokens,
                "usd": round(usd, 6),
            },
        }

        # perceive -> (update notes) -> decide -> act, all in one turn (plan #3).
        # Apply every update_notes write, then execute at most ONE press_buttons
        # (one movement per turn keeps the observation cadence fixed); extra
        # movement calls are ignored. Every tool_use still gets a tool_result.
        note_calls = [tc for tc in resp.tool_calls if tc.name == UPDATE_NOTES.name]
        press_calls = [tc for tc in resp.tool_calls if tc.name == PRESS_BUTTONS.name]
        result = note_update = None
        carrier = None

        for nc in note_calls:  # last write wins if several are somehow sent
            note_update = notebook.update(nc.input.get("content"))

        press_call = press_calls[0] if press_calls else None
        if press_call is not None:
            result = execute_press_buttons(env, press_call.input.get("buttons"))
            new_state = env.state()
            # A warp fires this turn -> settle the fade so the screenshot is a
            # clean frame, not black. Re-read after (mirrors the M0 spike).
            if new_state.map_id != state.map_id:
                env.settle(WARP_SETTLE_FRAMES)
                new_state = env.state()
        else:
            new_state = env.state()  # notes-only / no-op: the player did not move
        new_shot = env.screenshot()

        if not resp.tool_calls:
            # No tool call — nudge and re-show the screen (state unchanged).
            obs = build_observation(
                state, new_shot, None, tier, notebook.text,
                intro="You did not call a tool. You must call press_buttons to act.",
            )
            history.append(Message("user", obs))
            log_line = f"turn {turns}: no tool call (nudged) | ${usd:.4f}"
        else:
            obs = build_observation(
                new_state, new_shot, None, tier, notebook.text,
                intro=_event_line(note_update, result),
            )
            # The observation rides on the movement call if there is one, else on
            # the first notes call (Anthropic needs one tool_result per tool_use).
            carrier = press_call or (note_calls[0] if note_calls else resp.tool_calls[0])
            tool_results: list = []
            for tc in resp.tool_calls:
                if tc is carrier:
                    tool_results.append(ToolResultBlock(tc.id, obs))
                elif tc in note_calls:
                    tool_results.append(ToolResultBlock(tc.id, [TextBlock("Notes updated.")]))
                elif tc in press_calls:  # an extra movement beyond the first
                    tool_results.append(
                        ToolResultBlock(tc.id, [TextBlock("Ignored: one movement per turn.")])
                    )
                else:  # a tool outside our schema
                    tool_results.append(
                        ToolResultBlock(tc.id, [TextBlock(f"Unknown tool {tc.name!r} ignored.")])
                    )
            history.append(Message("user", tool_results))
            log_line = _turn_line(turns, result, new_state, usd, note_update)

        record["tool_calls"] = [{"name": tc.name, "input": tc.input} for tc in resp.tool_calls]
        record["tool_call"] = (
            {"name": carrier.name, "input": carrier.input} if carrier is not None else None
        )
        record["action"] = result.to_dict() if result is not None else None
        record["notes_update"] = note_update
        record["observation_hash"] = observation_fingerprint(new_shot, new_state, tier)
        if tier >= 1:
            record["notes"] = notebook.text
        record["state"] = new_state.to_dict()
        succeeded = result is not None and success(new_state)
        record["success"] = succeeded
        if tracer is not None:
            tracer.record_turn(record, screenshot=new_shot, raw=resp.raw)
        if log:
            log(log_line)

        state = new_state
        if succeeded:
            return _result(True, "success", turns, in_tokens, out_tokens, usd, clock() - t0, state)


def _event_line(note_update, result) -> str:
    """The 'what just happened' line shown to the model (notes and/or a move)."""
    parts = []
    if note_update is not None:
        s = f"You updated your notes ({note_update['chars']} chars saved)"
        if note_update["truncated"]:
            s += f", truncated to {MAX_NOTES_CHARS} chars"
        parts.append(s + ".")
    if result is not None:
        parts.append(describe_action(result))
    if not parts:
        parts.append("Unknown tool ignored; no action taken.")
    return " ".join(parts)


def _turn_line(turns, result, state, usd, note_update=None) -> str:
    bits = []
    if note_update is not None:
        bits.append(f"notes:{note_update['chars']}")
    if result is not None:
        bits.append(", ".join(result.executed) if result.executed else "no-op")
    label = " + ".join(bits) if bits else "no-op"
    return f"turn {turns}: [{label}] -> {state.map_name} {state.pos} | ${usd:.4f}"


def _result(success, stop, turns, in_tok, out_tok, usd, wall, state) -> RunResult:
    return RunResult(
        success=success,
        stop_reason=stop,
        turns=turns,
        input_tokens=in_tok,
        output_tokens=out_tok,
        usd=usd,
        wall_seconds=wall,
        final_state=state,
    )
