"""Loop + history window + Anthropic adapter, wired together with a fake client.

The unit tests cover each piece; this one proves the harness-built messages
actually serialize into a *valid* Anthropic tool-use request on every turn — the
integration risk that unit tests miss: image-on-current-turn-only and
tool_use/tool_result pairing surviving the round trip.
"""

from types import SimpleNamespace

from pokebench.agents.anthropic import AnthropicAgent
from pokebench.agents.base import get_model
from pokebench.runner.loop import Caps, make_success_check, run_episode
from tests.test_loop import _corridor_world


class RecordingChatClient:
    """A fake anthropic client that records every request and always presses
    `right` (which wins the corridor world)."""

    def __init__(self):
        self.requests = []
        self._n = 0
        self.messages = self

    def create(self, **kwargs):
        self.requests.append(kwargs)
        n = self._n
        self._n += 1
        return SimpleNamespace(
            content=[
                SimpleNamespace(
                    type="tool_use",
                    id=f"toolu_{n}",
                    name="press_buttons",
                    input={"buttons": ["right"]},
                )
            ],
            usage=SimpleNamespace(input_tokens=120, output_tokens=15),
            stop_reason="tool_use",
        )


def _walk_blocks(content):
    for b in content:
        yield b
        if b.get("type") == "tool_result":
            yield from _walk_blocks(b["content"])


def _count_images(messages):
    return sum(
        1 for m in messages for b in _walk_blocks(m["content"]) if b.get("type") == "image"
    )


def test_every_request_is_a_valid_anthropic_tool_use_request():
    client = RecordingChatClient()
    agent = AnthropicAgent(get_model("haiku"), client=client)
    result = run_episode(
        _corridor_world(),
        agent,
        objective="reach Route 1",
        success=make_success_check({"map": "ROUTE_1"}),
        caps=Caps(max_turns=10),
    )
    assert result.success and result.turns == 3
    assert len(client.requests) == 3

    for req in client.requests:
        messages = req["messages"]
        # 1. must start with a user turn
        assert messages[0]["role"] == "user"
        # 2. exactly one screenshot per request — the current observation
        assert _count_images(messages) == 1
        # 3. system prompt + objective present, tool declared
        assert "PokéBench Agent" in req["system"] and "Route 1" in req["system"]
        assert req["tools"][0]["name"] == "press_buttons"
        # 4. every tool_result references a tool_use present in this request
        tool_use_ids = {
            b["id"]
            for m in messages
            if m["role"] == "assistant"
            for b in m["content"]
            if b.get("type") == "tool_use"
        }
        tool_result_ids = {
            b["tool_use_id"]
            for m in messages
            for b in _walk_blocks(m["content"])
            if b.get("type") == "tool_result"
        }
        assert tool_result_ids <= tool_use_ids  # no dangling tool_result


def _all_text(req):
    out = []
    for m in req["messages"]:
        for b in _walk_blocks(m["content"]):
            if b.get("type") == "text":
                out.append(b["text"])
    return "\n".join(out)


def test_tier1_state_block_and_notes_reach_the_model():
    client = RecordingChatClient()
    agent = AnthropicAgent(get_model("haiku"), client=client)
    run_episode(
        _corridor_world(),
        agent,
        objective="reach Route 1",
        success=make_success_check({"map": "ROUTE_1"}),
        caps=Caps(max_turns=10),
        tier=1,
    )
    first = client.requests[0]
    # Tier-1 system prompt carries the notes addendum; both tools are offered
    assert "update_notes" in first["system"]
    assert {t["name"] for t in first["tools"]} == {"press_buttons", "update_notes"}
    # the observation text carries the RAM state block + the notes section
    text = _all_text(first)
    assert "STATE:" in text and "facing:" in text
    assert "NOTES" in text


def test_base64_image_payload_is_decodable():
    import base64

    client = RecordingChatClient()
    agent = AnthropicAgent(get_model("haiku"), client=client)
    run_episode(
        _corridor_world(),
        agent,
        objective="reach Route 1",
        success=make_success_check({"map": "ROUTE_1"}),
        caps=Caps(max_turns=10),
    )
    first = client.requests[0]["messages"]
    image = next(b for m in first for b in _walk_blocks(m["content"]) if b.get("type") == "image")
    assert image["source"]["media_type"] == "image/png"
    raw = base64.standard_b64decode(image["source"]["data"])
    assert raw[:8] == b"\x89PNG\r\n\x1a\n"  # valid PNG signature
