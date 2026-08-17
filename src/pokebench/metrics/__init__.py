"""Scoring (M2): the five metrics + N-seed aggregation + results.json.

- `score.py` — `score_run(trace)` → success@cap, steps-to-success, tokens/$,
  stuck-loop index, invalid-action rate (all offline, from the JSONL trace).
- `results.py` — median over N seeds, one leaderboard row per
  (model, scenario, tier), and the `results.json` builder the M4 site renders.
- `validity.py` — the eval-integrity gate: did this run measure the *model* or
  the *harness*? Invalid seeds never reach a published row, and every rejection
  is recorded in `results.json` instead of a hand-written comment file.

The N-seed *executor* lives in `runner/bench.py`, and the matrix driver over
many configs in `runner/sweep.py`; scored output lands via `pokebench score` /
`pokebench bench` / `pokebench sweep`.
"""

from pokebench.metrics.results import aggregate, merge_rows, result_row, write_results
from pokebench.metrics.score import RunMetrics, score_run
from pokebench.metrics.validity import Validity, check_validity, partition

__all__ = [
    "RunMetrics",
    "score_run",
    "aggregate",
    "result_row",
    "merge_rows",
    "write_results",
    "Validity",
    "check_validity",
    "partition",
]
