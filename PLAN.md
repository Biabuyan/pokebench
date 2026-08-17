# PokéBench — build plan

**One-liner:** a fixed agent harness for Pokémon Red where **the model is the only variable** — same tools, same memory, same prompts, same observation format — measured on short, save-state-anchored scenarios with a public leaderboard and a write-up. Fills the documented gap that every famous run (Claude's, Gemini's) used incomparable custom scaffolds.

---

## Core design decisions (settle these before code)

1. **Emulator: PyBoy** (Python Game Boy emulator — headless, scriptable, screenshots as arrays, button API, save/load state). The user supplies their own legally-obtained ROM; **the repo never ships a ROM** — it ships save-state *deltas* and setup instructions. No monetisation (fan-project norms).
2. **Observation contract (the fairness core):** every model gets the identical package per turn — the screenshot + a minimal structured state block (map ID, coordinates, party summary, badges) read from the documented Pokémon Red RAM map. Two fixed **tool tiers**, reported separately: **Tier-0** = `press_buttons(sequence)` + screenshot only (pure vision agency); **Tier-1** = + structured state + a bounded notes file (the Claude-Plays-Pokémon-style memory). The May-2026 result that vision-tool upgrades were decisive for Opus 4.7 is exactly why tier must be held constant.
3. **Fixed agent scaffold:** perceive → (Tier-1: update capped notes) → decide → act. One system prompt, one tool doc, fixed context budget with a deterministic truncate/summarize policy, temperature pinned. No per-model prompt tuning — harness neutrality *is* the benchmark.
4. **Model adapter interface:** one `Agent(model_config)` seam; providers: Anthropic, OpenAI, Google, Ollama (local). Token + cost metering per provider baked into the runner.
5. **Scenarios, not full runs** (full games cost hundreds of hours — Gemini's run took ~813h): save-state-anchored milestones with RAM-checkable success predicates, step caps, and token caps.
   - **S1** Bedroom → exit Pallet Town (navigation baseline)
   - **S2** Route 1 → Viridian City Pokécenter (multi-screen navigation + healing)
   - **S3** Viridian Forest traversal (the maze that eats models)
   - **S4** (stretch) Defeat Brock (battle reasoning + type logic)
6. **Metrics:** success@cap · steps-to-success · tokens + $ per run · **stuck-loop index** (repeated-state detection — the classic failure mode, e.g. 78 hours in Mt. Moon) · invalid-action rate. Median over **N=3 seeds** (emulator is deterministic; variance comes from sampling).
7. **Trace everything:** every run emits a JSONL trace (observation hash, model output, tool call, state delta) + screenshot series → replayable, debuggable, and the raw material for the write-up.

## Security features

- **Sandboxing:** emulator + agent in Docker with **no network egress except an allowlisted LLM-API proxy** — the game container can't be a lethal-trifecta leg (LLM06 excessive agency / Rule-of-Two thinking, stated in SECURITY.md).
- **Unbounded-consumption guards (LLM10):** hard per-run step/token/dollar caps enforced by the runner, not the prompt.
- **Secrets:** API keys via env/secret manager only; pre-commit secret scanning.
- **Supply chain (OWASP A03:2025):** lockfile (`uv`), pinned deps, dependency scanning + provenance checks in CI, install scripts disabled.
- **Audit log:** the JSONL trace doubles as a complete tool-call audit trail.
- **`SECURITY.md`** maps each control to OWASP Top 10:2025 / LLM Top 10.

## Milestones

| # | Weekend(s) | Deliverable | Done when |
|---|---|---|---|
| M0 ✅ | 1 | **Harness spike** — PyBoy loads ROM, save/load states, button macros, screenshots, RAM peek (map ID + badge flags) | a *hardcoded* (non-LLM) script exits Pallet Town from the S1 save state — **DONE 2026-07-13** (PASS, 35 steps) |
| M1 | 2–3 | **Agent loop v1, one model** (start with a cheap tier, e.g. Haiku, for harness debugging) — observation contract, tool schema, capped notes memory, caps, JSONL trace | the model attempts S1 end-to-end unattended; trace replayable |
| M2 | 4 | **Scenario framework + metrics** — S1–S3 save states, success predicates, N-seed runner, results JSON, stuck-loop detector | one model has a full S1–S3 results row |
| M3 | 5 | **Cross-model** — OpenAI + Gemini + one local model adapters | first honest 4-model comparison table exists |
| M4 | 6 | **Leaderboard + trace viewer** — static site (Astro/Next, GitHub Pages/Vercel) generated from results JSON; click a cell → replay the run | public URL |
| M5 | 7–8 | **Publish** — README, SECURITY.md, blog post ("the harness giveth: why no two Pokémon runs are comparable"), socialize (pkmn.ai, LessWrong, X) | post live + repo public |

**Stretch:** S4 Brock · Tier-0 vs Tier-1 delta analysis (harness effects quantified — the headline chart) · adapt the scaffold to ARC-AGI-3 · enter the next PokéAgent Challenge.

## Cost + scope guardrails

- Budget per scenario run: cap ~300 agent turns → low single-digit dollars on mid-tier models; develop on cheap tiers, run the flagship comparison once at the end. Total project API spend target: **< $100**.
- **Not** goals: beating the game, RL training, building smarter nav tools (harness effects are the *finding*, not something to optimize away), supporting every model on earth. Four models, three scenarios, done well.

## Resources to lean on (verify current state when starting)

- PyBoy docs (game wrapper API, save states); the `pret/pokered` disassembly for the authoritative RAM map; PWhiddy's "Pokémon Red Experiments" RL repo as a reference for PyBoy + Red state-reading patterns (check its RAM-address utilities); PokéAPI not needed for the harness itself.
- The LessWrong analyses of the Claude runs (Mar 2025, May 2026) — the failure-mode taxonomy (stuck loops, note deletion, wall-hugging) is effectively a free metrics spec.

## Repo sketch

```
pokebench/
├── harness/        # pyboy wrapper, observation builder, tools, memory policy
├── agents/         # provider adapters (anthropic / openai / google / ollama)
├── scenarios/      # save-state deltas + success predicates + caps (yaml)
├── runner/         # N-seed executor, budget enforcement, JSONL traces
├── metrics/        # scoring, stuck-loop index, results.json builder
├── web/            # static leaderboard + trace viewer
├── SECURITY.md     # controls mapped to OWASP 2025 / LLM Top 10
└── blog/           # the write-up draft
```

---

## Where the code actually is vs. this plan (updated 2026-07-19)

The build docs track live status; this section is a quick plan-fidelity snapshot.
`README.md` (in this repo) has the public framing. `HANDOFF.md` and `CLAUDE.md` are the
detailed working record and session rules respectively — both gitignored since 2026-08-04,
so they exist on the build machine but **not in a fresh clone of this public repo**; not
linked here for that reason.

- **M0** ✅ done · **M1** ✅ done (Tier-0 **and** Tier-1: state block + facing + bounded
  `update_notes`; caps in code; JSONL trace + screenshots; `pokebench run`). First Haiku
  runs done — the loop works; Haiku is a weak navigator (a *finding*, not a bug; it's the
  cheap debug tier per the plan).
- **Design decision #3** (perceive → update notes → decide → act, in one turn) is honored:
  at Tier-1 the model may call `update_notes` **and** `press_buttons` in the same turn.
- **Trace** carries the `observation_hash` (decision #7).
- **M2 complete (2026-07-20).** S1–S3 scenarios + anchor states, N-seed runner, metrics,
  `results.json`, and the median-over-3-seeds sweep: haiku/tier-0 has a full S1–S3 row
  (9 seeds), which is M2's stated done-condition. Haiku scores 0/9 — the weak-navigator
  baseline the plan expected from the cheap debug tier.
- **Security sandbox — written but NOT implemented in practice.** The `docker-compose.yml`
  + `docker/proxy/` egress allowlist exist, but they are **opt-in, never built or run, and
  unverified** — every run so far went straight to the host, so nothing is sandboxed yet
  (`SECURITY.md` marks it "Not implemented yet — written but never built, run, or verified").
- **Not yet started:** M3 (more model adapters), M4 (leaderboard), M5 (write-up).
