"""PokéBench CLI.

  pokebench play       interactive play + auto save-state capture
  pokebench inspect    print structured state (and screenshot) from a save state
  pokebench spike-s1   M0 acceptance: hardcoded (non-LLM) route exits Pallet Town
  pokebench run        one model attempts one scenario, traced
  pokebench bench      N seeds of one config -> results.json
  pokebench sweep      a matrix of configs, driven to N *valid* seeds each
  pokebench score      score traces offline, with the eval-integrity gate
  pokebench replay     inspect a completed run trace
  pokebench watch      live read-only dual-pane viewer (screen + reasoning)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import replace
from pathlib import Path

DEFAULT_ROM = Path(os.environ.get("POKEBENCH_ROM", "roms/pokemon_red.gb"))


def _add_rom_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--rom",
        type=Path,
        default=DEFAULT_ROM,
        help=f"path to your Pokémon Red ROM (default: {DEFAULT_ROM}, env POKEBENCH_ROM)",
    )


def cmd_play(args: argparse.Namespace) -> int:
    from pokebench.recorder import play

    play(
        rom=args.rom,
        state=args.state,
        capture_map=None if args.no_capture else args.capture,
        min_party=args.min_party,
        out=args.out,
    )
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    from pokebench.harness.emulator import Emulator

    emu = Emulator(args.rom, window=False)
    emu.load_state(args.state)
    emu.settle(30)
    state = emu.state()
    print(json.dumps(state.to_dict(), indent=2))
    if args.png:
        emu.screenshot().save(args.png)
        print(f"screenshot -> {args.png}", file=sys.stderr)
    emu.close()
    return 0


def _spike_out_base(out_dir_arg: Path | None, scenario_id: str, timestamp: str) -> Path:
    """Default `spike-s1` out-dir, derived from the scenario id.

    `cmd_spike_s1` is scenario-agnostic (any scenario with a `spike_route`
    block runs), but until now `--out-dir` defaulted unconditionally to the
    literal `runs/spike-s1` -- so an S3 (or any non-S1) spike run would
    silently write its trace into a directory named for a different scenario,
    colliding with S1's own traces. An explicit `--out-dir` always wins.
    """
    base = out_dir_arg if out_dir_arg is not None else Path(f"runs/spike-{scenario_id}")
    return base / timestamp


def _spike_verdict(scenario: dict, final: object) -> bool:
    """Whether a spike-s1 oracle run actually satisfies the scenario's full
    `success:` block -- not just its `map` clause (S4, 2026-08-15). The old
    inline check (`final.map_name == scenario["success"]["map"]`) silently
    ignored any other clause (e.g. S4's `money_lte`), so a route that walked
    into the right map WITHOUT ever completing the purchase would still print
    PASS -- exactly the "unverified predicate" failure mode CLAUDE.md warns
    against ("do not ship a scenario whose predicate has never fired").
    Reuses `make_success_check` (the same predicate the real agent loop is
    scored against) rather than re-deriving verdict logic a second time.
    """
    from pokebench.runner.loop import make_success_check

    return make_success_check(scenario["success"])(final)


def cmd_spike_s1(args: argparse.Namespace) -> int:
    import yaml

    from pokebench.harness.emulator import Emulator
    from pokebench.harness.navigate import (
        EncounterAwareWalker,
        RouteError,
        WildRunEscaper,
        run_route,
    )
    from pokebench.harness.trace import TracingWalker

    scenario = yaml.safe_load(args.scenario.read_text(encoding="utf-8"))
    state_file = args.state or (args.scenario.parent / scenario["anchor"]["state_file"])
    if not Path(state_file).exists():
        print(
            f"FAIL: anchor state {state_file} not found.\n"
            "Record it first: `uv run pokebench play` and follow README step 4.",
            file=sys.stderr,
        )
        return 2

    out_dir = _spike_out_base(args.out_dir, scenario["id"], time.strftime("%Y%m%d-%H%M%S"))
    emu = Emulator(args.rom, window=args.window, speed=1 if args.window else 0)
    emu.load_state(state_file)
    emu.settle(120)

    start = emu.state()
    print(f"anchor: {start.summary()}")
    req = scenario["anchor"].get("requirements", {})
    if "map" in req and start.map_name != req["map"]:
        print(f"warning: anchor map is {start.map_name}, expected {req['map']} (continuing)")
    if start.party_count < req.get("min_party_count", 0):
        print(
            f"warning: party={start.party_count} < {req['min_party_count']} — "
            "did you save the state before getting your starter? Oak will block Route 1."
        )

    # TracingWalker sits directly on the emulator (not outside
    # EncounterAwareWalker) so that its own dpad()/tap() calls -- including
    # every escape attempt EncounterAwareWalker makes internally, which never
    # reach a wrapper placed OUTSIDE it -- all land in the same trace.jsonl,
    # in real call order (see trace.py's TracingWalker docstring).
    tracer = TracingWalker(emu, out_dir)
    # A real RUN macro, not the class's tap("a") placeholder: S1's route never
    # crosses wild-encounter grass so this was never exercised, but S2's
    # Route 1 leg does (found live 2026-08-11 -- see WildRunEscaper's
    # docstring). Gen-1 trainer battles still cannot be fled either way;
    # EncounterAwareWalker raises RouteError loudly on that, by design.
    walker = EncounterAwareWalker(tracer, escape=WildRunEscaper())
    t0 = time.time()
    try:
        final = run_route(walker, scenario["spike_route"], log=print)
    except RouteError as e:
        emu.screenshot().save(out_dir / "final.png")
        print(f"\nFAIL: {e}\ntrace + screenshots: {out_dir}")
        emu.close()
        return 1
    finally:
        tracer.close()

    emu.screenshot().save(out_dir / "final.png")
    ok = _spike_verdict(scenario, final)
    verdict = "PASS" if ok else "FAIL"
    print(
        f"\n{verdict}: ended at {final.summary()} against success={scenario['success']!r} "
        f"in {tracer.steps} steps, {time.time() - t0:.1f}s wall clock"
    )
    print(f"trace + screenshots: {out_dir}")
    emu.close()
    return 0 if ok else 1


def _caps_from_scenario(
    scenario: dict, max_turns=None, max_usd=None, max_tokens=None, max_minutes=None
):
    """Scenario caps, with optional per-run overrides.

    The overrides exist for M3's **turn-matched** cross-model comparison: every
    model must get the same number of turns, so the token / dollar / wall-clock
    caps have to be lifted out of the way and left as pure kill switches. They
    are overrides rather than YAML edits on purpose — the YAML values define
    what M2 measured, and rewriting them would retroactively change the meaning
    of an already-published results table.
    """
    from pokebench.runner.loop import Caps

    cs = scenario.get("caps", {})
    return Caps(
        max_turns=max_turns or cs.get("agent_turns", 300),
        max_tokens=max_tokens if max_tokens is not None else cs.get("max_tokens"),
        max_usd=max_usd if max_usd is not None else cs.get("max_usd"),
        max_wall_seconds=(max_minutes or cs.get("wall_clock_minutes", 30)) * 60,
    )


def _apply_cli_overrides(config, reasoning_arg: str, thinking_budget_arg: int | None):
    """`--reasoning off` / `--thinking-budget N` for `bench`/`sweep`.

    `--reasoning off` fixes a live trap, not a policy change: no `MODELS`
    entry sets `reasoning=False` explicitly (including sonnet), so every model
    inherits the dataclass default `reasoning=True`. Before this existed,
    `pokebench sweep --model sonnet` would silently run with adaptive thinking
    on, reproducing the 39%/37% ceiling-hit/no-tool-call pathology (CLAUDE.md
    "Two live caveats" #1) inside the sweep's own retry loop -- burning money
    on a broken leg with no CLI opt-out.

    `--thinking-budget N` (Stage 2, 2026-08-14) is the CLI handle for
    `ModelConfig.thinking_budget_tokens`, which Stage 1 landed but nothing
    could set: the registry gives every model `None` (adaptive thinking, or no
    thinking at all if `reasoning=False`), and `agents/anthropic.py` already
    reads the field -- it just had no way to become anything but `None`.

    Both are no-ops at their CLI defaults (`"default"` / `None`), so the
    documented registry policy stays intact unless a flag is explicitly
    passed; `dataclasses.replace` keeps every other field untouched. Callers
    must apply this before the FIRST `make_agent` call, not just the one
    inside the seed loop -- the fail-fast provider check also constructs an
    agent.
    """
    changes = {}
    if reasoning_arg == "off":
        changes["reasoning"] = False
    if thinking_budget_arg is not None:
        changes["thinking_budget_tokens"] = thinking_budget_arg
    return replace(config, **changes) if changes else config


def cmd_run(args: argparse.Namespace) -> int:
    import yaml

    from pokebench.agents.base import get_model, make_agent
    from pokebench.harness.emulator import Emulator
    from pokebench.harness.trace import RunTracer
    from pokebench.runner.loop import make_success_check, run_episode

    scenario = yaml.safe_load(args.scenario.read_text(encoding="utf-8"))
    state_file = args.state or (args.scenario.parent / scenario["anchor"]["state_file"])
    if not Path(state_file).exists():
        print(
            f"FAIL: anchor state {state_file} not found.\n"
            "Record it first: `uv run pokebench play` and follow README step 4.",
            file=sys.stderr,
        )
        return 2

    config = get_model(args.model)
    try:
        agent = make_agent(config)
    except KeyError as e:
        print(f"FAIL: {e}", file=sys.stderr)
        return 2

    # Caps: scenario declares them; CLI may tighten for a quick smoke run.
    caps = _caps_from_scenario(
        scenario, args.max_turns, args.max_usd, args.max_tokens, args.max_minutes
    )

    # Build the emulator first, so a missing/bad ROM fails before we create an
    # (empty) trace directory.
    emu = Emulator(args.rom, window=False)
    emu.load_state(state_file)
    emu.settle(120)
    start = emu.state()
    print(f"anchor: {start.summary()}")
    req = scenario["anchor"].get("requirements", {})
    if start.party_count < req.get("min_party_count", 0):
        print(
            f"warning: party={start.party_count} < {req['min_party_count']} — "
            "state may predate the starter; Oak will block Route 1."
        )

    out_dir = args.out_dir / time.strftime("%Y%m%d-%H%M%S")
    tracer = RunTracer(out_dir, screenshots=not args.no_screenshots, save_raw=args.save_raw)

    print(f"running {scenario['id']} | model={args.model} tier={args.tier}")
    try:
        result = run_episode(
            emu,
            agent,
            objective=scenario["objective"],
            success=make_success_check(scenario["success"]),
            caps=caps,
            tracer=tracer,
            tier=args.tier,
            scenario_id=scenario.get("id"),
            log=print,
        )
    finally:
        tracer.close()
        emu.close()

    tracer.finish(result.summary_dict())
    verdict = "SUCCESS" if result.success else f"STOPPED ({result.stop_reason})"
    print(
        f"\n{verdict}: {result.turns} turns | "
        f"{result.input_tokens}+{result.output_tokens} tokens | "
        f"${result.usd:.4f} | {result.wall_seconds:.1f}s"
    )
    print(f"final: {result.final_state.summary()}")
    print(f"trace: {out_dir}")
    return 0 if result.success else 1


def cmd_score(args: argparse.Namespace) -> int:
    """Score existing run traces into metrics (offline — no ROM/key).

    Every scored run is also checked by `metrics/validity.py`. Invalid seeds are
    reported and, by default, kept out of `results.json` — the point of the gate
    is that a run which measured the harness never silently becomes a
    leaderboard row. `--include-invalid` restores the pre-2026-08-04 behaviour
    for auditing what changed.
    """
    from collections import defaultdict

    from pokebench.metrics import check_validity, result_row, score_run, write_results
    from pokebench.replay import load_run

    scored = []
    for d in args.run_dirs:
        if not Path(d).exists():
            print(f"skip: {d} not found", file=sys.stderr)
            continue
        run = load_run(d)
        m = score_run(run)
        v = check_validity(run, m, expect_turn_matched=args.turn_matched)
        scored.append((d, m, v))
        steps = m.steps_to_success if m.steps_to_success is not None else "-"
        flag = "" if v.valid else f"  [INVALID: {v.reason}]"
        print(
            f"{d}: {m.scenario}/{m.model}/tier{m.tier} success={m.success} "
            f"turns={m.turns} steps={steps} tokens={m.input_tokens}+{m.output_tokens} "
            f"${m.cost_usd} stuck={m.stuck_index} idle={m.idle_rate} "
            f"tiles={m.tiles_explored} invalid={m.invalid_rate} "
            f"dead={m.no_tool_call_rate} ceiling={m.ceiling_hit_rate}{flag}"
        )
        if not v.valid:
            print(f"    -> {v.detail}", file=sys.stderr)
        elif "volitional_stop" in v.evidence:
            # Kept, but never passed over in silence: this is the seed1 case.
            print(f"    note: {v.evidence['volitional_stop']}")

    if not scored:
        print("no runs scored", file=sys.stderr)
        return 2

    invalid = [(d, m, v) for d, m, v in scored if not v.valid]
    if invalid:
        print(f"\n{len(invalid)} of {len(scored)} run(s) failed the eval-integrity check:")
        for d, _m, v in invalid:
            print(f"  {d}: {v.reason} — {v.detail}")
        if args.include_invalid:
            print("  (--include-invalid: counting them anyway)")

    groups = defaultdict(lambda: ([], []))
    for _d, m, v in scored:
        keep, drop = groups[(m.model, m.scenario, m.tier)]
        (keep if (v.valid or args.include_invalid) else drop).append(
            m if (v.valid or args.include_invalid) else (m, v)
        )
    rows = [result_row(keep, drop) for keep, drop in groups.values()]

    # `result_row` rejects a whole (model, scenario, tier) group outright when
    # its seeds ran under different turn budgets (`cap_turns_disagreement`) --
    # the one gate specifically built to stop a mixed-cap sweep from silently
    # blending incomparable medians. Previously this only surfaced inside the
    # JSON written by `--out`; a plain `pokebench score` printed every seed as
    # if valid and the rejection was discoverable only by opening the file
    # afterward. Print it here, unconditionally, so a human notices it live.
    for row in rows:
        conflicts = [e for e in row["exclusions"] if e["reason"] == "cap_turns_disagreement"]
        if conflicts:
            seen = conflicts[0]["evidence"].get("cap_turns_seen")
            print(
                f"\nCAP-TURNS CONFLICT [{row['model']}/{row['scenario']}/tier{row['tier']}]: "
                f"seeds ran under different turn budgets {seen} -- the whole group was "
                "rejected, not just a minority (score matching --max-turns runs together, "
                "or score them separately)",
                file=sys.stderr,
            )

    if args.out:
        write_results(args.out, rows, generated=time.strftime("%Y-%m-%dT%H:%M:%S"))
        print(f"wrote {len(rows)} row(s) -> {args.out}")
    return 0


def cmd_sweep(args: argparse.Namespace) -> int:
    """Drive a matrix of configs to N *valid* seeds each (needs ROM + key)."""
    import yaml

    from pokebench.agents.base import get_model, make_agent
    from pokebench.harness.emulator import Emulator
    from pokebench.metrics import result_row, score_run, write_results
    from pokebench.replay import load_run
    from pokebench.runner.bench import run_seeds
    from pokebench.runner.loop import make_success_check
    from pokebench.runner.sweep import (
        Cell,
        SweepCaps,
        reload_excluded_metrics,
        reload_valid_metrics,
        run_sweep,
    )

    scenarios = {}
    for path in args.scenario:
        spec = yaml.safe_load(path.read_text(encoding="utf-8"))
        state_file = path.parent / spec["anchor"]["state_file"]
        if not Path(state_file).exists():
            print(f"FAIL: anchor state {state_file} not found (record via `pokebench play`).",
                  file=sys.stderr)
            return 2
        scenarios[spec["id"]] = (spec, state_file, path)

    cells = [
        Cell(sid, model, args.tier)
        for sid in scenarios
        for model in args.model
    ]
    for model in args.model:
        try:
            # fail fast before any seed runs
            make_agent(
                _apply_cli_overrides(get_model(model), args.reasoning, args.thinking_budget)
            )
        except KeyError as e:
            print(f"FAIL: {e}", file=sys.stderr)
            return 2

    # Reload from the trace directories, not from results.json: a published row's
    # `seed_values` only carries six of RunMetrics' fields, so it cannot reconstruct
    # a real RunMetrics per prior seed -- see runner/sweep.py's resume-bug fix
    # (2026-08-06). This also means resume works even if results.json was deleted
    # or moved, as long as `out_dir` still holds the traces.
    #
    # Both halves -- valid AND excluded -- must be reloaded, or a cell resumed
    # across two invocations silently loses the first invocation's exclusions the
    # moment the second's row overwrites it in results.json (2026-08-16 fix; see
    # runner/sweep.py's module docstring).
    already: dict = {}
    already_excluded: dict = {}
    if args.resume:
        already = reload_valid_metrics(
            args.out_dir, cells, expect_turn_matched=not args.fixed_budget
        )
        already_excluded = reload_excluded_metrics(
            args.out_dir, cells, expect_turn_matched=not args.fixed_budget
        )

    def run_cell(cell: Cell, attempt: int):
        spec, state_file, _path = scenarios[cell.scenario]
        config = _apply_cli_overrides(get_model(cell.model), args.reasoning, args.thinking_budget)
        caps = _caps_from_scenario(
            spec, args.max_turns, args.max_usd, args.max_tokens, args.max_minutes
        )

        def make_env():
            emu = Emulator(args.rom, window=False)
            emu.load_state(state_file)
            emu.settle(120)
            return emu

        out_base = args.out_dir / cell.label / time.strftime("%Y%m%d-%H%M%S")
        run_seeds(
            make_env,
            lambda: make_agent(config),
            objective=spec["objective"],
            success=make_success_check(spec["success"]),
            caps=caps,
            out_base=out_base,
            seeds=1,
            tier=cell.tier,
            scenario_id=cell.scenario,
            save_raw=args.save_raw,
            log=print,
        )
        run = load_run(out_base / "seed0")
        return run, score_run(run)

    report = run_sweep(
        cells,
        run_cell,
        seeds=args.seeds,
        max_retries=args.max_retries,
        caps=SweepCaps(max_usd=args.sweep_max_usd, max_seeds=args.max_seeds),
        already_valid=already,
        already_excluded=already_excluded,
        expect_turn_matched=not args.fixed_budget,
        log=print,
    )

    rows = [
        result_row(cr.valid, cr.excluded)
        for cr in report.cells
        if cr.valid or cr.excluded
    ]
    if rows:
        write_results(args.results, rows, generated=time.strftime("%Y-%m-%dT%H:%M:%S"))
    print()
    for line in report.summary_lines():
        print(line)
    print(f"results -> {args.results}  traces -> {args.out_dir}")
    # A sweep that could not finish its matrix is a real failure — unlike a
    # model losing a scenario, which is data.
    return 0 if all(c.satisfied for c in report.cells) else 1


def cmd_bench(args: argparse.Namespace) -> int:
    """Run N seeds of one config, score + aggregate into results.json (needs ROM + key)."""
    import yaml

    from pokebench.agents.base import get_model, make_agent
    from pokebench.harness.emulator import Emulator
    from pokebench.metrics import result_row, write_results
    from pokebench.runner.bench import run_seeds
    from pokebench.runner.loop import make_success_check

    scenario = yaml.safe_load(args.scenario.read_text(encoding="utf-8"))
    state_file = args.state or (args.scenario.parent / scenario["anchor"]["state_file"])
    if not Path(state_file).exists():
        print(f"FAIL: anchor state {state_file} not found (record it via `pokebench play`).",
              file=sys.stderr)
        return 2
    config = _apply_cli_overrides(get_model(args.model), args.reasoning, args.thinking_budget)
    try:
        make_agent(config)  # fail fast on an unknown provider, before any seed runs
    except KeyError as e:
        print(f"FAIL: {e}", file=sys.stderr)
        return 2

    caps = _caps_from_scenario(
        scenario, args.max_turns, args.max_usd, args.max_tokens, args.max_minutes
    )

    def make_env():
        emu = Emulator(args.rom, window=False)
        emu.load_state(state_file)
        emu.settle(120)
        return emu

    label = f"{scenario['id']}-{args.model}-t{args.tier}"
    out_base = args.out_dir / label / time.strftime("%Y%m%d-%H%M%S")
    print(f"bench {scenario['id']} | model={args.model} tier={args.tier} seeds={args.seeds}")
    metrics = run_seeds(
        make_env,
        lambda: make_agent(config),
        objective=scenario["objective"],
        success=make_success_check(scenario["success"]),
        caps=caps,
        out_base=out_base,
        seeds=args.seeds,
        tier=args.tier,
        scenario_id=scenario["id"],
        save_raw=args.save_raw,
        log=print,
    )
    row = result_row(metrics)
    write_results(args.results, [row], generated=time.strftime("%Y-%m-%dT%H:%M:%S"))
    print(
        f"\n{scenario['id']}/{args.model}/tier{args.tier}: "
        f"success {row['success_rate']:.0%} ({row['seeds']} seeds) | "
        f"median turns={row['median_turns']} cost=${row['median_cost_usd']} "
        f"stuck={row['median_stuck_index']} idle={row['median_idle_rate']} "
        f"tiles={row['median_tiles_explored']} invalid={row['median_invalid_rate']}"
    )
    print(f"results -> {args.results}  traces -> {out_base}")
    # bench succeeds when it *ran and scored* — the model's win/loss is data in
    # results.json, not a command failure (models are expected to often fail).
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    from pokebench.replay import format_run, latest_run_dir, load_run

    run_dir = args.run_dir or latest_run_dir(args.base)
    if run_dir is None or not Path(run_dir).exists():
        print(
            f"no run found (looked in {args.base}). Run `pokebench run` first, "
            "or pass a run directory.",
            file=sys.stderr,
        )
        return 2
    print(format_run(load_run(run_dir), full=args.full))
    return 0


def cmd_site_build(args: argparse.Namespace) -> int:
    """Render `web/dist/` from `results.json` (+ `results_traces.txt`, if present).

    Offline like `replay`/`watch`/`score`: reads only files already on disk (never
    the ROM, an API, or the network). `--traces` is optional and missing-tolerant
    (see `site.build_site`'s docstring) so this still works on a fresh clone with no
    local `runs/`; it just publishes a leaderboard with no replay links.
    """
    from pokebench.site import build_site

    if not Path(args.results).exists():
        print(
            f"FAIL: {args.results} not found. Run `pokebench score --out ...` first.",
            file=sys.stderr,
        )
        return 2

    traces_path = args.traces
    if traces_path is not None and not Path(traces_path).exists():
        print(
            f"note: {traces_path} not found -- building the leaderboard with no replay "
            "links or curation commentary",
            file=sys.stderr,
        )
        traces_path = None

    report = build_site(args.results, args.out, traces_path)

    if report.missing_traces:
        print(
            f"note: {len(report.missing_traces)} trace dir(s) listed in {args.traces} were "
            "not found on disk (runs/ is gitignored/local-only) -- skipped, no replay page:",
            file=sys.stderr,
        )
        for m in report.missing_traces:
            print(f"  {m}", file=sys.stderr)

    print(
        f"wrote {report.rows} leaderboard row(s) + {report.run_pages} replay page(s) -> "
        f"{report.out_dir}"
    )
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    """Serve the live (or finished) run at `run_dir` for a browser to poll.

    Read-only, out-of-process (see `viewer.py`'s module docstring): this never
    touches the ROM, the emulator, or a running agent loop -- it only reads
    what `RunTracer` already wrote to disk, exactly like `pokebench replay`.
    """
    import webbrowser

    from pokebench.replay import latest_run_dir
    from pokebench.viewer import serve

    run_dir = args.run_dir or latest_run_dir(args.base)
    if run_dir is None or not Path(run_dir).exists():
        print(
            f"no run found (looked in {args.base}). Run `pokebench run` first, "
            "or pass a run directory.",
            file=sys.stderr,
        )
        return 2

    httpd = serve(run_dir, port=args.port)
    url = f"http://127.0.0.1:{httpd.server_port}/"
    print(f"watching {run_dir}")
    print(f"serving {url} (127.0.0.1 only; Ctrl+C to stop)")
    if not args.no_open:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="pokebench", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("play", help="interactive play + auto save-state capture")
    _add_rom_arg(p)
    p.add_argument("--state", type=Path, help="resume from an existing save state")
    p.add_argument("--capture", default="REDS_HOUSE_2F", help="map name that arms auto-capture")
    p.add_argument("--min-party", type=int, default=1, help="required party size to capture")
    p.add_argument("--out", type=Path, default=Path("scenarios/states/s1_bedroom.state"))
    p.add_argument("--no-capture", action="store_true", help="just play, never save states")
    p.set_defaults(func=cmd_play)

    p = sub.add_parser("inspect", help="print structured state from a save state")
    _add_rom_arg(p)
    p.add_argument("--state", type=Path, required=True)
    p.add_argument("--png", type=Path, help="also save a screenshot here")
    p.set_defaults(func=cmd_inspect)

    p = sub.add_parser("spike-s1", help="M0 acceptance: scripted route exits Pallet Town")
    _add_rom_arg(p)
    p.add_argument("--scenario", type=Path, default=Path("scenarios/s1_exit_pallet.yaml"))
    p.add_argument("--state", type=Path, help="override the scenario's anchor state file")
    p.add_argument(
        "--out-dir", type=Path, default=None,
        help="default: runs/spike-<scenario id>/<timestamp>",
    )
    p.add_argument("--window", action="store_true", help="show the emulator window (slower)")
    p.set_defaults(func=cmd_spike_s1)

    p = sub.add_parser("run", help="M1 agent loop: one model attempts a scenario unattended")
    _add_rom_arg(p)
    p.add_argument("--scenario", type=Path, default=Path("scenarios/s1_exit_pallet.yaml"))
    p.add_argument("--model", default="haiku", help="model key from the registry (default: haiku)")
    p.add_argument("--tier", type=int, default=0, choices=(0, 1), help="tool tier (default: 0)")
    p.add_argument("--state", type=Path, help="override the scenario's anchor state file")
    p.add_argument("--out-dir", type=Path, default=Path("runs/agent"))
    p.add_argument("--max-turns", type=int, help="override the scenario turn cap (for smoke runs)")
    p.add_argument("--max-usd", type=float, help="override the dollar kill switch")
    p.add_argument("--max-tokens", type=int, help="override the scenario token cap")
    p.add_argument("--max-minutes", type=int, help="override the scenario wall-clock cap")
    p.add_argument("--no-screenshots", action="store_true", help="skip per-turn screenshots")
    p.add_argument(
        "--save-raw",
        action="store_true",
        help="also save each turn's raw provider response body under runs/.../raw/ "
        "(off by default: real disk cost across a long run, and nothing scored reads it)",
    )
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("replay", help="inspect a completed agent run trace (no ROM/key)")
    p.add_argument(
        "run_dir", nargs="?", type=Path, help="run directory (default: latest under runs/agent)"
    )
    p.add_argument("--base", type=Path, default=Path("runs/agent"), help="where runs live")
    p.add_argument("--full", action="store_true", help="also show model reasoning per turn")
    p.set_defaults(func=cmd_replay)

    p = sub.add_parser(
        "watch",
        help="live read-only dual-pane viewer for a run (screen + model reasoning; no ROM/key)",
    )
    p.add_argument(
        "run_dir", nargs="?", type=Path, help="run directory (default: latest under --base)"
    )
    p.add_argument("--base", type=Path, default=Path("runs/agent"), help="where runs live")
    p.add_argument("--port", type=int, default=8765, help="localhost port to serve on")
    p.add_argument("--no-open", action="store_true", help="don't auto-open a browser tab")
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser(
        "site", help="build the static leaderboard + text replay pages (no ROM/key)"
    )
    site_sub = p.add_subparsers(dest="site_command", required=True)
    p2 = site_sub.add_parser(
        "build", help="render web/dist from results.json (+ results_traces.txt)"
    )
    p2.add_argument("--results", type=Path, default=Path("results.json"))
    p2.add_argument("--out", type=Path, default=Path("web/dist"))
    p2.add_argument(
        "--traces",
        type=Path,
        default=Path("results_traces.txt"),
        help="trace-dir list to link replay pages + surface curation commentary "
        "(optional; the leaderboard still builds without it)",
    )
    p2.set_defaults(func=cmd_site_build)

    p = sub.add_parser("score", help="score run traces into metrics (no ROM/key)")
    p.add_argument("run_dirs", nargs="+", type=Path, help="run directories to score")
    p.add_argument("--out", type=Path, help="write/merge aggregated rows into this results.json")
    p.add_argument(
        "--include-invalid",
        action="store_true",
        help="count seeds that failed the eval-integrity check (default: report and exclude)",
    )
    p.add_argument(
        "--turn-matched",
        action="store_true",
        help="also reject seeds that stopped on a non-turn cap (cross-model sweeps)",
    )
    p.set_defaults(func=cmd_score)

    p = sub.add_parser(
        "sweep",
        help="drive a matrix of configs to N *valid* seeds each (needs ROM + key)",
    )
    _add_rom_arg(p)
    p.add_argument(
        "--scenario", type=Path, nargs="+", required=True, help="one or more scenario YAMLs"
    )
    p.add_argument("--model", nargs="+", required=True, help="one or more registry model keys")
    p.add_argument("--tier", type=int, default=0, choices=(0, 1))
    p.add_argument("--seeds", type=int, default=3, help="VALID seeds required per cell")
    p.add_argument(
        "--max-retries", type=int, default=2, help="extra attempts per cell before giving up"
    )
    p.add_argument("--out-dir", type=Path, default=Path("runs/bench"))
    p.add_argument("--results", type=Path, default=Path("results.json"))
    # Sweep-level budget: the per-episode caps below bound ONE run; this bounds
    # the whole matrix, which is what was missing when an unattended sweep could
    # spend the project's remaining budget unsupervised.
    p.add_argument("--sweep-max-usd", type=float, help="hard dollar cap for the whole sweep")
    p.add_argument("--max-seeds", type=int, help="hard cap on total seeds launched")
    p.add_argument(
        "--resume",
        action="store_true",
        help="skip/top-up cells using valid seeds already on disk under --out-dir",
    )
    p.add_argument(
        "--fixed-budget",
        action="store_true",
        help="allow non-turn caps to bind (M2-style); default asserts turn-matching",
    )
    p.add_argument("--max-turns", type=int, help="override the scenario turn cap")
    p.add_argument("--max-usd", type=float, help="override the PER-SEED dollar kill switch")
    p.add_argument("--max-tokens", type=int, help="override the scenario token cap")
    p.add_argument("--max-minutes", type=int, help="override the scenario wall-clock cap")
    p.add_argument(
        "--save-raw",
        action="store_true",
        help="also save each turn's raw provider response body under runs/.../raw/",
    )
    p.add_argument(
        "--reasoning",
        choices=("default", "off"),
        default="default",
        help="'off' forces reasoning=False for every model in this sweep, overriding the "
        "registry default of 'on' -- use for a known-broken leg (e.g. Anthropic's "
        "ceiling-hit pathology, CLAUDE.md 'Two live caveats' #1) so a retry loop doesn't "
        "silently burn money reproducing it; 'default' (the default) makes no change",
    )
    p.add_argument(
        "--thinking-budget",
        type=int,
        default=None,
        help="Anthropic-only explicit extended-thinking budget in tokens (sets "
        "ModelConfig.thinking_budget_tokens and raises the request's max_tokens by the "
        "same amount, agents/anthropic.py) -- decouples deliberation from the frozen "
        "max_output_tokens=1024 answer ceiling instead of sharing it (CLAUDE.md 'Two live "
        "caveats' #1); unset (default) leaves every model on adaptive thinking, unchanged",
    )
    p.set_defaults(func=cmd_sweep)

    p = sub.add_parser("bench", help="run N seeds of one config -> results.json (needs ROM + key)")
    _add_rom_arg(p)
    p.add_argument("--scenario", type=Path, default=Path("scenarios/s1_exit_pallet.yaml"))
    p.add_argument("--model", default="haiku", help="model key from the registry")
    p.add_argument("--tier", type=int, default=0, choices=(0, 1))
    p.add_argument("--seeds", type=int, default=3, help="runs per config (median over these)")
    p.add_argument("--state", type=Path, help="override the scenario's anchor state file")
    p.add_argument("--out-dir", type=Path, default=Path("runs/bench"))
    # Repo root, not runs/ — runs/ is gitignored (traces are large and
    # machine-local), but results.json is the small published artifact the M4
    # leaderboard renders, so it has to be committable.
    p.add_argument("--results", type=Path, default=Path("results.json"))
    p.add_argument("--max-turns", type=int, help="override the scenario turn cap")
    p.add_argument("--max-usd", type=float, help="override the per-seed dollar kill switch")
    p.add_argument("--max-tokens", type=int, help="override the scenario token cap")
    p.add_argument("--max-minutes", type=int, help="override the scenario wall-clock cap")
    p.add_argument(
        "--save-raw",
        action="store_true",
        help="also save each turn's raw provider response body under runs/.../raw/",
    )
    p.add_argument(
        "--reasoning",
        choices=("default", "off"),
        default="default",
        help="'off' forces reasoning=False, overriding the registry default of 'on' -- use "
        "for a known-broken leg (e.g. Anthropic's ceiling-hit pathology, CLAUDE.md "
        "'Two live caveats' #1); 'default' (the default) makes no change",
    )
    p.add_argument(
        "--thinking-budget",
        type=int,
        default=None,
        help="Anthropic-only explicit extended-thinking budget in tokens (sets "
        "ModelConfig.thinking_budget_tokens and raises the request's max_tokens by the "
        "same amount, agents/anthropic.py) -- decouples deliberation from the frozen "
        "max_output_tokens=1024 answer ceiling instead of sharing it (CLAUDE.md 'Two live "
        "caveats' #1); unset (default) leaves every model on adaptive thinking, unchanged",
    )
    p.set_defaults(func=cmd_bench)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
