"""N-seed executor (M2): run one (model, scenario, tier) config N times and
score each run.

The emulator is deterministic and temperature is pinned, so the N seeds are N
*independent* runs whose only variance is the model's sampling. It takes
`make_env` / `make_agent` factories (a fresh, anchor-loaded env per seed) so
the aggregation is testable with fakes; the CLI wires the real Emulator +
AnthropicAgent.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from pokebench.harness.trace import RunTracer
from pokebench.metrics.score import RunMetrics, score_run
from pokebench.replay import load_run
from pokebench.runner.loop import Caps, SuccessCheck, run_episode


def run_seeds(
    make_env: Callable[[], object],
    make_agent: Callable[[], object],
    *,
    objective: str,
    success: SuccessCheck,
    caps: Caps,
    out_base: str | Path,
    seeds: int = 3,
    tier: int = 0,
    scenario_id: str | None = None,
    screenshots: bool = True,
    save_raw: bool = False,
    log: Callable[[str], None] | None = None,
) -> list[RunMetrics]:
    """Run `seeds` episodes into `out_base/seedN/`, scoring each. Returns the
    per-seed metrics (feed to `metrics.result_row` to aggregate).

    `save_raw` (Task A, 2026-08-11): opt-in, forwarded to each seed's
    `RunTracer` so a parser bug is diagnosable from `seedN/raw/turn_*.json`
    instead of leaving nothing on disk to contradict the parser.
    """
    out_base = Path(out_base)
    metrics: list[RunMetrics] = []
    for seed in range(seeds):
        out_dir = out_base / f"seed{seed}"
        tracer = RunTracer(out_dir, screenshots=screenshots, save_raw=save_raw)
        env = make_env()
        agent = make_agent()
        try:
            result = run_episode(
                env,
                agent,
                objective=objective,
                success=success,
                caps=caps,
                tracer=tracer,
                tier=tier,
                scenario_id=scenario_id,
                log=log,
            )
            tracer.finish(result.summary_dict())
        finally:
            tracer.close()
            close = getattr(env, "close", None)
            if callable(close):
                close()
        m = score_run(load_run(out_dir))
        metrics.append(m)
        if log:
            log(
                f"seed {seed}: success={m.success} turns={m.turns} ${m.cost_usd} "
                f"stuck={m.stuck_index} invalid={m.invalid_rate}"
            )
    return metrics
