# PokéBench

**A fixed agent harness for Pokémon Red where the model is the only variable.**

Every famous Pokémon LLM run (Claude's, Gemini's) used a different custom scaffold —
different tools, different memory, different prompts — so none of the results are
comparable. PokéBench holds the harness constant: same tools, same memory policy, same
system prompt, same observation format, measured on short save-state-anchored scenarios
with RAM-checkable success predicates.

> Status: **M0–M4 complete.** Five models from four vendors have run the same tier-0 harness,
> turn-matched at 100 turns, three seeds per scenario — 45 seeds, results in `results.json`.
> **Leaderboard is live: https://pokebench-snowy.vercel.app**
>
> **`gemini-3.5-flash` is the only model that solved more than one scenario** (5/9; S1 in a
> median 28 turns for $0.17). `gpt-5.6-terra` solved S1 3/3 and nothing else. **Nobody solved
> Viridian Forest — 0/15.** Every failing seed ran out of budget rather than failing the
> objective.
>
> The result that makes the case for the benchmark: **`claude-sonnet-4-6` scored 0/9 and
> explored 4 tiles on S1 — where `qwen3.5:4b`, running free on a laptop CPU, explored 21.**
> Price does not buy navigation.
>
> Two caveats, stated up front. **One:** the harness's shared 1024-token output ceiling
> truncated Anthropic's adaptive thinking on 39% of turns, so the sonnet row is its
> reasoning-**off** leg and is the only row without deliberation. A shared constant turned out
> not to be a neutral one — which is exactly the kind of harness effect this project exists to
> surface. **Two:** the headline figures are medians over 3 seeds, and sonnet's S1 seeds are
> 4/18/2 tiles — the median is honest but the distribution is bimodal, so `results.json`
> carries the per-seed values behind every median.
>
> Reading the traces rather than the table explains that 0/9: it is **not** a cap, the output
> ceiling, or malformed calls, but a confident misread of the screen that the model never
> revises — one seed spent 91 consecutive turns pressing `up` into a bedroom wall two tiles
> from the staircase it was looking for. The models that succeed are not the ones that read
> the screen correctly; they are the ones that notice an action had no effect.
>
> Every run is checked by an eval-integrity gate before it can become a published row, and
> rejected seeds are recorded in `results.json` with their reason rather than dropped.
> Metrics, the N-seed runner, the matrix driver, the `score`/`sweep`/`bench`/`replay` tools,
> and the static leaderboard generator are all unit-tested without a ROM or API key
> (232 tests). Next: M5, the write-up.
>
> **A follow-up probe (2 seeds, 500 turns instead of 100, informal — not part of the 45-seed
> table above) qualifies "nobody solved Viridian Forest, ran out of budget."** At 5× the turn
> budget, `gpt-5.6-terra` explored *fewer* tiles than on its own 100-turn seeds — it locked
> onto one dead-end tile for 41% of the run. `gemini-3.5-flash` covered more ground but never
> found an exit either, and its lone Pokémon took a poison hit mid-battle that bled it to 0 HP
> over the next 26 turns. **Corrected finding, not the original one:** the model did notice the
> hazard — its own commentary named the exact move ("POISON STING") in the turns just before it
> stuck, not the turn it actually stuck, and never mentioned poison again until the faint. That
> fact scrolled out of the harness's 10-turn rolling memory roughly 25 turns before the poison
> became fatal, and this probe ran at the tier with no notes scratchpad to hold it — but party
> HP and status are re-readable at any turn through the in-game START menu, a menu this
> project's own traces show even its weakest model driving unprompted elsewhere, so the model
> was never strictly dependent on memory for this fact either — which rules out a clean harness
> artifact: the harness made the fact hard to *retain*, but never made it *unavailable*. What's
> left is a model deficit of an uncertain kind — failing to hold what it had read, or failing to
> think to go back and re-read it — sitting on top of a harness that made the first of those
> considerably easier. A single informal seed can't separate those two, and this one doesn't.
>
> **A later, separate scripted probe initially reported finding and mapping an exit; that claim
> did not reproduce and has been retracted.** A corrected, exhaustive re-search of the reachable
> area found no exit from Viridian Forest *without winning a battle* — the probe flees wild
> encounters and cannot fight trainers, and it ran into three it could not get past. That is
> narrower than either "the forest has an exit" or "the forest has no exit": what's established
> is that no exit has been found that doesn't require combat, and combat hasn't been tried. The
> scenario's own anchor is a single Pokémon with no confirmed items — the same fragile party
> poisoned to death in the probe above — so the leading, still-untested hypothesis is that S3
> may require a fight its own starting state makes risky. Viridian Forest remains unsolved by
> every model tested, and, after this correction, genuinely open as to why.

## Design in one paragraph

Models are compared under two fixed **tool tiers**, reported separately:
**Tier-0** = `press_buttons` + screenshot only (pure vision agency);
**Tier-1** = + a structured state block (map, coordinates, party, badges — read from the
documented Pokémon Red RAM map) + a bounded notes file. One scaffold
(perceive → update notes → decide → act), one prompt, pinned temperature, deterministic
context truncation. No per-model prompt tuning — **harness neutrality is the benchmark.**
Scenarios are short and anchored to save states (S1 exit Pallet Town, S2 reach Viridian
Pokécenter, S3 traverse Viridian Forest), scored on success@cap, steps, tokens/$, a
stuck-loop index, and invalid-action rate, median of 3 seeds.

## Quickstart (Windows / macOS / Linux)

Prereqs: [uv](https://docs.astral.sh/uv/) and a **legally-obtained Pokémon Red (UE) ROM**.
This repo never ships a ROM and never will (see `roms/README.md`).

```sh
git clone https://github.com/biabuyan/pokebench && cd pokebench
uv sync                                  # creates .venv from the lockfile
# put your ROM at roms/pokemon_red.gb    (or set POKEBENCH_ROM)
```

### 1. Record the S1 anchor state (~10 min, one-time)

```sh
uv run pokebench play
```

A Game Boy window opens (arrows to move, `A`/`S` = A/B buttons, Enter = Start).
Play a fresh game up to the point where scenarios can start:

1. Intro: pick any names (short ones are convenient).
2. Leave the house and walk north into the tall grass — Prof. Oak stops you and takes
   you to his lab. **This is required:** before you have a starter, the game blocks the
   Route 1 exit, so a pre-starter state would make S1 unwinnable.
3. Pick any starter; fight the rival (win or lose, both fine).
4. Walk back home (the left house), go upstairs to your bedroom, and **stand still for
   ~5 seconds.** The recorder detects it and writes
   `scenarios/states/s1_bedroom.state` automatically — watch the console for
   `*** CAPTURED ***`, then close the window.

### 2. Run the M0 acceptance spike

```sh
uv run pokebench spike-s1            # headless, fast
uv run pokebench spike-s1 --window   # watch it play
```

A scripted, non-LLM route walks bedroom → downstairs → across Pallet Town → Route 1 and
prints `PASS` when `wCurMap == ROUTE_1`, plus a JSONL trace + screenshot series under
`runs/spike-s1/`. If it gets stuck (tile geometry is data, not code), tweak the route in
`scenarios/s1_exit_pallet.yaml` and re-run — every step's map/coords are in the log.

Other tools:

```sh
uv run pokebench inspect --state scenarios/states/s1_bedroom.state --png peek.png
uv run pytest                        # 232 unit tests, no ROM needed
```

## Repo layout

| Path | What it is |
|---|---|
| `src/pokebench/harness/` | PyBoy wrapper, RAM map (from pret/pokered), state reader, observation contract, `press_buttons` + notes tools, system prompt, JSONL tracing |
| `src/pokebench/agents/` | Provider adapters — Anthropic, OpenAI, Google, Ollama (all built, no vendor SDKs) |
| `src/pokebench/runner/` | Agent loop + hard turn/token/$/wall caps, rolling-history window, N-seed `bench` executor |
| `src/pokebench/metrics/` | success@cap, stuck-loop + idle metrics, `results.json` builder |
| `src/pokebench/replay.py` | Offline trace inspector (`pokebench replay`) |
| `scenarios/` | Scenario specs (YAML) + locally-recorded anchor save states (S1–S3) |
| `Dockerfile`, `docker/` | Optional sandbox — egress control (allowlist proxy, no direct egress) is **built and verified (2026-08-11)**; still **opt-in**, and no agent run has ever used it |
| `PLAN.md` | The full build plan |
| `web/` | Static leaderboard + trace viewer (`pokebench site build`, stdlib-only) — generated output is not committed; the built site is deployed at https://pokebench-snowy.vercel.app |
| `blog/` | (M5) the write-up |
| `SECURITY.md` | Controls mapped to OWASP Top 10:2025 / LLM Top 10 |

## Roadmap

- [x] **M0** Harness spike — PyBoy, save states, button macros, RAM peek, scripted Pallet Town exit
- [x] **M1** Agent loop v1 with one model — observation contract, tool tiers (0/1), `press_buttons` + capped notes, hard caps, JSONL traces; Haiku ran S1–S3 unattended
- [x] **M2** Scenario framework + metrics — S1–S3 scenarios + anchor states recorded, N-seed runner, six metrics (incl. stuck-loop + idle rate), `results.json`, `pokebench score`/`bench`/`replay`, and the median-over-3-seeds sweep: haiku/tier-0 has a full S1–S3 row (0/9, ~$6)
- [x] **M3** Cross-model — OpenAI / Google / Ollama adapters behind the same seam, and the first honest cross-model table: 5 models × S1–S3 × 3 seeds, turn-matched at 100 turns (`results.json`)
- [x] **M4** Leaderboard + trace viewer (static site) — live at https://pokebench-snowy.vercel.app, built offline with `pokebench site build` and deployed by hand (no CI/auto-deploy)
- [ ] **M5** Write-up: *the harness giveth — why no two Pokémon runs are comparable*

## Legal

Fan project. No ROMs are distributed; you must own the game. No monetisation.
Pokémon is © Nintendo / Creatures Inc. / GAME FREAK inc.

## License

MIT — see [LICENSE](LICENSE).
