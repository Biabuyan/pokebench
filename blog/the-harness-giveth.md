# The harness giveth: why no two Pokémon runs are comparable

*PokéBench — a fixed agent harness for Pokémon Red where the model is the only variable.
Leaderboard: [pokebench-snowy.vercel.app](https://pokebench-snowy.vercel.app). Repo:
[github.com/Biabuyan/pokebench](https://github.com/Biabuyan/pokebench).*

Every widely-shared "an LLM plays Pokémon" run — Claude's, Gemini's, the various open
community forks — ships with its own scaffold: its own tool set, its own
memory system, its own prompt, its own idea of what the model gets to see on screen. When
one of these runs clears a badge or gets stuck at the same gate for a week, the discourse
argues about the model. It should be arguing about the harness at least as much, because
nothing in the setup lets you separate the two. If a run plays better after someone adds a
new navigation tool mid-run, did the model get smarter, or did the harness get easier?
There is no control condition, so there is no way to answer that from the run itself.

That's the gap PokéBench tries to close. Freeze everything except the model — same
screenshot format and scale, same tool the agent calls to move, same system prompt, same
memory policy, same hard caps — and run several models through it. What's left over after
you hold the harness constant is, by construction, attributable to the model. The
interesting result, four milestones in, is how much the harness still turns out to matter
even after you've frozen it. This post is a report on that: one headline number, one
constant that quietly wasn't neutral, three traces read line by line, a fourth scenario that
turns a diagnosis into a demonstration, two informal findings, and one retraction.

Everything numeric below is checked against `results.json` and the traces under `runs/`
that produced it (`runs/` itself is gitignored and doesn't ship with the repo —
`results_traces.txt` at the repo root lists the exact seed directories the published
table was scored from, and every exclusion with its reason, so the table is reproducible
without re-running anything, and without a ROM, an API key or a network — in **two**
commands rather than one, both given later in this post alongside the reason the split is
necessary). Where a number comes from an informal probe rather than the published table, I
say so.

## The harness, frozen

The frozen contract is the methodology, not a footnote to it. Every model sees a
480×432 screenshot (the Game Boy's native 160×144 frame, scaled 3× with nearest-neighbour
interpolation — no filtering, no cropping, no overlay) and calls one tool,
`press_buttons`, to act. The system prompt is a single versioned file; every trace records
which version produced it. The rolling history window is ten turns, with the screenshot
attached only to the current turn (older turns keep the text, not the image). Two
observation tiers exist and are reported separately: Tier-0 is vision-only; Tier-1 adds a
structured RAM-read state block (map, coordinates, facing, party, badges) plus a
1000-character notes scratchpad. Every model in the published table below ran Tier-0 — the
Tier-0/Tier-1 delta was planned but never funded.

The part worth dwelling on is where the caps live. Turn, token, dollar, and wall-clock
limits are all enforced in the runner's own code, never mentioned in the prompt. This
sounds like a minor implementation choice; it isn't. A cap stated only in the prompt
("you have 100 turns") is not a cap at all — it's a suggestion a model can ignore, forget,
or, in the case of a system that reasons about its own constraints, potentially game.
Enforcing caps in code outside the model's context means every stop is real and every
stop reason is comparable across vendors. It also means a model can't be faulted or
credited for something the harness silently decided for it — which turns out to matter a
great deal, as the next two sections show.

## The headline, and its asterisk

The published cross-model table is five models across four vendors, run turn-matched at
100 turns, tier-0, three seeds per scenario, across three scenarios (S1: exit Pallet Town;
S2: reach the Viridian City Pokémon Center; S3: cross Viridian Forest) — 45 seeds. That match
is on turn count, not dollars, and deliberately so: capping every model at the same *spend*
instead would let a cheaper model simply buy more turns than a stronger, pricier one, scoring
budget position as much as capability. Turn-matching trades that for the mirror-image risk —
a shared turn count still lets per-turn cost and reasoning depth differ by vendor, which is
exactly the shape of problem the token-ceiling section below finds baked into a shared token
count — but between the two, it was the more defensible bias to build in on purpose.

A sixth model, Anthropic's Haiku, has rows of its own in `results.json` too, but on the older
fixed-turn-budget (300/400/600-turn, dollar-bound) caps from an earlier milestone, not the
100-turn comparison; it's kept in the artifact and excluded from the head-to-head table
deliberately, and I'll do the same here. `results_traces.txt` records 54 valid seed
directories for the S1–S3 runs (45 turn-matched plus Haiku's 9) with 9 more excluded during curation
(4 for output-ceiling truncation, explained below; 4 incomplete and 1 cap mismatch, itemised
in the file) and a further 3 single-seed groups superseded by a later 3-seed re-run.

| model | S1 exit Pallet | S2 Viridian PC | S3 Viridian Forest | total |
|---|---|---|---|---|
| `gemini-3.5-flash` | 3/3 | 2/3 | 0/3 | **5/9** |
| `gpt-5.6-terra` | 3/3 | 0/3 | 0/3 | **3/9** |
| `gemini-3.1-flash-lite` | 0/3 | 0/3 | 0/3 | **0/9** |
| `qwen3.5:4b` (local, free) | 0/3 | 0/3 | 0/3 | **0/9** |
| `claude-sonnet-4-6` (reasoning **off**) | 0/3 | 0/3 | 0/3 | **0/9** |

`gemini-3.5-flash` is the only model in *this* table that solves more than one scenario, and
does it cheapest of anyone here who solves anything — a median $0.17 for the 28-turn S1 win,
versus gpt's $0.26 for 42 turns. Both halves of that flip once the fourth scenario arrives
later in this post: gpt solves S4 as well as S1, and gemini-lite takes S4 for a median
$0.004, an order of magnitude under gemini's cheapest win. Nobody solves S3. Every failing seed in this table ran out of
turns; not one died doing something the game recognizes as a loss. (S3's 0/15 needs a
standing caveat, repeated everywhere it appears in this post: no scripted oracle exists for
Viridian Forest, so the success predicate has never once fired in any traced run — agent or
scripted probe — and whether the scenario is winnable at all under its current anchor state
is, as of this writing, genuinely unverified. Read 0/15 as a ceiling probe, not a ranking
result.)

The number that makes the case for building this benchmark in the first place is on S1:
`claude-sonnet-4-6` explored a median of **4 tiles** before running out its 100 turns.
`qwen3.5:4b` — a 4-billion-parameter model running free on a laptop CPU, at roughly 1.6
minutes per turn — explored **21**. Same harness, same screenshot, same tool, wildly
different amount of the map actually seen. On this specific metric — tiles explored, not win
rate — price bought nothing; if anything it bought the opposite. (On win rate it's the
reverse: the only two models in the table above that solve anything at all, gemini and gpt,
are both paid ones. The two metrics point in opposite directions on this
harness, and both are true at once.)

Now the asterisk, because it's part of the finding and not a disclaimer bolted on
afterward: "4 tiles" is a median over three seeds, and the three seeds are **4, 18, and
2**. That's bimodal, not a tight cluster around 4 — one of sonnet's three runs got outside
and covered nearly as much ground as the winning models before it also ran out the clock.
The median is the honest summary statistic and the headline survives it (sonnet's *best*
seed still explored less than qwen's *worst*), but anyone who reports "sonnet explored 4
tiles" without the spread is hiding half the picture, including from themselves. `N=3` is
what the budget allows here, and a distribution like this is exactly the argument for why
`N=3` medians need their per-seed values published alongside them, which is why
`results.json` carries `seed_values` for every row, not just medians.

## A harness constant that wasn't neutral

Here is the sharpest illustration of the title. `max_output_tokens=1024` is one number,
applied identically to every model in the sweep — which looks like the textbook definition
of a fair, neutral constant. It isn't, because "output tokens" doesn't mean the same thing
across vendors. For Anthropic's models with extended thinking turned on, the reasoning
trace and the final answer draw from the *same* 1024-token budget. For the other hosted
vendors in this sweep, reasoning is either billed separately or (as turned out to be the case
for one adapter) not returned at all under this harness's request shape. The local
model is a partial exception worth naming: Ollama gets the same 1024 tokens with nothing
separating thought from answer — the same shape as Anthropic, at a much smaller dose.

And the small dose is not zero, which is worth being exact about since the alternative is
claiming a clean sweep the artifact doesn't support. `qwen-local` truncates on a median 2–8%
of turns depending on scenario; its S4 rate of 8% sits *above* the integrity gate's own 5%
ceiling-hit threshold, and those seeds stay in the published table only because the rule
additionally requires a dead-turn rate above 10%, where qwen's land at 7–9%. gemini clips 1%
of its S3 turns. Both are small beside the 39% below — but one shared number constrains one
vendor's deliberation heavily, the local model measurably, and the rest not at all.

Measured directly over the sweep, with reasoning off and then on for sonnet:

| condition | turns | mean output tokens | % turns at the 1024 ceiling | % turns with no tool call |
|---|---|---|---|---|
| sonnet, reasoning **off** | 900 | 183 | 0.0% | 1.8% |
| sonnet, reasoning **on** (adaptive) | 392 | 718 | **39.0%** | **37.2%** |

With reasoning on, sonnet ran out of output budget mid-thought on 39% of turns and, as a
direct consequence, emitted no move at all on 37% of them. Those runs aren't measuring
whether sonnet can play Pokémon; they're measuring how long a thought fits in 1024 tokens.
They were excluded from the table for exactly that reason (`output_ceiling_truncation`,
four of the nine excluded seeds mentioned above), which leaves the published sonnet row as
its **reasoning-off leg** — the only row measured with its vendor's deliberation mode
deliberately switched off. Every comparison against sonnet in this post, and in the table,
carries that asymmetry: measuring it with that mode on would have measured the harness's
token ceiling instead of the model — whether that mode is actually the *more capable* one on
this harness turns out, in the next section, to be a question the evidence doesn't settle
either way.

Raising the ceiling is the obvious fix and was considered and declined. It's a
frozen-contract change — it would have to apply to the whole sweep to stay fair, and
because thinking blocks get carried forward in history, a higher ceiling inflates every
subsequent turn's input cost too. A back-of-envelope estimate for re-sweeping sonnet alone
came to $36–54, against a remaining project budget of about $62 at the time — between 58% and
87% of everything left. A later probe then spent $5.83 of that, which made the top of the
range nearly the entire remainder.
Deferred deliberately, not an oversight — but worth being loud about, because "one shared
constant, chosen in the name of fairness, that turns out to silently change what one
vendor's row measures" is close to the thesis of this whole post in miniature.

(A related null result, while we're on constants that looked like they'd predict
something and didn't: turns with no tool call at all — `no_tool_call_rate` — are not the
failure signal they look like. Across all 60 turn-matched seeds in this post (S1–S3's 45,
plus the fourth scenario's 15 introduced later) the winning seeds span 0.0–0.19 and the losing
seeds 0.0–0.16 — fully overlapping — and the single highest dead-turn rate in the whole table
belongs to a seed that *won*. Winners do skew toward zero (14 of 20 at exactly 0.0, against 14
of 40 losers), but that is confounded with model identity — the models that win are the models
that go quiet least — and it cuts the wrong way for using the metric as a diagnostic: a high
rate tells you nothing on its own, since the worst offender in the table is a winning seed.)

That leaves one thing genuinely open above: whether sonnet's *play* would look any different
once the ceiling stopped eating its deliberation, or whether the ceiling was an accounting
artifact sitting on top of otherwise-unchanged behavior. It doesn't have to stay open — the
next section tests it directly.

## Giving sonnet a thinking budget of its own

This section is not from the published table either — an informal probe, S1 only, four
seeds total (one calibration run, then the N=3 batch quoted below), built for one purpose: to
find out whether fixing the ceiling pathology above actually changes what sonnet does, or only
changes what gets reported about it. It cost $5.00, measured — summed directly from the four
seeds' own recorded `cost_usd` ($1.2848 + $1.4583 + $1.1415 + $1.1197) — plus a separate
one-call, ~$0.01–0.05 live-API probe (`tools/probe_thinking_budget.py`) that confirmed
Anthropic's request shape before any of that was spent. It is not in `results.json`, was never
scored into the leaderboard, and should be read as exploratory, same as the HP-drain section
below — and it covers exactly one scenario; S2 and S3 have not been probed this way.

The fix: `ModelConfig` grew a `thinking_budget_tokens` field, wired into `agents/anthropic.py`
to send `thinking: {"type": "enabled", "budget_tokens": N}` with `max_tokens` raised to
`N + 1024`. The 1024-token answer allowance is exactly what it was for every published row;
thinking now gets its own separate room instead of borrowing from it. Sonnet was re-run on S1
with a 4096-token thinking budget (`pokebench bench --model sonnet --scenario
scenarios/s1_exit_pallet.yaml --thinking-budget 4096 --seeds 3`, token cap raised 4× above the
published sweep's; the dollar cap stayed at the published $2.50, which mattered not at all
because every seed, in every leg, ran out on the 100-turn cap at $1.07–1.46 and nothing else):

| sonnet / S1 | reasoning off (published) | reasoning on, 4096-token budget (N=3) |
|---|---|---|
| success | 0/3 | 0/3 |
| median cost | $1.2515 | $1.1415 |
| tiles explored | `[4, 18, 2]`, median 4 | `[6, 2, 2]`, median 2 |
| ceiling-hit rate | 0.0% | 0.0% |
| no-tool-call rate | 0.0% (median; seeds 0/16/0%) | 0.0% |

The ceiling/no-tool-call row this table is really answering is the "reasoning on (adaptive)"
row in the previous section's table: sonnet's old reasoning-on *adaptive* leg — no explicit
budget, sharing the 1024-token ceiling with its answer — hit that ceiling on 39.0% of turns and
went silent on 37.2% of them. (The published off-leg's own ceiling-hit rate is trivially 0.0%
too, since it never spends anything on thinking — the reasoning-off column in the table above is the fair
comparison for *outcome*, not for the ceiling fix.) With an explicit, separate budget, sonnet
hit the ceiling on none of its 400 reasoning-on turns and went silent on none of them —
reverified turn by turn from the raw response bodies, not just the aggregate metric: peak
output on any of the four seeds was 403 tokens, against a 1024-token ceiling that no longer has
to also hold a thought. The truncation pathology was real, and fixing it was cheap.

It changed nothing about the outcome. Still 0/3. Still ran the full 100 turns, every seed,
every leg. Tiles explored look lower under the fix (`[6, 2, 2]`, median 2) than the published
off-leg (`[4, 18, 2]`, median 4) — worth noting the calibration seed alone, run before the N=3
batch and not folded into either median above, explored 8 — but both sides here are N=3 with
wide, overlapping spreads, and a three-versus-three comparison at this spread cannot support a
claim that reasoning made things *worse*. The honest claim is narrower: there is no evidence
here that deliberation helps sonnet play S1 on this harness, at this budget. At the median the
fixed leg cost about $0.11 less ($1.1415 vs $1.2515), but the per-seed ranges overlap (on:
$1.12–1.46; off: $1.07–1.32), so the supported reading is "no more expensive," not "cheaper."

That the truncation defect and sonnet's underlying gameplay defect turn out to be separable —
fixing one for $5 changed nothing measurable about the other — points the same way as this
post's reading of sonnet's failure in the next section: the problem was not a shortage of
room to deliberate. It was perceiving whether an action had executed. One important limit on
that inference, detailed just below: sonnet drew on the budget only on turn 1 and thought on
none of the remaining 99 turns of any seed, so what this actually shows is that sonnet does
not reach for deliberation here — not that deliberation, taken up, would not have helped.

One more thing surfaced by reading the raw traces, worth recording precisely because it is not
resolved: across all four reasoning-on runs — 400 turns — a `thinking` content block appears
exactly once per seed, always on turn 1 (86, 75, 93, and 84 tokens across the four seeds
respectively, out of the 4096 offered), and never again on any of the following 99 turns of any
seed. This is not the harness dropping thinking blocks between turns: `messages_to_anthropic`
re-emits captured thinking blocks from `provider_state`, first in the assistant turn, exactly
as Anthropic's API requires — verified by reading the function, and by turns 2–10 explicitly
reporting `thinking_tokens: 0` in the response body rather than erroring. One plausible read,
offered as a hypothesis this probe does not test and cannot resolve: turn 1 is qualitatively
harder — an unfamiliar screen, no history yet — while every later turn looks to the model
enough like more of an already-understood situation that it reaches straight for a move
instead. That is a guess, not a finding, and it stays a guess until something is built to test
it.

## What "not solving it" looks like from inside a trace

The table says sonnet scored 0/9. It doesn't say why, and "why" turns out to matter more
than the number. Reading the three S1 traces end to end (cost: $0, since the traces were
already paid for) rules out the three obvious mechanical explanations. All three seeds stopped on the
turn cap, spending only about half of their dollar budget — money was never the
constraint. Peak output per turn was 197/384/240 tokens against the 1024 ceiling — zero
ceiling hits, so this isn't the truncation problem from the previous section; this is the
clean reasoning-off leg. Across all 300 turns, zero malformed tool calls, zero no-ops from
bad input. A fourth mechanical explanation — the harness's own ten-turn memory window, which
later in this post turns out to matter a great deal — does not apply to sonnet the way it
applies to the poisoning further down: the model's position is re-readable in every frame it
is given, so its position is never what ages out of context. That *in-frame* re-readability is
exactly what will not be true of the poisoning — HP is recoverable there too, but only by
choosing to open a menu.

What actually happened is a confident misread of the screen that locks in early and never
gets revised. Seed0 reaches `REDS_HOUSE_2F` at position `(5,1)` on turn 10 and then sits
there for **91 consecutive turns**, pressing `up`, narrating with increasing confidence
that it is looking at "the gate entrance to Route 1." The real staircase down is at
`(7,1)` — same row, two tiles to its right. By turn 100 the model is still describing the
exact same frame: "Now I can ACTUALLY see the current screen! The player character is
standing right below the gate." It never was. Seed1 gets outside, reaches Pallet Town,
and at turn 63 concludes it has already crossed past the tree line onto Route 1 — then
acts only intermittently for the rest of the run, sixteen percent of its turns emitting no
tool call at all, because as far as the model is concerned the scenario is already won.
Seed2 moves exactly twice, on turns 4 and 5, then spends the remaining 95
turns pressing `down` without the position ever changing again. It never mentions stairs
after turn 1; instead it narrates all 95 of those turns as though it's already on the
first floor next to the exit — a TV and computer with "Mom (NPC)" standing beside it, and,
by turn 99, "the exit (black area) is RIGHT BELOW the player — just 1 tile away!" The recorded
state says otherwise the entire time: `REDS_HOUSE_2F`, `(3,7)`, unmoved since turn 5. A stable,
confident, wrong belief about which floor it's even on is a sharper illustration of the
same failure than a wall mistaken for a staircase.

Sonnet is not alone in misreading the screen — every model in this sweep does, constantly
— and sonnet does notice, in the moment, when a plan isn't working. Seed0 breaks off its
own "go north" plan at turn 8 to report "it looks like I went into the house again instead
of going north," and at turn 94 — 84 turns into a stall that runs 91 turns in total —
writes "I notice the player seems to be stuck or moving very slowly. Let me try pressing UP
multiple times to push through." Gemini reads a Viridian City frame wrong on the way to
solving S2 and, at turn 89 of that run, also catches itself out loud:

> "Ah! In the last turn, I sent `right, right, up` instead of `right, right, right, right`.
> That's why we are facing up, 1 tile left of the building wall!"

Two turns later, at turn 91, the run reaches `VIRIDIAN_POKECENTER`. Earlier in the same
run, at turn 70, there's a turn with no tool call at all; turn 71 opens with "Oops! I
didn't call the tool in my previous thought block because it was truncated or I wrote too
much." The discriminator
between sonnet and gemini on this harness is not that one notices a stalled plan and the
other doesn't — it's what the noticing produces. Gemini's turn-89 correction names the
exact wrong button sequence it sent and what that implies about where it now stands;
seed0's corrections are undirected — press harder, try the same input again — and never
revise the belief that was actually wrong. That noticing-without-re-diagnosing pattern is
seed0's specifically, evidenced at turns 8 and 94; it is not how the other two S1 seeds
fail. Seed1 declares victory at turn 63 and shows no comparable noticing after that — it
just acts less. Seed2 lands on its wrong-floor belief at turn 2 and holds it to turn 100 —
bar one wilder detour on turn 4 ("I'm now outside in Pallet Town!", still indoors) — with
barely a flicker of doubt: one "the player appears to be stuck," at turn 64, that changes
nothing about what it believes next. One model, one scenario, one harness, three distinct
shapes of the same underlying deficit — that spread is itself worth noting, not smoothed into a single
"sonnet notices X" sentence.

## A fourth scenario, published, that turns the diagnosis into a demonstration

Everything above was written against a three-scenario table. A fourth scenario, S4 —
`scenarios/s4_viridian_mart.yaml`, "walk up to the Viridian City Poké Mart counter and buy
something" — has since been added and scored into `results.json` for real (schema 5, 23
rows, verified by reading the file directly), not run as an informal probe the way the
thinking-budget section above and the HP-drain section below were. Its success predicate is `{map:
VIRIDIAN_MART, money_lte: 2900}`; starting money is $3000, so the predicate cannot fire from
position alone — it requires an actual completed purchase. A scripted, non-LLM oracle solves
it in 10 discrete actions (3 walking steps + a 7-tap buy-confirm macro,
`spike_route:` in the scenario YAML, independently re-verified from
`runs/spike-s4_viridian_mart/20260815-024037/trace.jsonl`: final state `VIRIDIAN_MART (2,5)`
facing left, money 2800), so the task is provably short. Every `meta.json` under
`runs/sweep_s4/` confirms the same frozen contract as the rest of this post: tier 0,
temperature 1.0, `max_output_tokens` 1024, system prompt v2, 10-turn rolling history,
`press_buttons` the only tool — and `cap_turns: 100` in every row, same turn-matching as
S1–S3. (Six models are registered; only five ran S4 — Haiku is simply absent from this sweep,
an open gap, not a decision.)

| model | S4 result | turns (N=3) |
|---|---|---|
| `gpt-5.6-terra` | **3/3** | `[6, 7, 6]` |
| `gemini-3.1-flash-lite` | **3/3** | `[7, 7, 17]` |
| `gemini-3.5-flash` | **3/3** | `[9, 21, 6]` |
| `claude-sonnet-4-6` (reasoning off) | **3/3** | `[11, 10, 11]` |
| `qwen3.5:4b` (local, free) | **0/3** | `[100, 100, 100]` |

Sonnet solved it — every seed, 10 or 11 turns. This is the same model that is 0/9 across
S1–S3, that spent 91 consecutive turns pressing `up` at a staircase it called a gate. Given a
task that requires reading shop dialogue, opening a menu, tracking state across turns, and
confirming a purchase, it did all of that cleanly, three times out of three. That narrows this
post's claim about sonnet by elimination, on this one scenario and this one N=3:
instruction-following, tool use, reading game text, and multi-turn state tracking all look
intact here. What's missing looks narrower than "sonnet can't play this game" — with one limit
worth stating plainly: S4 is a 10-turn task, and sonnet's S1 misreads were already forming by
turn 2. A short scenario doesn't prevent the misread so much as finish before it costs
anything. So what S4 establishes is which layers are intact, not which of vision or
self-correction is the residual.

S4 also does something the three-scenario table couldn't: it separates two models that were
otherwise indistinguishable. `gemini-3.1-flash-lite` and `qwen3.5:4b` are both flat 0/9 on
S1–S3. On S4, gemini-lite is 3/3 and qwen-local is 0/3.

**The claim that must not follow from that, and does not survive checking the traces: that S4
separates them on menu-driving capability.** Reading all 300 of qwen's S4 turns
(`runs/sweep_s4/s4_viridian_mart-qwen-local-t0/*/seed0/run.jsonl`) directly rules it out.
`state.money` reads exactly 3000 on every one of qwen's 300 turns, across all three seeds —
the BUY menu's confirming tap never landed once. And the winning tile-and-facing pose turns
out not to be singular: the scripted oracle and gemini's three wins (plus two of
gemini-lite's) land at `(2,5)` facing left, but gpt's three wins, sonnet's three wins, and
gemini-lite's third win all land at a different tile entirely, `(0,7)` facing up — both are
confirmed genuine purchases (money drops 3000→2800 or 2900 in every case, not a scoring
artifact, most likely two approach tiles onto the same counter object, though the exact
in-game geometry wasn't traced further than that). qwen reaches neither pose correctly, in
any seed: one seed (`20260815-194359`) touches `(2,5)` twice — turn 2 facing up, turn 71
facing down — and moves off within the very next turn both times, never trying a different
facing from the same square; `(0,7)` is never reached at all, in any seed. Money never moves
because the counter is never faced correctly, not because the menu defeats the model. S4
never actually tested menu-driving for qwen — it failed at navigation first, exactly the
S1–S3 failure mode reproducing on a new map.

The sharper comparison is qwen against gemini-lite's third seed, same mistake shape, opposite
outcome. Both park adjacent to the counter and press the wrong way. gemini-lite's seed
(`20260815-031854`) stops at `(0,7)` facing left and presses `a` on six straight turns (4
through 9) with zero effect — money and position both frozen — goes quiet with no tool call
at all on turn 10, and at turn 11 turns to face up and tries again from the identical tile;
the purchase completes at turn 17. qwen never makes that pivot, in either near-miss. Same
mistake, and the discriminator that decides the outcome is the same one the section above —
*What "not solving it" looks like from inside a trace* — already names: self-correction, not
vision.

qwen was not inert while failing this, for what it's worth. Reading the trace screenshots
directly (not `model.text`, which is empty on 300/300 of qwen's S4 turns and 1197/1200 of its
turns project-wide) shows its three seeds covering 8, 11, and 11 tiles between them and
triggering three distinct, legitimate game systems, never the BUY menu: one seed
(`20260815-220512`) opens the START menu and drifts into its own Pokédex, viewing entries up
through Squirtle and beyond for roughly a dozen turns before finding its way back to
gameplay; another (`20260815-194359`) repeatedly bumps an NPC elsewhere in the store into
the line "No! POTIONs are all sold out."; and that same seed presses `down` on the `(3,7)`
interior door mat and warps itself clean out of the Mart into `VIRIDIAN_CITY (29,20)` — gen-1's
door-mat warps only fire when the player is standing on the mat and presses into the tile
beyond, which is exactly what this button press did — before finding its way back in three
turns later.

So the honest reading is narrower than "S4 adds a menu-capability axis": S4 is a shorter,
denser navigation task with a menu component bolted on the end. It discriminates better than
S1–S3 mostly *because* its navigation is easy enough that every model except qwen reaches the
menu at all. Reproduce the whole table in two `score` invocations rather than one (no ROM,
key, or network): `python -m uv run pokebench score $(grep -v '^#' results_traces.txt | grep
-v sweep_s4) --out results.json`, then `python -m uv run pokebench score $(grep -v '^#'
results_traces.txt | grep sweep_s4) --turn-matched --out results.json`. The split is necessary
because S4's invalid seeds are left uncommented for the integrity gate to classify
mechanically rather than curated by omission; `results_traces.txt` explains why in full. The
real sweep behind these five rows needed 25 attempts, not 15 — sonnet alone burned 9 of the
10 exclusions (dollar- and token-cap stops plus a few runs that never finished) chasing 3
valid seeds — at a measured $4.4669 total, summed directly from every attempt's own recorded
cost, valid and excluded alike.

The noticing-without-re-diagnosing distinction settles a design question rather than just
describing sonnet's failure. The obvious next architecture to propose after reading sonnet's
three S1 traces is a planner/executor
split — separate the "what should I do" reasoning from the "did it work" perception. But
sonnet's *plan* was correct on every single turn of every seed — go downstairs, exit the
house, head north — including seed1 and seed2, whose plans never wavered either; what
differed between the three was only which piece of state each one got wrong: whether the
player had actually moved (seed0), whether it had already crossed the tree line (seed1), or
which floor it was even on (seed2). What failed, in all three, was never the planning step;
it was perceiving whether the plan had executed. A planner/executor split adds a second arm on
the side that was already working — and an executor arm carrying the same accumulated context
would be the same model reading the same 480×432 screenshot, misperceiving as the single arm
did. A perception arm invoked *fresh* each turn, with no prior belief to defend, is a genuinely
different proposal, and nothing here tests it.
One fix this evidence points to is a "did my last action change anything" signal — which shades
straight into Tier-1 state feedback, and into the kind of harness-supplied navigational crutch
this project's own non-goals rule out: even the minimal one-bit version hands back ground
truth the model was supposed to derive itself, and the full version is the smarter-nav-tool
shortcut that would stop the benchmark from measuring the model at all. Though "derive
itself" deserves a caveat that cuts against me here: whether you *moved* is a two-frame
question, and the window keeps exactly one frame, replacing every earlier screenshot with a
placeholder. So there is a third option this post hasn't tested and the non-goals don't
obviously forbid — keep the previous frame, still vision-only, no RAM and no nav tool. The
failure mode is real, interesting, and disqualifies the obvious fix; whether it motivates
that quieter one is open.

## A fact the model saw, and the context window dropped

Like the thinking-budget section above, this one isn't from the published table either —
it's an informal, $5.83, two-seed probe run at 500 turns instead of 100 (`--max-turns 500`,
all other caps raised so turns bind), specifically to see whether S3's failures were a
capability ceiling or just a turn-budget shortfall.

Reading gemini's 500-turn Viridian Forest trace surfaced a failure mode none of the
100-turn seeds were long enough to show. At turn 254 the enemy Weedle's last move, Poison
Sting, poisons the player's Pokémon on its way out, HP ticking from 20 to 18 in the same
turn the status effect is set; the Weedle itself doesn't faint until the next turn. HP
shows back at 20 by turn 255 — five turns before the
`in_battle` flag itself clears at turn 260 — and poison resumes ticking once the fight is
actually over. Over the next 26 turns HP drains from 20 to 0 while the model's own
narration is entirely about navigation:

> Turn 265, HP 18: "Excellent! We are in the clear path leading north now."
> Turn 268, HP 16: "Perfect! There is a nice open path heading north right here."

By turn 286 HP reads 0. Turn 287: "Oh no, 'A fainted!' Wait, we must have been poisoned or
in a battle? Ah! Out of battle poison damage?" — the model works out what happened only
after its only Pokémon is already down. Turn 290 is a forced blackout warp back to Viridian
City with the party auto-healed to full — a different map entirely from Viridian Forest,
where the poisoning happened; the model then spends roughly 106 turns re-navigating back
toward the forest, reaching the south gate at turn 396 and stepping back inside by turn
398 — over a fifth of the 500-turn probe budget spent recovering from a mistake whose
consequences it never tracked, with roughly 100 turns still left when it finally gets back in.

This is a second and distinct failure mode from sonnet's self-reinforcing misread: the model
was navigating competently, by its own account correctly, while a status effect quietly
killed its only party member. But the tempting reading — "the model doesn't watch its own
party state" — is too quick on this trace, and the harness is implicated more deeply than
that. It was reading the move that did it: in the turns just before the status lands, its
narration at 249 and 251 transcribes `Enemy WEEDLE used POISON STING!`, and back at turn 146
it had already puzzled over an ambiguous `Poi...` on screen before resolving it, wrongly, as
"Points". Then, between turn 252 and the faint at 287, the word never appears in its text
again. At Tier-0 the ten-turn rolling window is the only memory that exists — the notes
scratchpad is a Tier-1 tool, unavailable here — so those observations aged out of context
around turn 261, some 25 turns before the drain became fatal.

One detail keeps this from being a clean harness artifact: HP and status are re-readable at
any turn from the same START menu this post shows another model opening unprompted. The
harness made the fact hard to
retain. It never made it unavailable. So what is left is a model deficit of an uncertain
kind — failing to hold what it had read, or failing to think to go and re-read it — sitting on
top of a harness that made the first of those considerably easier. One seed of one informal
probe cannot separate them, and this one doesn't.

## The retraction

This project published an internal claim that later turned out to be wrong, and the honest
way to write it up is to walk through how, not to quietly drop it.

Viridian Forest — the scenario nobody's model has solved — was probed with a scripted,
non-LLM route-finder to answer a narrower question: does an exit even exist that a script
could find? An early pass reported yes: a north gate at a specific tile, reachable in a
verified minimum path, with the "no exit found" question closed. A second pass, re-running
the same search with a more careful walker, could not reproduce it.

The mechanism was a timing bug in how the probe checked for battles. The script advanced
one tile at a time and checked an in-battle flag once after each move. For an ordinary wild
encounter this is enough — the flag sets on the same call that freezes the player's
position. But a trainer's sight-trigger doesn't work the same way: the "!" indicator and the
forced walk-up dialogue take several more move-attempts to resolve into the in-battle flag
actually being set, and until it sets, the player's position looks exactly like it's stuck
against a wall — indistinguishable, from the outside, from a genuine dead end. The original
probe's route to the "north gate" started from a tile the script had recorded as a wall.
It wasn't a wall. It was a trainer that hadn't finished noticing the player yet, misread by
a check that only looked once.

With the walker corrected to retry through that multi-step trainer-sighting window instead
of giving up after one check, an exhaustive search of the entire region reachable from the
scenario's starting point — 668 tiles, fully mapped — found no exit at all. But the
corrected search has its own honest limit: it flees ordinary wild encounters and cannot
fight a trainer battle, and it ran into three trainers it could not get past along the way.
So the true, narrower finding is not "the forest has an exit" (the retracted claim) and it
is not "the forest has no exit" either — that would overcorrect past what was actually
shown. What's established is: **no exit is reachable without winning a battle that this
probe was not built to fight.** Whether an exit exists behind one of those three trainers is
still an open question. Given that Viridian Forest's scenario anchor is a single Pokémon
with no confirmed items — the same fragile starting state the poisoning probe above showed
can be killed mid-run — a live, still-untested hypothesis is that S3's 0/15 reflects an
unavoidable combat requirement layered onto a starting state that makes combat itself
risky. That is a hypothesis, not a finding, and it stays that way until it's tested.

## What this implies for agent evals generally

The pattern across every section above is the same shape: a decision made explicitly to
produce a fair, harness-neutral measurement — a shared output ceiling, a shared screenshot
format, a single check-once validity rule — turns out, on inspection, to not be neutral at
all, or to be neutral in a way that hides a different problem. The output ceiling penalized
one vendor's deliberation. A validity check built to catch truncated runs would, if
applied too bluntly, have deleted a legitimate result — a model that stopped acting because
it believed, wrongly, that it had won — while catching the real truncation artifacts it was
aimed at, whose dead-turn rates run 31–42% against that seed's 16%. The cost of the blunt
rule is false positives, not misses: it would have thrown away a finding to catch what a
narrower rule catches anyway. The
integrity gate meant to keep mismatched trace types out of one scored row shipped with a
blind spot of its own — it would silently average a 500-turn probe seed together with
100-turn table seeds into one median, with the mismatch visible only as a stray `500` in a
`seed_values` list, until that specific failure mode was tested for directly and closed
two days later, in the commit that added
`test_result_row_rejects_the_whole_group_on_cap_turns_disagreement`. Even a scripted, non-LLM
probe — the thing built specifically to be a ground-truth check *against* the harness — had
its own measurement artifact baked into how it checked for battles, and produced a false
positive as a result.

None of this is an argument against measurement. It's an argument that a fixed harness
doesn't buy you a neutral one for free — the work isn't building the harness once and
trusting it, it's continuing to interrogate the harness with the same skepticism you'd
apply to the model. Every constant picked to keep things fair is a design decision with
its own failure modes, and some of those only show up after a table has already been
published on top of them. "The harness giveth" cuts both ways: it gives you the ability to
compare five models on equal footing, and it gives every one of them a specific, sometimes
hidden way the harness itself shaped what got measured. Reporting the
second half honestly is not a caveat on the result. On this project's evidence so far, it's
most of the result.

---

Full traces, scoring code, and the exact seed directories behind every number above are at
[github.com/Biabuyan/pokebench](https://github.com/Biabuyan/pokebench); the live table is
at [pokebench-snowy.vercel.app](https://pokebench-snowy.vercel.app).
