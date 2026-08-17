"""Live probe: does Anthropic's `thinking: {"type": "enabled", "budget_tokens": N}`
shape work, and what does it require of `max_tokens`? (Task, 2026-08-04, Stage 0
of the thinking-budget plan — see CLAUDE.md "Two live caveats" #1 for the
39%/37% ceiling-hit pathology this is meant to fix.)

**Costs real money and hits the real network** — the one exception to
`tools/README.md`'s "$0 by construction" rule, because the question here is
about the live API's request/response contract, not ROM geometry, and cannot
be answered offline. Budgeted for **exactly one call** (~$0.01-0.05 at
sonnet-4-6 prices for a few hundred tokens). Do not re-run this casually; it
is kept, per `tools/README.md`'s own rule, so the finding stays falsifiable
instead of becoming "a previous session said it worked" folklore — the exact
failure mode that motivated keeping probe scripts in the first place.

Sends ONE minimal `messages.create` call to `claude-sonnet-4-6` with:

    thinking: {"type": "enabled", "budget_tokens": 1024}   # SDK's documented
                                                            # minimum (see
                                                            # anthropic.types
                                                            # .message_create_params
                                                            # .ThinkingConfigParam
                                                            # docstring, checked
                                                            # offline before
                                                            # spending anything:
                                                            # "Requires a minimum
                                                            # budget of 1,024
                                                            # tokens and counts
                                                            # towards your
                                                            # max_tokens limit.")
    max_tokens: 2048   # budget_tokens (1024) + the frozen answer allowance
                       # (max_output_tokens=1024) -- the exact relationship
                       # Stage 1b intends to use in agents/anthropic.py, so
                       # this call tests the real formula, not a guess.

and prints, verbatim:
  1. whether the call succeeded or the exact error body if it 400ed,
  2. resp.usage (to see whether thinking tokens are broken out separately
     from output tokens, or folded into one number), and
  3. the response content block types (to confirm a `thinking` block plus a
     `text`/`tool_use` block both come back under one budget).

No harness code is touched by running this -- it does not import
`pokebench.agents.anthropic` and does not go through `agents/_http.py`. It is
a standalone confirmation that the wire shape exists before any adapter code
is written to use it.

Usage (needs ANTHROPIC_API_KEY in the environment; costs money):

    python tools/probe_thinking_budget.py
"""

from __future__ import annotations

import json
import sys

BUDGET_TOKENS = 1024  # documented SDK minimum for extended thinking
MAX_TOKENS = BUDGET_TOKENS + 1024  # budget + the frozen 1024 answer allowance


def main() -> int:
    import anthropic

    client = anthropic.Anthropic()

    print(
        f"-> one call to claude-sonnet-4-6: thinking.budget_tokens={BUDGET_TOKENS}, "
        f"max_tokens={MAX_TOKENS}",
        file=sys.stderr,
    )
    try:
        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=MAX_TOKENS,
            thinking={"type": "enabled", "budget_tokens": BUDGET_TOKENS},
            messages=[{"role": "user", "content": "What is 2+2? Answer in one word."}],
        )
    except anthropic.APIStatusError as e:
        # A clean negative is a complete result -- print it verbatim and stop.
        # Do not retry with a different shape; that is the next session's call.
        print("FAILED (APIStatusError):", file=sys.stderr)
        print(f"  status_code: {e.status_code}", file=sys.stderr)
        print(f"  body: {e.body}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001 - this is a one-shot diagnostic probe
        print(f"FAILED ({type(e).__name__}): {e}", file=sys.stderr)
        return 1

    print("SUCCESS", file=sys.stderr)
    print("content block types:", [b.type for b in resp.content])
    print("stop_reason:", resp.stop_reason)
    print("usage:", json.dumps(resp.usage.model_dump(), indent=2))
    for block in resp.content:
        if block.type == "thinking":
            print(f"thinking block ({len(block.thinking)} chars):", block.thinking[:200])
        elif block.type == "text":
            print("text block:", block.text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
