"""M3 adapters (OpenAI / Google / Ollama) are pure translation layers, tested
against a fake JSON poster so no package import, network, or API key is needed.

The wire shapes asserted here were verified against live responses on
2026-07-20 (see HANDOFF.md M3); these tests pin them so a refactor cannot
silently change what the models are shown. The OpenAI `"reasoning"` output
item shape was re-verified live on 2026-08-06 (Task C) -- see the two
`test_openai_reasoning_*` tests below for what changed and what did not.
"""

import json

import pytest

from pokebench.agents._http import HttpxPoster, ProviderHTTPError
from pokebench.agents.base import ModelConfig, TokenUsage, get_model, make_agent
from pokebench.agents.google import GoogleAgent, messages_to_google
from pokebench.agents.google import parse_response as google_parse
from pokebench.agents.ollama import OllamaAgent, messages_to_chat
from pokebench.agents.ollama import parse_response as ollama_parse
from pokebench.agents.openai import OpenAIAgent, messages_to_openai
from pokebench.agents.openai import parse_response as openai_parse
from pokebench.harness.observation import (
    ImageBlock,
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from pokebench.harness.tools import PRESS_BUTTONS


class FakePoster:
    """Records the request and replays a canned response."""

    def __init__(self, response):
        self.response = response
        self.url = None
        self.payload = None
        self.headers = None

    def __call__(self, url, payload, headers=None):
        self.url, self.payload, self.headers = url, payload, headers or {}
        return self.response


# --- OpenAI (Responses API) ------------------------------------------------


def test_openai_parses_function_call_and_usage():
    body = {
        "output": [
            {"type": "message", "content": [{"type": "output_text", "text": "Going up."}]},
            {
                "type": "function_call",
                "call_id": "call_1",
                "name": "press_buttons",
                "arguments": '{"buttons": ["up"]}',
            },
        ],
        "usage": {"input_tokens": 170, "output_tokens": 118},
        "status": "completed",
    }
    parsed = openai_parse(body)
    assert parsed.text == "Going up."
    assert parsed.tool_calls[0].name == "press_buttons"
    assert parsed.tool_calls[0].input == {"buttons": ["up"]}
    assert parsed.usage == TokenUsage(170, 118)
    assert parsed.stop_reason == "completed"


def test_openai_reasoning_summary_is_captured_in_model_text():
    # The Responses API surfaces a reasoning summary as its own top-level
    # `type: "reasoning"` item, sitting ALONGSIDE the `message` item -- not
    # nested inside it. The old parser only ever walked `message` items, so
    # gpt's model.text was empty on every turn of every trace ever recorded;
    # it was assumed for weeks to be the model emitting no chain-of-thought.
    # It was our parser dropping it. This pins the fix.
    #
    # VERIFIED LIVE 2026-08-06 (Task C, one call to gpt-5.6-terra via
    # /v1/responses, the exact registry model + this harness's request shape):
    # the OUTER envelope here is real -- `type: "reasoning"` is a top-level
    # item and `summary` is a list. What is NOT verified is the inner
    # `{"type": "summary_text", "text": ...}` shape of a POPULATED entry: the
    # harness sends no `reasoning` request param (by design -- see
    # openai.py's module docstring on not touching the reasoning knob), and
    # under that request OpenAI returned `"summary": []` -- always empty, no
    # matter what the model reasoned about. See the test directly below,
    # built from that live response (trimmed of its encrypted_content blob),
    # which pins what actually happens on every real turn today: this
    # code path executes but contributes nothing, because `summary` is never
    # populated without asking for it. Whether to start asking (a request
    # shape change) is a call for the explorer/reviewer, not this fixture.
    body = {
        "output": [
            {
                "type": "reasoning",
                "id": "rs_1",
                "summary": [{"type": "summary_text", "text": "Checking the map for stairs."}],
            },
            {"type": "message", "content": [{"type": "output_text", "text": "Going up."}]},
        ],
        "usage": {"input_tokens": 170, "output_tokens": 118},
        "status": "completed",
    }
    parsed = openai_parse(body)
    assert parsed.text  # no longer empty
    assert "Checking the map for stairs." in parsed.text
    assert "Going up." in parsed.text  # the message text is still captured too


def test_openai_reasoning_item_with_empty_summary_contributes_nothing():
    # The actual shape of a live turn under THIS harness's request payload
    # (captured 2026-08-06, gpt-5.6-terra, /v1/responses, no `reasoning` param
    # sent -- see the test above): a "reasoning" item with `summary: []`
    # (encrypted_content present but opaque) directly followed by a
    # "function_call" item, and NO "message" item at all -- the model followed
    # the system prompt's "act by calling a tool; never reply with text
    # alone" instruction and skipped a separate text item entirely. Net
    # effect: `parsed.text` is empty on a real, successful, tool-calling turn.
    # This is not a parser bug -- there is nothing here to parse -- but it
    # does mean the reasoning-item fix above does not, in practice, populate
    # `model.text` for gpt under the harness's current request shape.
    body = {
        "id": "resp_live_2026-08-06",
        "status": "completed",
        "output": [
            {
                "id": "rs_live",
                "type": "reasoning",
                "content": [],
                "encrypted_content": "<opaque, not parsed>",
                "summary": [],
            },
            {
                "id": "fc_live",
                "type": "function_call",
                "status": "completed",
                "arguments": '{"buttons":["down"]}',
                "call_id": "call_live",
                "name": "press_buttons",
            },
        ],
        "reasoning": {"context": "all_turns", "effort": "medium", "summary": None},
        "usage": {
            "input_tokens": 696,
            "output_tokens": 63,
            "output_tokens_details": {"reasoning_tokens": 42},
        },
    }
    parsed = openai_parse(body)
    assert parsed.text == ""  # nothing to surface -- summary is empty, no message item
    assert parsed.tool_calls[0].input == {"buttons": ["down"]}
    assert parsed.usage == TokenUsage(696, 63)


def test_openai_parse_captures_the_raw_body_verbatim():
    # Task A (2026-08-11): the raw body is what makes a parser bug (like the
    # "reasoning" item drop above) diagnosable after the fact instead of
    # invisible forever.
    body = {
        "output": [{"type": "message", "content": [{"type": "output_text", "text": "hi"}]}],
        "usage": {"input_tokens": 1, "output_tokens": 1},
        "status": "completed",
    }
    assert openai_parse(body).raw is body


def test_openai_malformed_arguments_degrade_to_empty_dict():
    # A model emitting invalid JSON must waste a turn, not crash the run --
    # the invalid-action-rate metric is what should capture it.
    body = {"output": [{"type": "function_call", "call_id": "c",
                        "name": "press_buttons", "arguments": "{not json"}]}
    assert openai_parse(body).tool_calls[0].input == {}


def test_openai_flattens_tool_result_and_carries_image_separately():
    messages = [
        Message("assistant", [ToolUseBlock("call_1", "press_buttons", {"buttons": ["a"]})]),
        Message("user", [ToolResultBlock("call_1", [TextBlock("moved"), ImageBlock(b"PNG")])]),
    ]
    items = messages_to_openai(messages)
    kinds = [i.get("type") or i.get("role") for i in items]
    # function_call and function_call_output are TOP-LEVEL items, not nested
    # blocks; the screenshot follows as its own user message because Responses
    # tool output is text-only.
    assert kinds == ["function_call", "function_call_output", "user"]
    assert items[0]["call_id"] == "call_1"
    assert json.loads(items[0]["arguments"]) == {"buttons": ["a"]}
    assert items[1]["output"] == "moved"
    assert items[2]["content"][0]["type"] == "input_image"
    assert items[2]["content"][0]["image_url"].startswith("data:image/png;base64,")


def test_openai_request_shape_and_no_reasoning_knob():
    poster = FakePoster({"output": [], "usage": {}})
    agent = OpenAIAgent(get_model("gpt"), post=poster, api_key="k")
    agent.act("SYSTEM", [Message("user", [TextBlock("hi")])], [PRESS_BUTTONS])

    p = poster.payload
    assert poster.url.endswith("/v1/responses")  # NOT /v1/chat/completions
    assert p["model"] == "gpt-5.6-terra"
    assert p["instructions"] == "SYSTEM"
    assert p["temperature"] == 1.0
    assert p["parallel_tool_calls"] is False  # Tier-0: one action per turn
    assert p["tools"][0]["name"] == "press_buttons"  # flat, not nested "function"
    assert p["tools"][0]["parameters"] == PRESS_BUTTONS.input_schema
    # Harness neutrality: no reasoning knob is ever sent, so the model runs as
    # its vendor ships it (see the adapter docstring).
    assert "reasoning_effort" not in p and "reasoning" not in p
    assert poster.headers["Authorization"] == "Bearer k"


def test_openai_allow_parallel_lifts_the_one_tool_restriction():
    poster = FakePoster({"output": [], "usage": {}})
    agent = OpenAIAgent(get_model("gpt"), post=poster, api_key="k")
    agent.act("S", [Message("user", [TextBlock("hi")])], [PRESS_BUTTONS], allow_parallel=True)
    assert poster.payload["parallel_tool_calls"] is True


# --- Google (generateContent) ----------------------------------------------


def test_google_parses_function_call_and_synthesizes_call_id():
    body = {
        "candidates": [{
            "content": {"parts": [
                {"text": "Moving right."},
                {"functionCall": {"name": "press_buttons", "args": {"buttons": ["right"]}}},
            ]},
            "finishReason": "STOP",
        }],
        "usageMetadata": {"promptTokenCount": 1152, "candidatesTokenCount": 16},
    }
    parsed = google_parse(body)
    assert parsed.text == "Moving right."
    assert parsed.tool_calls[0].input == {"buttons": ["right"]}
    # Gemini issues no call id and pairs responses by function NAME, so the
    # adapter uses the name as the neutral id (see google.parse_response).
    assert parsed.tool_calls[0].id == "press_buttons"
    assert parsed.usage == TokenUsage(1152, 16)
    assert parsed.stop_reason == "STOP"


def test_google_uses_model_role_and_inline_data():
    messages = [
        Message("user", [ImageBlock(b"PNG"), TextBlock("look")]),
        Message("assistant", [TextBlock("ok")]),
    ]
    wire = messages_to_google(messages)
    assert wire[0]["role"] == "user"
    assert wire[0]["parts"][0]["inline_data"]["mime_type"] == "image/png"
    # Gemini's assistant role is "model", not "assistant".
    assert wire[1]["role"] == "model"


def test_google_tool_result_and_image_share_one_content_entry():
    messages = [Message("user", [ToolResultBlock("c1", [TextBlock("moved"), ImageBlock(b"P")])])]
    parts = messages_to_google(messages)[0]["parts"]
    assert "functionResponse" in parts[0]
    assert "inline_data" in parts[1]


def test_google_request_shape():
    poster = FakePoster({"candidates": [], "usageMetadata": {}})
    agent = GoogleAgent(get_model("gemini"), post=poster, api_key="k")
    agent.act("SYSTEM", [Message("user", [TextBlock("hi")])], [PRESS_BUTTONS])

    p = poster.payload
    assert "gemini-3.5-flash:generateContent" in poster.url
    assert p["systemInstruction"]["parts"][0]["text"] == "SYSTEM"
    assert p["generationConfig"]["temperature"] == 1.0
    assert p["tools"][0]["function_declarations"][0]["name"] == "press_buttons"
    # Regression: the key must ride in a header, never the query string. A
    # `?key=...` URL leaked the live key into an httpx traceback on 2026-07-20.
    assert "key=" not in poster.url
    assert "k" not in poster.url
    assert poster.headers["x-goog-api-key"] == "k"


def test_google_parse_captures_the_raw_body_verbatim():
    body = {
        "candidates": [{"content": {"parts": [{"text": "hi"}]}, "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1},
    }
    assert google_parse(body).raw is body


def test_provider_http_error_redacts_the_query_string():
    from pokebench.agents._http import redact

    assert redact("https://api.example.com/v1/x:go?key=SECRET") == "https://api.example.com/v1/x:go"


# --- Transport: rate-limit retries -----------------------------------------


class ScriptedPoster(HttpxPoster):
    """Replays canned (status, headers, text, json) tuples instead of HTTP."""

    def __init__(self, responses, **kw):
        super().__init__(sleep=lambda s: self.slept.append(s), **kw)
        self.responses = list(responses)
        self.slept: list[float] = []
        self.calls = 0

    def _send(self, url, payload, headers):
        self.calls += 1
        return self.responses.pop(0)


def test_retries_rate_limit_then_succeeds():
    # A free-tier Gemini key (20 req/min) 429s mid-run; the transport must ride
    # it out rather than killing the sweep, as it did on 2026-07-20.
    body = '{"error":{"status":"RESOURCE_EXHAUSTED"},"retryDelay":"2s"}'
    poster = ScriptedPoster([
        (429, {}, body, None),
        (429, {}, body, None),
        (200, {}, "", {"ok": True}),
    ])
    assert poster("https://x/y", {}) == {"ok": True}
    assert poster.calls == 3
    # Waits as long as the provider asked (+ a small margin), not a guess.
    assert poster.slept == [2.5, 2.5]


def test_honours_retry_after_header_over_backoff():
    from pokebench.agents._http import backoff_delay

    assert backoff_delay(0, {"retry-after": "7"}, "") == 7.5
    # No hint from the provider -> exponential, and always bounded.
    assert 4.0 <= backoff_delay(2, {}, "") <= 5.0


def test_gives_up_after_max_retries_and_redacts():
    poster = ScriptedPoster([(429, {}, "slow down", None)] * 3, max_retries=2)
    with pytest.raises(ProviderHTTPError) as e:
        poster("https://api.example.com/v1/x?key=SECRET", {})
    assert "SECRET" not in str(e.value)
    assert poster.calls == 3  # initial + 2 retries


class FlakyConnectionPoster(HttpxPoster):
    """Raises transport errors for the first `fails` sends, then succeeds."""

    def __init__(self, fails, exc, **kw):
        super().__init__(sleep=lambda s: self.slept.append(s), **kw)
        self.fails = fails
        self.exc = exc
        self.slept: list[float] = []
        self.calls = 0

    def _send(self, url, payload, headers):
        self.calls += 1
        if self.calls <= self.fails:
            raise self.exc
        return (200, {}, "", {"ok": True})


def test_connection_errors_are_retried():
    # A DNS drop killed a 10.8h sweep leg on 2026-07-21: getaddrinfo failure
    # raises with no HTTP status, so the status-code path never saw it.
    import httpx

    poster = FlakyConnectionPoster(2, httpx.ConnectError("getaddrinfo failed"))
    assert poster("https://x/y", {}) == {"ok": True}
    assert poster.calls == 3
    assert len(poster.slept) == 2


def test_connection_errors_give_up_eventually_and_redact():
    import httpx

    poster = FlakyConnectionPoster(99, httpx.ConnectError("getaddrinfo failed"), max_retries=2)
    with pytest.raises(ProviderHTTPError) as e:
        poster("https://api.example.com/v1/x?key=SECRET", {})
    assert "SECRET" not in str(e.value)
    assert "ConnectError" in str(e.value)


def test_client_errors_are_not_retried():
    # A 400 is a bug in our request; retrying just burns time and money.
    poster = ScriptedPoster([(400, {}, "bad request", None)])
    with pytest.raises(ProviderHTTPError):
        poster("https://x/y", {})
    assert poster.calls == 1
    assert poster.slept == []


# --- Ollama (local, chat-completions shape) --------------------------------


def test_ollama_parses_tool_call_and_usage():
    body = {
        "choices": [{
            "message": {"content": "", "tool_calls": [{
                "id": "call_0", "type": "function",
                "function": {"name": "press_buttons", "arguments": '{"buttons":["a"]}'},
            }]},
            "finish_reason": "tool_calls",
        }],
        "usage": {"prompt_tokens": 324, "completion_tokens": 119},
    }
    parsed = ollama_parse(body)
    assert parsed.tool_calls[0].input == {"buttons": ["a"]}
    assert parsed.usage == TokenUsage(324, 119)


def test_ollama_emits_tool_role_message_then_image():
    messages = [Message("user", [ToolResultBlock("c1", [TextBlock("moved"), ImageBlock(b"P")])])]
    wire = messages_to_chat(messages)
    assert wire[0]["role"] == "tool" and wire[0]["tool_call_id"] == "c1"
    assert wire[0]["content"] == "moved"
    assert wire[1]["role"] == "user"
    assert wire[1]["content"][0]["type"] == "image_url"


def test_ollama_request_shape_prepends_system_and_is_free():
    poster = FakePoster({"choices": [], "usage": {}})
    config = get_model("qwen-local")
    agent = OllamaAgent(config, post=poster)
    agent.act("SYSTEM", [Message("user", [TextBlock("hi")])], [PRESS_BUTTONS])

    p = poster.payload
    assert poster.url == "http://localhost:11434/v1/chat/completions"
    assert p["messages"][0] == {"role": "system", "content": "SYSTEM"}
    assert p["parallel_tool_calls"] is False
    assert p["tools"][0]["function"]["name"] == "press_buttons"
    # Local inference is free; a nonzero price would corrupt the $ cap and the
    # cost column of the leaderboard.
    assert config.cost(TokenUsage(1_000_000, 1_000_000)) == 0.0


def test_ollama_parse_captures_the_raw_body_verbatim():
    body = {
        "choices": [{"message": {"content": "hi", "tool_calls": []}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }
    assert ollama_parse(body).raw is body


# --- Registry + factory ----------------------------------------------------


def test_every_registered_model_pins_temperature_and_resolves_to_an_adapter():
    from pokebench.agents.base import MODELS

    for name, config in MODELS.items():
        assert config.temperature is not None, name
        agent = make_agent(config, post=FakePoster({})) if config.provider != "anthropic" \
            else make_agent(config)
        assert agent.config is config
        assert hasattr(agent, "act")


def test_make_agent_rejects_unknown_provider():
    bogus = ModelConfig("x", "mystery", "m", 1.0, 16, 1.0, 1.0)
    with pytest.raises(KeyError, match="no adapter for provider"):
        make_agent(bogus)


def test_m3_roster_ids_and_prices_are_pinned():
    # Guards against a silent model/pricing drift invalidating a results table.
    assert get_model("sonnet").model_id == "claude-sonnet-4-6"
    assert get_model("gpt").model_id == "gpt-5.6-terra"
    assert get_model("gemini").model_id == "gemini-3.5-flash"
    assert get_model("gemini-lite").model_id == "gemini-3.1-flash-lite"
    assert get_model("qwen-local").model_id == "qwen3.5:4b"
    # The two Google entries are distinct rows, not an override of each other:
    # the paid run must add to the table, not replace the free-tier one.
    assert get_model("gemini").model_id != get_model("gemini-lite").model_id
    assert get_model("gpt").cost(TokenUsage(1_000_000, 0)) == 2.5
    assert get_model("gemini").cost(TokenUsage(0, 1_000_000)) == 9.0
