"""The Anthropic adapter is a pure translation layer, tested against a fake
client so no package import, network, or API key is needed."""

from dataclasses import replace
from types import SimpleNamespace

from pokebench.agents.anthropic import (
    AnthropicAgent,
    messages_to_anthropic,
    parse_response,
)
from pokebench.agents.base import TokenUsage, get_model
from pokebench.harness.observation import (
    ImageBlock,
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from pokebench.harness.tools import PRESS_BUTTONS


def _fake_response(content, in_tok=100, out_tok=20, stop="tool_use"):
    return SimpleNamespace(
        content=content,
        usage=SimpleNamespace(input_tokens=in_tok, output_tokens=out_tok),
        stop_reason=stop,
    )


class FakeMessages:
    def __init__(self, resp):
        self.resp = resp
        self.last_kwargs = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        return self.resp


class FakeClient:
    def __init__(self, resp):
        self.messages = FakeMessages(resp)


def test_parse_response_splits_text_and_tool_calls():
    resp = _fake_response(
        [
            SimpleNamespace(type="text", text="I'll go up."),
            SimpleNamespace(
                type="tool_use", id="toolu_1", name="press_buttons", input={"buttons": ["up"]}
            ),
        ]
    )
    parsed = parse_response(resp)
    assert parsed.text == "I'll go up."
    assert len(parsed.tool_calls) == 1
    tc = parsed.tool_calls[0]
    assert tc.id == "toolu_1" and tc.name == "press_buttons" and tc.input == {"buttons": ["up"]}
    assert parsed.usage == TokenUsage(100, 20)
    assert parsed.stop_reason == "tool_use"


def test_cost_matches_haiku_prices():
    config = get_model("haiku")
    # 1M input + 1M output at $1 / $5 per MTok
    assert config.cost(TokenUsage(1_000_000, 1_000_000)) == 6.0


def test_messages_to_anthropic_encodes_image_and_tool_result():
    messages = [
        Message("user", [ImageBlock(b"\x89PNG..."), TextBlock("look")]),
        Message(
            "user",
            [ToolResultBlock("toolu_1", [ImageBlock(b"xyz"), TextBlock("after")])],
        ),
    ]
    wire = messages_to_anthropic(messages)
    img_block = wire[0]["content"][0]
    assert img_block["type"] == "image"
    assert img_block["source"]["type"] == "base64"
    assert img_block["source"]["media_type"] == "image/png"
    tr = wire[1]["content"][0]
    assert tr["type"] == "tool_result" and tr["tool_use_id"] == "toolu_1"
    assert tr["content"][0]["type"] == "image" and tr["content"][1]["type"] == "text"


def _no_reasoning(name="haiku"):
    """The pre-2026-07-21 policy: Anthropic ran with thinking off."""
    return replace(get_model(name), reasoning=False)


def test_act_calls_create_with_frozen_request_shape():
    resp = _fake_response(
        [SimpleNamespace(type="tool_use", id="t", name="press_buttons", input={"buttons": ["a"]})]
    )
    client = FakeClient(resp)
    agent = AnthropicAgent(_no_reasoning(), client=client)

    out = agent.act("SYSTEM", [Message("user", [TextBlock("hi")])], [PRESS_BUTTONS])

    kw = client.messages.last_kwargs
    assert kw["model"] == "claude-haiku-4-5"
    assert kw["temperature"] == 1.0
    assert kw["max_tokens"] == 1024
    assert kw["system"] == "SYSTEM"
    assert kw["tool_choice"] == {"type": "auto", "disable_parallel_tool_use": True}
    assert kw["tools"][0]["name"] == "press_buttons"
    assert kw["tools"][0]["input_schema"] == PRESS_BUTTONS.input_schema
    assert "thinking" not in kw
    assert out.tool_calls[0].input == {"buttons": ["a"]}


def test_allow_parallel_lifts_the_one_tool_restriction():
    resp = _fake_response(
        [SimpleNamespace(type="tool_use", id="t", name="press_buttons", input={"buttons": ["a"]})]
    )
    client = FakeClient(resp)
    agent = AnthropicAgent(_no_reasoning(), client=client)
    agent.act("S", [Message("user", [TextBlock("hi")])], [PRESS_BUTTONS], allow_parallel=True)
    # Tier-1 lets update_notes + press_buttons share a turn, so no disable flag.
    assert client.messages.last_kwargs["tool_choice"] == {"type": "auto"}


# --- Reasoning-enabled policy (current) ------------------------------------


def test_reasoning_sends_adaptive_thinking_and_drops_tool_choice():
    resp = _fake_response(
        [SimpleNamespace(type="tool_use", id="t", name="press_buttons", input={"buttons": ["a"]})]
    )
    client = FakeClient(resp)
    agent = AnthropicAgent(get_model("sonnet"), client=client)  # reasoning=True by default
    agent.act("S", [Message("user", [TextBlock("hi")])], [PRESS_BUTTONS])

    kw = client.messages.last_kwargs
    assert kw["thinking"] == {"type": "adaptive"}
    # Forced/serialised tool choice conflicts with extended thinking; the
    # runner still enforces one movement per turn.
    assert "tool_choice" not in kw
    assert kw["temperature"] == 1.0  # sampling stays pinned - still the seed-variance source
    # thinking_budget_tokens is unset (get_model's default): max_tokens must stay
    # the frozen 1024 answer allowance, unboosted. This is the assertion that
    # protects every published row -- see the two tests below.
    assert kw["max_tokens"] == 1024


# --- Explicit thinking budget (Stage 1b, 2026-08-14) -----------------------
#
# `max_output_tokens=1024` was a runaway-consumption guard (SECURITY.md LLM10),
# not a limit on deliberation -- but Anthropic bills extended thinking against
# `max_tokens`, so with adaptive thinking + reasoning=True the guard silently
# became a thinking cap for this one vendor (CLAUDE.md "Two live caveats" #1:
# sonnet hit the ceiling on 39% of turns, no tool call on 37%). Stage 0's live
# probe (`tools/probe_thinking_budget.py`, one call, 2026-08-14) confirmed the
# shape works and the relationship this uses: budget_tokens=1024 (the API's
# documented minimum) + max_tokens=budget+1024 succeeded, and the response
# separates thinking tokens from answer tokens via
# `usage.output_tokens_details.thinking_tokens`.


def test_thinking_budget_tokens_sends_enabled_and_raises_max_tokens():
    resp = _fake_response(
        [SimpleNamespace(type="tool_use", id="t", name="press_buttons", input={"buttons": ["a"]})]
    )
    client = FakeClient(resp)
    config = replace(get_model("sonnet"), thinking_budget_tokens=1024)
    agent = AnthropicAgent(config, client=client)
    agent.act("S", [Message("user", [TextBlock("hi")])], [PRESS_BUTTONS])

    kw = client.messages.last_kwargs
    assert kw["thinking"] == {"type": "enabled", "budget_tokens": 1024}
    # budget (1024) + the unchanged 1024 answer allowance -- the exact formula
    # Stage 0's live probe verified works.
    assert kw["max_tokens"] == 2048
    # Forced/serialised tool choice still conflicts with extended thinking.
    assert "tool_choice" not in kw
    assert kw["temperature"] == 1.0


def test_no_thinking_budget_tokens_leaves_current_behaviour_byte_for_byte():
    """The assertion that protects every published row: with the field
    unset (the default for every existing MODELS entry), the request shape
    must be identical to before this field existed -- adaptive thinking
    against the unmodified 1024, nothing raised."""
    resp = _fake_response(
        [SimpleNamespace(type="tool_use", id="t", name="press_buttons", input={"buttons": ["a"]})]
    )
    client = FakeClient(resp)
    config = get_model("sonnet")
    assert config.thinking_budget_tokens is None  # the registry default, untouched
    agent = AnthropicAgent(config, client=client)
    agent.act("S", [Message("user", [TextBlock("hi")])], [PRESS_BUTTONS])

    kw = client.messages.last_kwargs
    assert kw["thinking"] == {"type": "adaptive"}
    assert kw["max_tokens"] == 1024
    assert "tool_choice" not in kw


def test_thinking_blocks_are_captured_and_replayed_first():
    # Anthropic wants thinking echoed back unchanged on the same model, and it
    # must lead the assistant turn. Dropping it does not 400, but the model
    # would lose its own prior reasoning every turn.
    thinking = SimpleNamespace(
        type="thinking", model_dump=lambda exclude_none: {"type": "thinking", "thinking": "hmm"}
    )
    tool = SimpleNamespace(type="tool_use", id="t1", name="press_buttons", input={"buttons": ["a"]})
    parsed = parse_response(_fake_response([thinking, tool]))

    tc = parsed.tool_calls[0]
    assert tc.provider_state["thinking_blocks"] == [{"type": "thinking", "thinking": "hmm"}]

    wire = messages_to_anthropic(
        [Message("assistant", [TextBlock("going up"), ToolUseBlock(*(tc.id, tc.name, tc.input),
                                                                  tc.provider_state)])]
    )
    kinds = [b["type"] for b in wire[0]["content"]]
    assert kinds[0] == "thinking"  # must lead the turn
    assert kinds == ["thinking", "text", "tool_use"]


def test_no_thinking_blocks_leaves_provider_state_unset():
    tool = SimpleNamespace(
        type="tool_use", id="t", name="press_buttons", input={"buttons": ["a"]}
    )
    parsed = parse_response(_fake_response([tool]))
    assert parsed.tool_calls[0].provider_state is None


# --- raw response capture (Task A, 2026-08-11) ------------------------------


def test_parse_response_captures_raw_body_via_model_dump():
    # The real anthropic SDK Message is a pydantic model; model_dump(mode="json")
    # is how it is turned into something json.dump can write to a side file.
    tool = SimpleNamespace(type="tool_use", id="t", name="press_buttons", input={"buttons": ["a"]})
    resp = _fake_response([tool])
    resp.model_dump = lambda mode="python": {"id": "msg_1", "stop_reason": "tool_use"}
    parsed = parse_response(resp)
    assert parsed.raw == {"id": "msg_1", "stop_reason": "tool_use"}


def test_parse_response_raw_is_none_without_model_dump():
    # Every other fixture in this file uses a plain SimpleNamespace with no
    # model_dump; raw must stay None rather than error, so none of those
    # tests needed to change to keep passing.
    tool = SimpleNamespace(type="tool_use", id="t", name="press_buttons", input={"buttons": ["a"]})
    parsed = parse_response(_fake_response([tool]))
    assert parsed.raw is None
