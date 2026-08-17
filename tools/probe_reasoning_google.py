"""Live probe: does Gemini's `generateContent` report reasoning/thought tokens
in `usageMetadata`, and does `generationConfig.thinkingConfig` change anything
observable? (Task, 2026-08-15 — "measure what reasoning gpt and gemini
actually spend", the open half of CLAUDE.md "Two live caveats" #1: the same
ceiling-sharing question was answered for Anthropic by
`probe_thinking_budget.py`, never for OpenAI/Google.)

**Costs real money and hits the real network** — the same one-time exception
`tools/README.md` grants `probe_thinking_budget.py`. Budgeted for **two calls**
at `gemini-3.5-flash` prices (~$0.01-0.03 total for a few hundred tokens
each). Do not re-run casually; kept per `tools/README.md`'s "don't let a
finding become unfalsifiable folklore" rule.

`agents/google.py` sends **no `thinkingConfig` at all**, deliberately — this
probe does NOT change that adapter. It only asks, standalone, two questions:

1. **Harness-current shape** — the exact request `agents/google.py`'s
   `GoogleAgent.act` sends today (systemInstruction / contents / tools /
   toolConfig / generationConfig.temperature+maxOutputTokens, no
   `thinkingConfig` key). Does `usageMetadata` break out a reasoning/thought
   token count separately from `candidatesTokenCount`?
2. **Candidate shape** — the same request plus
   `generationConfig.thinkingConfig: {"thinkingBudget": 1024,
   "includeThoughts": true}`. Does adding it change `usageMetadata`'s shape,
   the token counts, or surface a `thought` part in the response content
   (Gemini marks thought parts with `"thought": true` inside a normal `part`,
   not a separate item type the way OpenAI/Anthropic do)?

Both calls use one dummy Pokémon-shaped function-declaration tool (a
`press_button` enum), mirroring the harness's real turn shape (image
observation swapped for a short text description here, since the pixels are
not what's under test).

No harness code is touched or imported by running this — it does not import
`pokebench.agents.google` and does not go through `agents/_http.py`. It posts
directly with `httpx` (already a transitive dependency of the `anthropic`
package, per `agents/_http.py`'s own docstring), so no vendor SDK is added.

Usage (needs GEMINI_API_KEY, or GOOGLE_API_KEY, in the environment; costs
money):

    python tools/probe_reasoning_google.py
"""

from __future__ import annotations

import json
import os
import sys

import httpx

API_ROOT = "https://generativelanguage.googleapis.com/v1beta/models"
MODEL = "gemini-3.5-flash"

# Mirrors agents/google.py's tools_to_google() shape for one Tier-0-style tool.
TOOL = {
    "function_declarations": [
        {
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
    ]
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
        "systemInstruction": {"parts": [{"text": SYSTEM}]},
        "contents": [{"role": "user", "parts": [{"text": USER_TEXT}]}],
        "tools": [TOOL],
        "toolConfig": {"functionCallingConfig": {"mode": "AUTO"}},
        "generationConfig": {
            "temperature": 1.0,  # the frozen ModelConfig.temperature value
            "maxOutputTokens": 1024,  # the frozen ModelConfig.max_output_tokens value
        },
    }


def _call(label: str, payload: dict, api_key: str) -> dict | None:
    print(f"\n=== {label} ===", file=sys.stderr)
    print("payload:", json.dumps(payload, indent=2), file=sys.stderr)
    url = f"{API_ROOT}/{MODEL}:generateContent"
    try:
        # Key in a header, never `?key=` -- a query-string key leaks into
        # every exception message and log line (agents/google.py's own rule).
        resp = httpx.post(
            url, json=payload, headers={"x-goog-api-key": api_key}, timeout=120.0
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
    candidates = body.get("candidates") or []
    finish = candidates[0].get("finishReason") if candidates else None
    print("finishReason:", finish)
    print("usageMetadata (verbatim):", json.dumps(body.get("usageMetadata") or {}, indent=2))
    for cand in candidates:
        for part in cand.get("content", {}).get("parts") or []:
            if part.get("thought"):
                print("thought part:", part.get("text", "")[:200])
            elif "text" in part:
                print("text part:", part["text"])
            elif "functionCall" in part:
                print("functionCall part:", part["functionCall"])
    return body


def main() -> int:
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        print("FAILED: GEMINI_API_KEY (or GOOGLE_API_KEY) is not set", file=sys.stderr)
        return 1

    harness_shape = _base_payload()
    _call("call 1: harness-current shape (no thinkingConfig)", harness_shape, api_key)

    candidate_shape = _base_payload()
    candidate_shape["generationConfig"]["thinkingConfig"] = {
        "thinkingBudget": 1024,
        "includeThoughts": True,
    }
    _call("call 2: candidate shape (+ generationConfig.thinkingConfig)", candidate_shape, api_key)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
