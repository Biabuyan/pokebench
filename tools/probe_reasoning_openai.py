"""Live probe: does OpenAI's Responses API report reasoning tokens in `usage`,
and does a `reasoning` request parameter change anything observable? (Task,
2026-08-15 — "measure what reasoning gpt and gemini actually spend", the open
half of CLAUDE.md "Two live caveats" #1: the same ceiling-sharing question was
answered for Anthropic by `probe_thinking_budget.py`, never for OpenAI/Google.)

**Costs real money and hits the real network** — the same one-time exception
`tools/README.md` grants `probe_thinking_budget.py`. Budgeted for **two calls**
at `gpt-5.6-terra` prices (~$0.02-0.06 total for a few hundred tokens each). Do
not re-run casually; kept per `tools/README.md`'s "don't let a finding become
unfalsifiable folklore" rule.

`agents/openai.py` sends **no `reasoning` parameter at all**, deliberately (see
its module docstring) — this probe does NOT change that adapter. It only asks,
standalone, two questions the adapter's own module docstring leaves open:

1. **Harness-current shape** — the exact request `agents/openai.py`'s
   `OpenAIAgent.act` sends today (model / instructions / input / tools /
   parallel_tool_calls / max_output_tokens / temperature, no `reasoning` key).
   Does `usage` break out a reasoning/thinking token count separately from
   `output_tokens`, the way Anthropic's `output_tokens_details.thinking_tokens`
   does?
2. **Candidate shape** — the same request plus
   `reasoning: {"effort": "medium", "summary": "auto"}` (the Responses API's
   documented reasoning knob). Does adding it change `usage`'s shape, the
   token counts, or produce a non-empty reasoning `summary` (the earlier
   harness probe, CLAUDE.md, found summary comes back `[]` under shape 1)?

Both calls use one dummy Pokémon-shaped function tool (a `press_button`
enum), mirroring the harness's real turn shape (image observation swapped for
a short text description here, since the pixels are not what's under test) so
"OpenAI rejects function tools without reasoning_effort on chat-completions"
(the adapter docstring's finding) has a fair chance to reproduce or not
reproduce on Responses.

No harness code is touched or imported by running this — it does not import
`pokebench.agents.openai` and does not go through `agents/_http.py`. It posts
directly with `httpx` (already a transitive dependency of the `anthropic`
package, per `agents/_http.py`'s own docstring), so no vendor SDK is added.

Usage (needs OPENAI_API_KEY in the environment; costs money):

    python tools/probe_reasoning_openai.py
"""

from __future__ import annotations

import json
import os
import sys

import httpx

RESPONSES_URL = "https://api.openai.com/v1/responses"
MODEL = "gpt-5.6-terra"

# Mirrors agents/openai.py's tools_to_openai() shape for one Tier-0-style tool.
TOOL = {
    "type": "function",
    "name": "press_button",
    "description": "Press one Game Boy button.",
    "parameters": {
        "type": "object",
        "properties": {
            "button": {
                "type": "string",
                "enum": ["up", "down", "left", "right", "a", "b", "start", "select"],
            }
        },
        "required": ["button"],
    },
}

SYSTEM = (
    "You are playing Pokemon Red. On each turn, decide the single next button "
    "to press and call the press_button tool with it."
)
USER_TEXT = (
    "You are standing in your bedroom facing a staircase two tiles to your "
    "east. There is a table between you and the stairs. Decide the next "
    "single button to press to make progress toward the stairs, and call "
    "press_button with it."
)


def _base_payload() -> dict:
    return {
        "model": MODEL,
        "instructions": SYSTEM,
        "input": [{"role": "user", "content": [{"type": "input_text", "text": USER_TEXT}]}],
        "tools": [TOOL],
        "parallel_tool_calls": False,
        "max_output_tokens": 1024,  # the frozen ModelConfig.max_output_tokens value
        "temperature": 1.0,  # the frozen ModelConfig.temperature value
    }


def _call(label: str, payload: dict, api_key: str) -> dict | None:
    print(f"\n=== {label} ===", file=sys.stderr)
    print("payload:", json.dumps(payload, indent=2), file=sys.stderr)
    try:
        resp = httpx.post(
            RESPONSES_URL,
            json=payload,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=120.0,
        )
    except Exception as e:  # noqa: BLE001 - one-shot diagnostic probe
        print(f"FAILED (transport {type(e).__name__}): {e}", file=sys.stderr)
        return None
    if resp.status_code >= 400:
        # A clean negative is a complete result -- print it verbatim and stop
        # this call. Do not retry with a different shape; that is a later call.
        print(f"FAILED (HTTP {resp.status_code}):", file=sys.stderr)
        print(f"  body: {resp.text[:2000]}", file=sys.stderr)
        return None
    body = resp.json()
    print("SUCCESS", file=sys.stderr)
    print("status:", body.get("status"))
    print("output item types:", [item.get("type") for item in body.get("output") or []])
    print("usage (verbatim):", json.dumps(body.get("usage") or {}, indent=2))
    for item in body.get("output") or []:
        if item.get("type") == "reasoning":
            print("reasoning item summary:", item.get("summary"))
        elif item.get("type") == "function_call":
            print("function_call:", item.get("name"), item.get("arguments"))
        elif item.get("type") == "message":
            for part in item.get("content") or []:
                if part.get("type") == "output_text":
                    print("message text:", part.get("text"))
    return body


def main() -> int:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("FAILED: OPENAI_API_KEY is not set", file=sys.stderr)
        return 1

    harness_shape = _base_payload()
    _call("call 1: harness-current shape (no reasoning param)", harness_shape, api_key)

    candidate_shape = _base_payload()
    candidate_shape["reasoning"] = {"effort": "medium", "summary": "auto"}
    _call("call 2: candidate shape (+ reasoning.effort/summary)", candidate_shape, api_key)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
