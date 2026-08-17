# Save states (local only)

Anchor save states are recorded locally with `uv run pokebench play` and are
**not tracked by git** — they are derived from your own ROM/playthrough, and
everything in this directory except this README is git-ignored (`.gitignore`
has the pattern). The original plan (`PLAN.md`) considered shipping save-state
*deltas*; the decision actually made was simpler and is what's implemented:
ship nothing derived from the ROM, and reproduce each anchor locally with
`pokebench play` instead — the same "bring your own ROM" stance the repo
takes everywhere else.

Expected files:

- `s1_bedroom.state` — REDS_HOUSE_2F, starter obtained (party >= 1). Anchor for S1.
- `s2_route1.state` — ROUTE_1, anchor for S2 (reach Viridian Pokécenter).
- `s3_forest.state` — VIRIDIAN_FOREST, anchor for S3 (traverse the forest).
- `s4_mart_unlocked.state` — VIRIDIAN_MART interior (3,7), Oak's Parcel
  already delivered. Anchor for S4 (Mart purchase). **Not captured with
  `pokebench play`** like the other three — see
  `scenarios/s4_viridian_mart.yaml`'s header for why (a mandatory,
  un-skippable gen-1 cutscene + soft-lock makes a fresh first-visit save
  state unusable as this scenario's anchor) and the exact scripted
  reproduction path.
