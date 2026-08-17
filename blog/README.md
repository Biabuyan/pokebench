# The write-up (M5)

**Status: drafted.** `the-harness-giveth.md` in this directory is the M5 post — title
**"the harness giveth: why no two Pokémon runs are comparable"** — written against the
published `results.json`/`results_traces.txt` and the traces under `runs/`, with every
figure it uses checked against those artifacts (or the trace files themselves) before
being used, not transcribed from an earlier draft or from HANDOFF's prose on trust. It has
not been reviewed or published externally yet.

**Correction to this file's own old skeleton, below (kept struck, not deleted, per this
project's convention of correcting loudly rather than silently editing):**

~~1. Every famous Pokémon run used a different scaffold — the results measure the
   harness as much as the model (the May-2026 vision-tool upgrade is the smoking gun).
2. PokéBench: hold the harness constant, vary only the model; Tier-0 vs Tier-1.
3. Results: the 4-model table + the Tier-0/Tier-1 delta chart (the headline).
4. Failure-mode taxonomy observed (stuck loops, wall-hugging, note misuse) vs the
   LessWrong analyses.
5. What this implies for agent evals generally.~~

Two things were wrong with the plan above by the time M5 was actually written, and the
post was built around what the repo can actually support instead:

- **There is no Tier-0/Tier-1 delta chart, and there never has been one.** M3 ran every
  model at Tier-0 only; the Tier-1 comparison was priced (an estimated $4–6 to run
  gemini/gpt through it) and **deliberately deferred to preserve budget for M4** — a real
  decision, not an oversight, recorded in `HANDOFF.md`. Calling it "the headline" in the
  old skeleton was aspirational at the time it was written and stayed unfixed for too
  long. The published post does not depend on this chart existing and does not claim a
  Tier-0/Tier-1 delta anywhere. If a future session runs that comparison, it gets its own
  post or a substantial update to this one — not a chart bolted onto a piece that was
  written without it.
- **It's not a 4-model table.** The turn-matched cross-model comparison is **five** models
  across four vendors (`gemini-3.5-flash`, `gemini-3.1-flash-lite`, `gpt-5.6-terra`,
  `qwen3.5:4b`, `claude-sonnet-4-6`); a sixth, Haiku, has a row in `results.json` too but on
  the older fixed-turn-budget caps, not turn-matched, and is deliberately kept out of the
  head-to-head comparison (see `CLAUDE.md`'s caveat 2 — "do not mix the haiku rows into the
  M3 table"). The post states this distinction explicitly rather than rounding to a
  cleaner-sounding model count.

What actually shipped, structured around material that exists rather than the original
five-point plan: the comparability gap; the frozen-harness methodology and why caps live
in runner code, never the prompt; the headline tile-count number with its bimodal seed
spread stated next to it, not hidden inside the median; the `max_output_tokens=1024`
finding (a shared constant that silently penalized only Anthropic's reasoning, so the
published sonnet row is its reasoning-**off** leg); two traces read line by line (sonnet's
91-turn stall at the wrong tile, gemini's self-correction quotes, both re-verified directly
against `run.jsonl` for this post, not just against `HANDOFF.md`'s summary of them); an
informal, clearly-labelled 500-turn probe finding that a model can navigate confidently
while its own party is dying of an untracked status ailment; and the Viridian Forest
north-gate claim's full retraction, told procedurally rather than summarized. It closes on
what the pattern implies for agent evals generally — including that the project's own
eval-integrity gate had a blind spot (silently averaging mismatched-cap-turn seeds into one
row) that took a dedicated check to find and close, which is itself an instance of the
post's own thesis.

No Pokémon Red screenshots are used anywhere in the post — deliberate, matching the rest of
this repo: no ROM ships here and nothing derived from one does either.
