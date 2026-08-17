"""JSONL run traces — the audit-log spine of PokéBench (see SECURITY.md).

`TracingWalker` records the M0 scripted spike (one dpad action per line).
`RunTracer` (M1) records the agent loop: one line per turn carrying the model's
output, the tool call, token usage, cost, the resulting state, and a screenshot
— so every LLM run is a complete, replayable tool-call audit trail.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from pokebench.harness.state import GameState


class TracingWalker:
    """Wraps an Emulator (or any Walker) and records every dpad action.

    Also records `tap()` (Task B/C, 2026-08-06: `EncounterAwareWalker`'s
    press-and-release primitive for resolving battles / dismissing bump-text)
    on the SAME step counter and into the SAME `trace.jsonl`, interleaved in
    real call order -- an escape attempt is exactly the kind of thing worth
    auditing after the fact (the HP/PP hazard that motivated it), and a
    separate log would be easy to miss.
    """

    def __init__(self, inner, out_dir: str | Path, screenshots: bool = True):
        self.inner = inner
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.screenshots = screenshots and hasattr(inner, "screenshot")
        self.steps = 0
        self._trace = open(self.out_dir / "trace.jsonl", "a", encoding="utf-8")
        self._t0 = time.time()

    def state(self) -> GameState:
        return self.inner.state()

    def _record(self, action: dict) -> None:
        self.steps += 1
        state = self.inner.state()
        record = {
            "step": self.steps,
            "t": round(time.time() - self._t0, 3),
            "action": action,
            "state": state.to_dict(),
        }
        self._trace.write(json.dumps(record) + "\n")
        self._trace.flush()
        if self.screenshots:
            self.inner.screenshot().save(self.out_dir / f"step_{self.steps:04d}.png")

    def dpad(self, direction: str) -> None:
        self.inner.dpad(direction)
        self._record({"type": "dpad", "button": direction})

    def tap(self, button: str) -> None:
        self.inner.tap(button)
        self._record({"type": "tap", "button": button})

    def settle(self, frames: int) -> None:
        self.inner.settle(frames)

    def close(self) -> None:
        self._trace.close()


class RunTracer:
    """Audit log for one agent-loop episode.

    Writes ``meta.json`` (run configuration), ``run.jsonl`` (one record per
    turn), ``summary.json`` (the final RunResult), and — unless disabled — a
    ``turn_XXXX.png`` screenshot series. The loop hands each turn a plain dict;
    keeping the record shape here (not in the loop) means the trace format has
    a single owner.

    ``save_raw`` (opt-in, default off — Task A, 2026-08-11): when true, the
    unparsed provider response body for a turn is written to
    ``raw/turn_XXXX.json`` instead of being inlined into ``run.jsonl``. This is
    the only way an `AgentResponse`-parser bug can ever be caught after the
    fact — a dropped field in `parse_response` (see the OpenAI "reasoning"
    item incident) leaves no trace today because nothing on disk contradicts
    the parser. Off by default because a raw body is pure debug payload with
    real, unbounded disk cost across a long run (turns * body size), and
    nothing in metrics/replay reads it back — it would silently grow every
    run's footprint for a feature that exists to catch a rare class of bug,
    not one anything currently depends on.
    """

    def __init__(self, out_dir: str | Path, screenshots: bool = True, save_raw: bool = False):
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.screenshots = screenshots
        self.save_raw = save_raw
        self.turns = 0
        self._trace = open(self.out_dir / "run.jsonl", "a", encoding="utf-8")
        self._t0 = time.time()

    def write_meta(self, meta: dict) -> None:
        (self.out_dir / "meta.json").write_text(
            json.dumps(meta, indent=2), encoding="utf-8"
        )

    def record_turn(self, record: dict, screenshot=None, raw: dict | None = None) -> None:
        self.turns += 1
        record = {"turn": self.turns, "t": round(time.time() - self._t0, 3), **record}
        self._trace.write(json.dumps(record) + "\n")
        self._trace.flush()
        if self.screenshots and screenshot is not None:
            screenshot.save(self.out_dir / f"turn_{self.turns:04d}.png")
        if self.save_raw and raw is not None:
            raw_dir = self.out_dir / "raw"
            raw_dir.mkdir(parents=True, exist_ok=True)
            (raw_dir / f"turn_{self.turns:04d}.json").write_text(
                json.dumps(raw, indent=2), encoding="utf-8"
            )

    def finish(self, summary: dict) -> None:
        (self.out_dir / "summary.json").write_text(
            json.dumps(summary, indent=2), encoding="utf-8"
        )

    def close(self) -> None:
        self._trace.close()
