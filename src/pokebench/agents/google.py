"""Google Gemini adapter — the `generateContent` REST endpoint.

Verified live 2026-07-20 against `gemini-3.5-flash`: an image part, a
`function_declarations` tool, and `generationConfig.temperature` all coexist in
one request and come back as a `functionCall` part.

Shape differences from Anthropic:
- Roles are `user` / **`model`** (not "assistant").
- The system prompt is `systemInstruction`, not a top-level string.
- Everything is a "part": text, `inline_data` (images), `functionCall`,
  `functionResponse` — and parts mix freely inside one content entry, so a tool
  result and its screenshot ride together without the extra message the OpenAI
  adapter needs.
- There is no per-request "only one tool call" switch (`functionCallingConfig`
  only has AUTO/ANY/NONE), so Tier-0's one-action-per-turn rule rests on the
  runner's existing one-movement-per-turn enforcement rather than on the API.
"""

from __future__ import annotations

import base64

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

API_ROOT = "https://generativelanguage.googleapis.com/v1beta/models"


def _b64(png: bytes) -> str:
    return base64.standard_b64encode(png).decode("ascii")


def _blocks_to_parts(blocks: list) -> list[dict]:
    parts: list[dict] = []
    for b in blocks:
        if isinstance(b, TextBlock):
            parts.append({"text": b.text})
        elif isinstance(b, ImageBlock):
            parts.append({"inline_data": {"mime_type": "image/png", "data": _b64(b.png)}})
        elif isinstance(b, ToolUseBlock):
            part = {"functionCall": {"name": b.name, "args": b.input}}
            # Gemini 3 rejects a replayed functionCall whose thoughtSignature is
            # missing ("required for tools to work correctly", HTTP 400), so the
            # signature captured in parse_response rides back here verbatim.
            sig = (b.provider_state or {}).get("thoughtSignature")
            if sig:
                part["thoughtSignature"] = sig
            parts.append(part)
        elif isinstance(b, ToolResultBlock):
            text = "".join(x.text for x in b.content if isinstance(x, TextBlock))
            parts.append(
                {
                    "functionResponse": {
                        # Gemini pairs a response to its call by FUNCTION NAME,
                        # not by an opaque id (it issues no call ids at all).
                        # parse_response therefore uses the function name as the
                        # neutral ToolCall.id, so tool_use_id *is* the name here.
                        "name": b.tool_use_id,
                        "response": {"result": text or "(no text)"},
                    }
                }
            )
            for img in (x for x in b.content if isinstance(x, ImageBlock)):
                parts.append(
                    {"inline_data": {"mime_type": "image/png", "data": _b64(img.png)}}
                )
        else:  # pragma: no cover - guards a new block type
            raise TypeError(f"cannot serialize content block {b!r}")
    return parts


def messages_to_google(messages: list[Message]) -> list[dict]:
    return [
        {"role": "model" if m.role == "assistant" else "user",
         "parts": _blocks_to_parts(m.content)}
        for m in messages
    ]


def tools_to_google(tools: list[ToolSpec]) -> list[dict]:
    return [
        {
            "function_declarations": [
                {"name": t.name, "description": t.description, "parameters": t.input_schema}
                for t in tools
            ]
        }
    ]


class GoogleAgent:
    """`Agent` implementation over Gemini `generateContent`."""

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

            key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
            if not key:
                raise RuntimeError("GEMINI_API_KEY (or GOOGLE_API_KEY) is not set")
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
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": messages_to_google(messages),
            "tools": tools_to_google(tools),
            "toolConfig": {"functionCallingConfig": {"mode": "AUTO"}},
            "generationConfig": {
                "temperature": self.config.temperature,
                "maxOutputTokens": self.config.max_output_tokens,
            },
        }
        # Key goes in a header, never the query string: a `?key=` URL leaks the
        # secret into every exception message, log line, and traceback.
        url = f"{API_ROOT}/{self.config.model_id}:generateContent"
        return parse_response(self._post(url, payload, {"x-goog-api-key": self.api_key}))


def parse_response(body: dict) -> AgentResponse:
    """Turn a generateContent payload into the neutral AgentResponse."""
    text_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    candidates = body.get("candidates") or []
    finish = None
    if candidates:
        finish = candidates[0].get("finishReason")
        for part in candidates[0].get("content", {}).get("parts") or []:
            if "text" in part:
                text_parts.append(part["text"])
            elif "functionCall" in part:
                fc = part["functionCall"]
                args = fc.get("args") or {}
                name = fc.get("name", "")
                sig = part.get("thoughtSignature")
                tool_calls.append(
                    ToolCall(
                        # Gemini issues no call ids and pairs a functionResponse
                        # to its call by NAME, so the name is the id. (Two calls
                        # to the *same* tool in one turn would collide; Tier-0
                        # forbids that and Tier-1 only pairs differently-named
                        # tools, so it cannot arise today.)
                        id=name,
                        name=name,
                        input=args if isinstance(args, dict) else {},
                        provider_state={"thoughtSignature": sig} if sig else None,
                    )
                )
    usage = body.get("usageMetadata") or {}
    return AgentResponse(
        text="".join(text_parts),
        tool_calls=tool_calls,
        usage=TokenUsage(
            input_tokens=usage.get("promptTokenCount", 0) or 0,
            output_tokens=usage.get("candidatesTokenCount", 0) or 0,
        ),
        stop_reason=finish,
        # Verbatim response JSON only -- Google's key rides the `x-goog-api-key`
        # header on the request, never the response body, so the raw side file
        # RunTracer writes from this cannot carry it (Task A, 2026-08-11; see
        # the module docstring on why the header, not `?key=`, is used at all).
        raw=body,
    )
