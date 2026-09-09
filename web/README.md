# Leaderboard + trace viewer (M4)

**Live at https://pokebench-snowy.vercel.app** — publicly reachable, no auth wall.
Vercel project `pokebench` (org `jia-yuans-projects-808a6406`, stable alias
`pokebench-snowy.vercel.app`).

Static site generated from `results.json` (the M2/M3 metrics pipeline's committed
artifact) plus `results_traces.txt` (which run directories produced each row, and the
curation commentary explaining what was excluded and why). No screenshots ship here —
that is a settled decision, not a placeholder: publishing thousands of frames of a
commercial ROM is a different act from the repo's existing "bring your own
hash-verified ROM, we ship nothing" stance, so Stage 1/2 publish zero Pokémon Red
frames. A replay page tells the reader to run `pokebench watch <run-dir>` locally,
against their own ROM, to see a run's screen alongside its reasoning.

Build it:

```sh
python -m uv run pokebench site build --results results.json --out web/dist
```

Offline — no ROM, no network, no API key. It reads `results.json` (committed) and, if
present, walks the trace directories `results_traces.txt` lists (`runs/` is
gitignored/local-only, so a fresh clone without local traces still gets a working
leaderboard; it just has no replay links). Output lands in `web/dist/`, which is
itself gitignored — generated, not source, the same relationship `runs/` has to
`results.json`. Rebuild it whenever `results.json` or `results_traces.txt` changes;
never commit the output.

## `leaderboard-receipt.json`

Alongside `index.html`, the build writes `web/dist/leaderboard-receipt.json` — the same
turn-cap split the page states in prose, as JSON, so a second page, a bot, or a reader
checking the table does not have to scrape the HTML or re-derive `site.py`'s logic
first (raised as [issue #1](https://github.com/Biabuyan/pokebench/issues/1)).

It is a receipt for exactly one file. Every field is derivable from `results.json`
alone, and `results_sha256` is the hash of that file's raw bytes, so it can be checked
directly:

```sh
sha256sum results.json          # matches the receipt's results_sha256
```

`results_traces.txt`'s curated exclusion counts are deliberately *not* in it, even
though the builder has them in hand — they are a different unit from `rows[].exclusions`
and live in a file that hash does not cover, so including them would make the hash look
like it vouches for numbers it never saw.

Two fields that are easy to conflate and are kept apart on purpose:

- `off_cap_models` — `haiku`, whose rows are the earlier fixed-turn-budget milestone
  (`cap_turns` 300/400/600, one per scenario). These rows **are** in the artifact; they
  are excluded from the head-to-head count only. Each cap keeps its own `turn_caps`
  bucket rather than being flattened into one off-cap number no row carries.
- `seed_exclusions` — seeds the eval-integrity gate (`metrics/validity.py`) rejected as
  evidence. These are **not** in the artifact at all.

`summary_line` is the page's own headline sentence verbatim: both it and the receipt
read one `_cap_split()` in `site.py`, so the JSON cannot drift from the HTML it
restates. `tests/test_site.py` pins that.

**What `seed_exclusions` does not tell you.** It counts what `results.json` records,
which is not the same as counting every attempt the sweep made. `pokebench sweep
--resume` reconstructs only the *valid* seeds of a cell it has already run, so a cell
that needed several `--resume` invocations keeps only the last invocation's exclusion
list — earlier rejected attempts are real, and are visible in the trace directories
under `runs/`, but never reach `results.json`. That gap is open, not fixed, and it is
invisible from `results.json` alone, so the receipt ships the count with a `caveat`
string rather than a bare number. Treat `seed_exclusions` as a floor.

**Stack: Python stdlib only** (`src/pokebench/site.py` — `html.escape`, f-strings, no
Jinja2, no framework, no build step), matching `viewer.py`'s established pattern. The
dependency set in `pyproject.toml` does not grow for this.

## Deploying

**Deploys are manual by design, not an unfinished step.** Auto-deploy would mean either
committing `web/dist/` (8.8 MB of generated output, against the same principle that
keeps `runs/` gitignored and only `results.json` committed) or standing up a CI job
holding Vercel credentials, which does not exist. Rebuild and redeploy by hand whenever
`results.json` changes:

```sh
python -m uv run pokebench site build --results results.json --out web/dist
cd web/dist
vercel --prod --archive=tgz
cd ../..
python -m uv run pokebench site verify --url https://pokebench-snowy.vercel.app
```

Every step here was learned the expensive way — do not simplify this back down:

1. **The build must run locally.** `runs/` is gitignored, so a Vercel- or
   GitHub-connected build has no traces to read and would publish 54 dead replay links.
   A git-connected Vercel project was tried first and cannot work at all: it failed with
   "No python entrypoint found" because Vercel auto-detected `pyproject.toml` and
   assumed a Python serverless app, not a static site.
2. **`cd web/dist` before running `vercel`.** This step has failed in two different
   ways, and the second one is the dangerous one.
   - *Loudly*, the first time: from the repo root with no `.gitignore` coverage,
     `vercel` uploaded the whole working directory — 211 MB, including `.venv` and
     `runs/`'s 8,040 files — instead of the site's ~315 files / 11 MB.
   - *Silently*, on **2026-08-18**: the repo-root `.gitignore` now has `web/dist/`
     (line 18), and the Vercel CLI applies the deploy root's `.gitignore`. So a
     repo-root deploy strips the **one directory that holds the site**, uploads the
     source tree in its place, and reports `● Ready` in 1s with no warning. The
     leaderboard served a bare 404 for **22 days** before anyone noticed, while
     `/pyproject.toml`, `/src/pokebench/cli.py`, `/uv.lock` and three untracked
     working JSONs (`results_s4.json`, `calib_v2_n3.json`, `tier1_smoke.json`) were
     publicly served in its place. Nothing leaked that `.gitignore` protects
     (`CLAUDE.md`, `HANDOFF.md`, `.claude/`, `roms/`, `runs/`, `.env*` all 404'd), but
     three files that are not part of the public artifact were live for three weeks.

   Do not rely on reading the deploy output to catch this — it looks identical to a
   good deploy. That is what step 5 is for.
3. **`--archive=tgz` is load-bearing, not a style choice.** It sends one tarball instead
   of ~315 individual files. The repo-root attempts exhausted Vercel's free-tier daily
   upload quota (`api-upload-free`, "more than 5000, try again in 1 day"), which
   surfaced only as opaque `Upload aborted` errors with no obvious connection to quota.
4. **Answer "n" to "Pull development environment variables into .env.local?"**
   Answering "y" once wrote a live `VERCEL_OIDC_TOKEN` into `web/dist` — the publish
   directory — where a deploy would have served it at a public URL. It was caught and
   deleted before any successful deploy and was never committed (`.env*` is
   gitignored), so nothing leaked. A static site has no runtime and needs no env vars;
   the prompt should always be declined here.
5. **Run `pokebench site verify` after every deploy — the deploy's own exit code does
   not mean the site is up.** It fetches the served bytes back and asserts both halves
   of the 2026-08-18 outage: `/` returns 200 *and is byte-identical* to the local
   `index.html` (a stale deploy answers 200 and looks healthy), every replay page
   resolves, and none of `MUST_NOT_PUBLISH` (`pyproject.toml`, `uv.lock`,
   `src/pokebench/cli.py`, `results_traces.txt`, `CLAUDE.md`, `HANDOFF.md`, `.env`) is
   reachable. It exits non-zero on any mismatch, so it can be chained onto the deploy.
   This is the only command in the CLI that makes a network call; it still needs no ROM
   and no API key, and its tests inject a fake fetcher (`tests/test_site_verify.py`)
   so the suite keeps its no-network rule.

The repo itself is public, so there is deliberately no GitHub Pages workflow either:
Pages is one more moving part for no benefit when Vercel already serves the static
output with a one-command deploy.
