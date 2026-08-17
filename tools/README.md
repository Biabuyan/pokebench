# tools/

One-off research probes: scripts that answer a specific geometry/mechanics
question against the real ROM, outside the `pokebench` CLI, because the
question is exploratory rather than something an agent (or the benchmark
harness) ever needs at run time. They are **kept and committed** — see
`probe_forest_route.py`'s own module docstring for the precedent this
directory follows: a previous session found the Viridian Forest route with a
throwaway script, deleted it after use, and the *fact* it found outlived the
*evidence* for it. A later, from-scratch re-run (`probe_forest_route.py` in
this directory) could not reproduce the claim at all: an exhaustive BFS over
every tile reachable from the S3 anchor found no exit from Viridian Forest,
only three confirmed unfleeable trainer encounters that a cruder single-shot
"blocked" check had misrecorded as walls. Don't repeat that: if a probe
answers a question worth recording, keep the probe.

**What's committed:** the scripts themselves (`probe_forest_route.py` and any
future `probe_*.py`), each with a docstring stating what it does, how to
re-run it, and (if it hardcodes an observed game mechanic, like the RUN-menu
button macro) how that mechanic was originally derived.

**What's gitignored (`.gitignore` has the patterns):**
- `_probe_screens/` — screenshots a probe saves for visual sanity-checking.
  Regenerable by re-running the probe; not evidence on its own.
- `_*.json` / `*_probe_result.json` — scratch debug dumps and discovered-route
  output. **These are NOT the durable record of a finding.** A route a probe
  discovers belongs in a scenario YAML's `spike_route:` block (data); a
  geometry fact belongs in `tests/fake_world.py` plus a regression test
  (code); the finding itself, with how it was derived and any caveats,
  belongs in `HANDOFF.md` — a local, gitignored working-session record kept
  on the build machine, not part of a public clone of this repo, so this is
  guidance for whoever is maintaining the project rather than a citation a
  reader can follow. An untracked JSON blob with no author, no docstring, and no test
  pinning it is not verifiable by a future session and should not be treated
  as evidence — exactly what went wrong with `tools/_debug_edges.json`
  (produced by an earlier, terminated session mid-debugging and left on
  disk). Live re-verification found it contained at least one wrong entry: it
  recorded all four directions out of `VIRIDIAN_FOREST (1,18)` as blocked,
  but `up` and `down` actually both lead into the same unfleeable trainer, not
  a wall — a race between reading player position and a movement finishing
  had let the trainer sighting get misrecorded as a wall. It was deleted
  rather than kept, and the one fact it had corroborated (a separate, genuine
  26-column wall run) was independently re-confirmed live and recorded in the
  durable homes above instead. If a probe's raw output is worth keeping past
  the session that produced it, promote it into one of the three durable
  homes above, don't leave it as a loose file in this directory.

**Cost discipline:** every probe here must run against the local ROM only —
no network, no API key, $0 by construction (see each script's own docstring
for how it stays that way; typically by driving `Emulator`/`EncounterAwareWalker`
directly, never an `Agent`) — **with one stated exception:**
`probe_thinking_budget.py` (2026-08-14) makes exactly one live call to the real
Anthropic API (~$0.01–0.05) to confirm a request/response wire shape
(`thinking: {"type": "enabled", "budget_tokens": N}` + the `max_tokens`
relationship it requires) before any adapter code was written to depend on it —
a question this directory's usual $0-by-construction probes cannot answer,
since it is about the live API contract, not ROM geometry. Its own docstring
states the exception and the exact budget; kept per the same "don't let a
finding become unfalsifiable folklore" rule as every other probe here, not
re-run casually.
