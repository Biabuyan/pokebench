"""Anthropic Messages API adapter — the M1 model behind the fixed harness.

Pure translation: neutral messages/tools in (built by the harness), an
`AgentResponse` out. It adds no context and does not touch the prompt or the
observation — those are frozen upstream. The `anthropic` package and the API
key are only needed when `act()` actually calls the API, so importing this
module (and unit-testing it with a fake client) needs neither.
"""

from __future__ import annotations

import base64

from pokebench.agents.base import AgentResponse, ModelConfig, TokenUsage, ToolCall
from pokebench.harness.observation import (
    SCREENSHOT_MEDIA_TYPE,
    ImageBlock,
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from pokebench.harness.tools import ToolSpec

_MEDIA_TYPE = SCREENSHOT_MEDIA_TYPE


def _content_to_anthropic(blocks: list) -> list[dict]:
    out: list[dict] = []
    for b in blocks:
        if isinstance(b, TextBlock):
            out.append({"type": "text", "text": b.text})
        elif isinstance(b, ImageBlock):
            out.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": _MEDIA_TYPE,
                        "data": base64.standard_b64encode(b.png).decode("ascii"),
                    },
                }
            )
        elif isinstance(b, ToolUseBlock):
            out.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
        elif isinstance(b, ToolResultBlock):
            out.append(
                {
                    "type": "tool_result",
                    "tool_use_id": b.tool_use_id,
                    "content": _content_to_anthropic(b.content),
                    "is_error": b.is_error,
                }
            )
        else:  # pragma: no cover - guards against a new block type slipping through
            raise TypeError(f"cannot serialize content block {b!r}")
    return out


def messages_to_anthropic(messages: list[Message]) -> list[dict]:
    """Neutral messages -> Anthropic message list.

    With extended thinking on, the model returns a `thinking` block alongside
    its tool call. Anthropic asks that those be echoed back unchanged when the
    conversation continues on the same model, and they must come **first** in
    the assistant turn. Dropping them does not error (verified 2026-07-21 —
    unlike Gemini, which 400s on a missing thoughtSignature), but it would mean
    the model loses its own prior reasoning every turn, so they are carried
    through `ToolUseBlock.provider_state` and re-emitted here.
    """
    out: list[dict] = []
    for m in messages:
        content = _content_to_anthropic(m.content)
        if m.role == "assistant":
            thinking = [
                blk
                for b in m.content
                if isinstance(b, ToolUseBlock)
                for blk in (b.provider_state or {}).get("thinking_blocks", [])
            ]
            if thinking:
                content = [*thinking, *content]
        out.append({"role": m.role, "content": content})
    return out


def tools_to_anthropic(tools: list[ToolSpec]) -> list[dict]:
    return [
        {"name": t.name, "description": t.description, "input_schema": t.input_schema}
        for t in tools
    ]


class AnthropicAgent:
    """`Agent` implementation over `anthropic.Anthropic().messages.create`."""

    def __init__(self, config: ModelConfig, client=None):
        self.config = config
        self._client = client  # inject a fake in tests; real client built lazily

    @property
    def client(self):
        if self._client is None:
            import anthropic  # lazy: no dependency unless we actually call the API

            # The SDK's default is max_retries=2, which is not enough for an
            # unattended multi-hour sweep: a DNS drop overnight on 2026-07-21
            # raised APIConnectionError straight through and killed a leg after
            # 10.8 hours. The explicit timeout bounds a single hung request —
            # observed turns take ~4s, so 120s is generous. (Non-Anthropic
            # adapters get the equivalent behaviour from agents/_http.py.)
            self._client = anthropic.Anthropic(max_retries=8, timeout=120.0)
        return self._client

    def act(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        allow_parallel: bool = False,
    ) -> AgentResponse:
        # Auto: the model may reason in text before acting, or waste the turn by
        # not acting (a real agentic weakness the benchmark should capture).
        # Tier-1 allows parallel calls so `update_notes` + `press_buttons` can
        # share a turn (perceive -> note -> act); Tier-0 forbids it (one action).
        kwargs = dict(
            model=self.config.model_id,
            max_tokens=self.config.max_output_tokens,
            temperature=self.config.temperature,
            system=system,
            tools=tools_to_anthropic(tools),
            messages=messages_to_anthropic(messages),
        )
        if getattr(self.config, "reasoning", False):
            budget = getattr(self.config, "thinking_budget_tokens", None)
            if budget:
                # Explicit budget (Stage 1b, 2026-08-14): unlike adaptive
                # thinking below, this raises max_tokens to fit the budget
                # PLUS the unchanged 1024 answer allowance, so deliberation
                # can no longer crowd the answer out of the same ceiling —
                # the mechanism behind the 39%/37% pathology this exists to
                # fix (CLAUDE.md "Two live caveats" #1). Formula verified live
                # by the Stage 0 probe (tools/probe_thinking_budget.py, one
                # call): budget_tokens=1024 + max_tokens=2048 succeeded.
                kwargs["thinking"] = {"type": "enabled", "budget_tokens": budget}
                kwargs["max_tokens"] = budget + self.config.max_output_tokens
            else:
                # Adaptive thinking: the model decides how much to think, so no
                # per-model budget is tuned. Verified to fit inside the frozen
                # max_output_tokens=1024 (282 output tokens on a real observation).
                # `tool_choice` is omitted here because forcing/serialising tool
                # choice conflicts with extended thinking; one movement per turn is
                # still enforced by the runner, as it is for Gemini.
                kwargs["thinking"] = {"type": "adaptive"}
        else:
            # Auto: the model may reason in text before acting, or waste the
            # turn by not acting (a real agentic weakness worth capturing).
            # Tier-1 allows parallel calls so `update_notes` + `press_buttons`
            # can share a turn; Tier-0 forbids it (one action).
            tool_choice = {"type": "auto"}
            if not allow_parallel:
                tool_choice["disable_parallel_tool_use"] = True
            kwargs["tool_choice"] = tool_choice
        return parse_response(self.client.messages.create(**kwargs))


def _as_wire(block):
    """Serialise a response block back to its request shape, unchanged."""
    dump = getattr(block, "model_dump", None)
    return dump(exclude_none=True) if callable(dump) else dict(block)


def parse_response(resp) -> AgentResponse:
    """Turn an Anthropic Message into the neutral AgentResponse."""
    text_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    # Captured verbatim so the assistant turn can be replayed with its reasoning
    # intact (see messages_to_anthropic). Opaque to the harness.
    thinking_blocks = [
        _as_wire(b) for b in resp.content if b.type in ("thinking", "redacted_thinking")
    ]
    for block in resp.content:
        if block.type == "text":
            text_parts.append(block.text)
        elif block.type == "tool_use":
            tool_calls.append(
                ToolCall(
                    id=block.id,
                    name=block.name,
                    input=dict(block.input),
                    provider_state=(
                        {"thinking_blocks": thinking_blocks} if thinking_blocks else None
                    ),
                )
            )
    usage = TokenUsage(
        input_tokens=getattr(resp.usage, "input_tokens", 0) or 0,
        output_tokens=getattr(resp.usage, "output_tokens", 0) or 0,
    )
    return AgentResponse(
        text="".join(text_parts),
        tool_calls=tool_calls,
        usage=usage,
        stop_reason=getattr(resp, "stop_reason", None),
        raw=_response_to_raw(resp),
    )


def _response_to_raw(resp) -> dict | None:
    """The full Message body, verbatim, for the RunTracer raw side file.

    The real SDK Message is a pydantic model (`model_dump`); test fixtures
    here are plain `SimpleNamespace`s with no such method, so this stays
    `None` for them exactly as it did before `raw` existed — no other adapter
    test needed to change. `mode="json"` keeps the result plain-JSON-safe
    (enums/datetimes stringified) since this is written straight to disk.
    """
    dump = getattr(resp, "model_dump", None)
    if not callable(dump):
        return None
    try:
        return dump(mode="json")
    except TypeError:
        return dump()
