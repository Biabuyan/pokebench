"""OpenAI adapter — the **Responses** API (`/v1/responses`).

Why Responses and not chat-completions: a live probe (2026-07-20) against
`gpt-5.6-terra` showed `/v1/chat/completions` *rejects* function tools outright —

    "Function tools with reasoning_effort are not supported for gpt-5.6-terra
     in /v1/chat/completions. To use function tools, use /v1/responses or set
     reasoning_effort to 'none'."

Chat-completions therefore only works with the model's reasoning switched off
(the probe measured 18 output tokens vs 118 on Responses). Disabling reasoning
would be exactly the per-model tuning the benchmark forbids: it would pit a
crippled GPT against a default-configured Gemini and make the *harness* the
variable. So this adapter uses Responses and touches no reasoning knob — every
provider runs as its vendor ships it.

Two shape differences from Anthropic worth knowing:
- `function_call` / `function_call_output` are **top-level items** in `input`,
  not blocks nested inside a message.
- `function_call_output.output` is a plain string, so a tool result carrying a
  screenshot is emitted as the text output *plus* a following user message
  holding the image. Same content, same order — only the envelope differs.
"""

from __future__ import annotations

import base64
import json

from pokebench.agents._http import HttpxPoster
from pokebench.agents.base import AgentResponse, ModelConfig, TokenUsage, ToolCall
from pokebench.harness.observation import (
    ImageBlock,
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from pokebench.harness.tools import ToolSpec

RESPONSES_URL = "https://api.openai.com/v1/responses"


def _data_url(png: bytes) -> str:
    return "data:image/png;base64," + base64.standard_b64encode(png).decode("ascii")


def _text_of(blocks: list) -> str:
    return "".join(b.text for b in blocks if isinstance(b, TextBlock))


def messages_to_openai(messages: list[Message]) -> list[dict]:
    """Flatten neutral messages into the Responses `input` item list."""
    items: list[dict] = []
    for m in messages:
        parts: list[dict] = []
        for b in m.content:
            if isinstance(b, TextBlock):
                key = "output_text" if m.role == "assistant" else "input_text"
                parts.append({"type": key, "text": b.text})
            elif isinstance(b, ImageBlock):
                parts.append({"type": "input_image", "image_url": _data_url(b.png)})
            elif isinstance(b, ToolUseBlock):
                # Flush any accumulated parts before the top-level call item.
                if parts:
                    items.append({"role": m.role, "content": parts})
                    parts = []
                items.append(
                    {
                        "type": "function_call",
                        "call_id": b.id,
                        "name": b.name,
                        "arguments": json.dumps(b.input),
                    }
                )
            elif isinstance(b, ToolResultBlock):
                if parts:
                    items.append({"role": m.role, "content": parts})
                    parts = []
                items.append(
                    {
                        "type": "function_call_output",
                        "call_id": b.tool_use_id,
                        "output": _text_of(b.content) or "(no text)",
                    }
                )
                # Responses tool output is text-only; carry any screenshot in a
                # following user message so the observation still reaches the model.
                imgs = [x for x in b.content if isinstance(x, ImageBlock)]
                if imgs:
                    items.append(
                        {
                            "role": "user",
                            "content": [
                                {"type": "input_image", "image_url": _data_url(i.png)}
                                for i in imgs
                            ],
                        }
                    )
            else:  # pragma: no cover - guards a new block type
                raise TypeError(f"cannot serialize content block {b!r}")
        if parts:
            items.append({"role": m.role, "content": parts})
    return items


def tools_to_openai(tools: list[ToolSpec]) -> list[dict]:
    # Responses uses a flat function tool (no nested "function" object).
    return [
        {
            "type": "function",
            "name": t.name,
            "description": t.description,
            "parameters": t.input_schema,
        }
        for t in tools
    ]


class OpenAIAgent:
    """`Agent` implementation over the OpenAI Responses API."""

    def __init__(self, config: ModelConfig, post=None, api_key: str | None = None):
        self.config = config
        # log=print so a rate-limit backoff shows up as progress rather
        # than looking like the run has hung.
        self._post = post or HttpxPoster(log=print)
        self._api_key = api_key

    @property
    def api_key(self) -> str:
        if self._api_key is None:
            import os

            key = os.environ.get("OPENAI_API_KEY")
            if not key:
                raise RuntimeError("OPENAI_API_KEY is not set")
            self._api_key = key
        return self._api_key

    def act(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        allow_parallel: bool = False,
    ) -> AgentResponse:
        payload = {
            "model": self.config.model_id,
            "instructions": system,
            "input": messages_to_openai(messages),
            "tools": tools_to_openai(tools),
            "parallel_tool_calls": bool(allow_parallel),
            "max_output_tokens": self.config.max_output_tokens,
            "temperature": self.config.temperature,
        }
        body = self._post(
            RESPONSES_URL, payload, {"Authorization": f"Bearer {self.api_key}"}
        )
        return parse_response(body)


def parse_response(body: dict) -> AgentResponse:
    """Turn a Responses payload into the neutral AgentResponse."""
    text_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    for item in body.get("output") or []:
        kind = item.get("type")
        if kind == "function_call":
            try:
                args = json.loads(item.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            tool_calls.append(
                ToolCall(
                    id=item.get("call_id") or item.get("id") or "",
                    name=item.get("name", ""),
                    input=args if isinstance(args, dict) else {},
                )
            )
        elif kind == "message":
            for part in item.get("content") or []:
                if part.get("type") == "output_text":
                    text_parts.append(part.get("text", ""))
        elif kind == "reasoning":
            # A reasoning summary is its own top-level item, not nested inside
            # "message" -- the old parser never looked here. `summary` is a
            # list of parts, normally `{"type": "summary_text", "text": ...}`
            # when populated.
            #
            # Live-verified 2026-08-06 (Task C, gpt-5.6-terra via this exact
            # request shape): the envelope above is real, but `summary` came
            # back `[]` -- empty -- because this adapter sends no `reasoning`
            # request param (deliberately, see the module docstring), and
            # OpenAI does not populate a summary unless one is requested. That
            # same live turn also had no separate "message" item (the model
            # went straight from reasoning to the tool call, per the system
            # prompt's "act by calling a tool; never reply with text alone").
            # So in practice, under this harness's current request shape,
            # this loop usually has nothing to add and `model.text` stays
            # empty on real GPT turns -- this parses correctly whenever a
            # summary IS present, but does not by itself guarantee one is.
            for part in item.get("summary") or []:
                if isinstance(part, str):
                    text_parts.append(part)
                elif isinstance(part, dict):
                    text_parts.append(part.get("text", ""))
    usage = body.get("usage") or {}
    return AgentResponse(
        # Cosmetic only (affects `pokebench replay --full` output; no scoring
        # metric reads `model.text` -- metrics/score.py keys off
        # `record["tool_call"]`/`observation_hash`). A space keeps a reasoning
        # summary and the message text from running together unseparated
        # ("Checking the map.Going up.") when both are present.
        text=" ".join(p for p in text_parts if p),
        tool_calls=tool_calls,
        usage=TokenUsage(
            input_tokens=usage.get("input_tokens", 0) or 0,
            output_tokens=usage.get("output_tokens", 0) or 0,
        ),
        stop_reason=body.get("status"),
        # The decoded response JSON, verbatim -- never the request (the key
        # rides in a header on the way out, never in this payload or in the
        # provider's reply), so nothing key-shaped reaches the raw side file
        # RunTracer writes from this (Task A, 2026-08-11).
        raw=body,
    )
