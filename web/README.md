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
```

Every step here was learned the expensive way — do not simplify this back down:

1. **The build must run locally.** `runs/` is gitignored, so a Vercel- or
   GitHub-connected build has no traces to read and would publish 54 dead replay links.
   A git-connected Vercel project was tried first and cannot work at all: it failed with
   "No python entrypoint found" because Vercel auto-detected `pyproject.toml` and
   assumed a Python serverless app, not a static site.
2. **`cd web/dist` before running `vercel`.** From the repo root, `vercel` uploads the
   whole working directory — 211 MB, including `.venv` and `runs/`'s 8,040 files —
   instead of the site's 218 files / 8.8 MB.
3. **`--archive=tgz` is load-bearing, not a style choice.** It sends one tarball instead
   of 218 individual files. The repo-root attempts exhausted Vercel's free-tier daily
   upload quota (`api-upload-free`, "more than 5000, try again in 1 day"), which
   surfaced only as opaque `Upload aborted` errors with no obvious connection to quota.
4. **Answer "n" to "Pull development environment variables into .env.local?"**
   Answering "y" once wrote a live `VERCEL_OIDC_TOKEN` into `web/dist` — the publish
   directory — where a deploy would have served it at a public URL. It was caught and
   deleted before any successful deploy and was never committed (`.env*` is
   gitignored), so nothing leaked. A static site has no runtime and needs no env vars;
   the prompt should always be declined here.

The repo itself is public, so there is deliberately no GitHub Pages workflow either:
Pages is one more moving part for no benefit when Vercel already serves the static
output with a one-command deploy.
