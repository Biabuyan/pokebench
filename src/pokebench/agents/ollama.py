"""Ollama adapter — a locally served model over Ollama's OpenAI-compatible
`/v1/chat/completions` endpoint.

This is PLAN's "one local model" leg: no API key, no egress, no per-token cost.
Verified live 2026-07-20 with `qwen3.5:4b` — an image part and a function tool
*do* work in the same request, returning `press_buttons(["a"])`. The catch is
speed, not capability: that single turn took **28.6s** on this machine's
integrated GPU (CPU inference), roughly 14x the hosted models. Size sweeps
accordingly and raise the scenario wall-clock cap, or the local row gets fewer
turns for a hardware reason rather than a capability one.

Unlike the OpenAI Responses adapter, this speaks the older chat-completions
shape: tool results are `{"role": "tool", ...}` messages whose content is a
plain string, so a screenshot rides in a following user message.
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

DEFAULT_BASE_URL = "http://localhost:11434/v1"


def _data_url(png: bytes) -> str:
    return "data:image/png;base64," + base64.standard_b64encode(png).decode("ascii")


def messages_to_chat(messages: list[Message]) -> list[dict]:
    """Neutral messages -> OpenAI chat-completions message list."""
    out: list[dict] = []
    for m in messages:
        parts: list[dict] = []
        calls: list[dict] = []
        for b in m.content:
            if isinstance(b, TextBlock):
                parts.append({"type": "text", "text": b.text})
            elif isinstance(b, ImageBlock):
                parts.append({"type": "image_url", "image_url": {"url": _data_url(b.png)}})
            elif isinstance(b, ToolUseBlock):
                calls.append(
                    {
                        "id": b.id,
                        "type": "function",
                        "function": {"name": b.name, "arguments": json.dumps(b.input)},
                    }
                )
            elif isinstance(b, ToolResultBlock):
                if parts:
                    out.append({"role": m.role, "content": parts})
                    parts = []
                text = "".join(x.text for x in b.content if isinstance(x, TextBlock))
                out.append(
                    {
                        "role": "tool",
                        "tool_call_id": b.tool_use_id,
                        "content": text or "(no text)",
                    }
                )
                imgs = [x for x in b.content if isinstance(x, ImageBlock)]
                if imgs:
                    out.append(
                        {
                            "role": "user",
                            "content": [
                                {"type": "image_url", "image_url": {"url": _data_url(i.png)}}
                                for i in imgs
                            ],
                        }
                    )
            else:  # pragma: no cover - guards a new block type
                raise TypeError(f"cannot serialize content block {b!r}")
        if parts or calls:
            msg: dict = {"role": m.role}
            if parts:
                msg["content"] = parts
            if calls:
                msg["content"] = msg.get("content") or None
                msg["tool_calls"] = calls
            out.append(msg)
    return out


def tools_to_chat(tools: list[ToolSpec]) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.input_schema,
            },
        }
        for t in tools
    ]


class OllamaAgent:
    """`Agent` implementation over a local Ollama server."""

    def __init__(
        self,
        config: ModelConfig,
        post=None,
        base_url: str = DEFAULT_BASE_URL,
    ):
        self.config = config
        # log=print so a rate-limit backoff shows up as progress rather
        # than looking like the run has hung.
        self._post = post or HttpxPoster(log=print)
        self.base_url = base_url.rstrip("/")

    def act(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        allow_parallel: bool = False,
    ) -> AgentResponse:
        payload = {
            "model": self.config.model_id,
            "messages": [{"role": "system", "content": system}, *messages_to_chat(messages)],
            "tools": tools_to_chat(tools),
            "parallel_tool_calls": bool(allow_parallel),
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_output_tokens,
        }
        body = self._post(f"{self.base_url}/chat/completions", payload, {})
        return parse_response(body)


def parse_response(body: dict) -> AgentResponse:
    """Turn a chat-completions payload into the neutral AgentResponse."""
    choices = body.get("choices") or []
    text = ""
    tool_calls: list[ToolCall] = []
    finish = None
    if choices:
        finish = choices[0].get("finish_reason")
        msg = choices[0].get("message") or {}
        text = msg.get("content") or ""
        for c in msg.get("tool_calls") or []:
            fn = c.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            tool_calls.append(
                ToolCall(
                    id=c.get("id") or "",
                    name=fn.get("name", ""),
                    input=args if isinstance(args, dict) else {},
                )
            )
    usage = body.get("usage") or {}
    return AgentResponse(
        text=text,
        tool_calls=tool_calls,
        usage=TokenUsage(
            input_tokens=usage.get("prompt_tokens", 0) or 0,
            output_tokens=usage.get("completion_tokens", 0) or 0,
        ),
        stop_reason=finish,
        # Local server, no key at all -- still verbatim response only, for the
        # same reason as the hosted adapters (Task A, 2026-08-11).
        raw=body,
    )
