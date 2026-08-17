"""Replay / inspect a completed agent run from its JSONL trace.

`pokebench replay [run_dir]` reads a run directory (`meta.json`, `run.jsonl`,
`summary.json`) and prints a per-turn view, a token/cost breakdown, and a
stuck-loop report. It touches no ROM, emulator, or API — just the trace files —
so it is a pure, offline debugging tool and a preview of the M2 stuck-loop
index (surfaced read-only; it never changes how a run behaves).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Run:
    dir: Path
    meta: dict
    records: list[dict]
    summary: dict | None


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def load_run(run_dir: str | Path) -> Run:
    d = Path(run_dir)
    records: list[dict] = []
    jsonl = d / "run.jsonl"
    if jsonl.exists():
        for line in jsonl.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return Run(d, _read_json(d / "meta.json") or {}, records, _read_json(d / "summary.json"))


def latest_run_dir(base: str | Path = "runs/agent") -> Path | None:
    base = Path(base)
    if not base.exists():
        return None
    runs = [p for p in base.iterdir() if p.is_dir() and (p / "run.jsonl").exists()]
    return max(runs, key=lambda p: p.stat().st_mtime) if runs else None


def _stuck_key(record: dict):
    """A key for 'the world is unchanged'. Prefer the observation fingerprint;
    fall back to map+position for older traces that predate it."""
    h = record.get("observation_hash")
    if h:
        return ("hash", h)
    st = record.get("state", {})
    return ("pos", st.get("map_id"), st.get("x"), st.get("y"))


def find_stuck_segments(records: list[dict], min_len: int = 3) -> list[tuple]:
    """Runs of >= min_len consecutive turns where the world did not change.
    Returns (start_turn, end_turn, count, label) per segment."""
    segments: list[tuple] = []
    i, n = 0, len(records)
    while i < n:
        j = i
        while j + 1 < n and _stuck_key(records[j + 1]) == _stuck_key(records[i]):
            j += 1
        length = j - i + 1
        if length >= min_len:
            st = records[i].get("state", {})
            label = f"{st.get('map_name', '?')} ({st.get('x', '?')},{st.get('y', '?')})"
            segments.append((records[i].get("turn"), records[j].get("turn"), length, label))
        i = j + 1
    return segments


def token_report(records: list[dict]) -> dict:
    turns = len(records)
    inp = sum(r.get("usage", {}).get("input_tokens", 0) for r in records)
    out = sum(r.get("usage", {}).get("output_tokens", 0) for r in records)
    cost = sum(r.get("cost_usd", 0.0) for r in records)
    return {
        "turns": turns,
        "input_tokens": inp,
        "output_tokens": out,
        "avg_input_per_turn": round(inp / turns) if turns else 0,
        "avg_output_per_turn": round(out / turns) if turns else 0,
        "cost_usd": round(cost, 4),
    }


def _turn_bits(record: dict) -> str:
    bits = []
    note = record.get("notes_update")
    if note is not None:
        bits.append(f"notes:{note.get('chars', '?')}")
    action = record.get("action")
    if action is not None:
        ex = action.get("executed") or []
        bits.append(", ".join(ex) if ex else "no-op")
    return " + ".join(bits) if bits else "no tool"


def format_turn(record: dict, full: bool = False) -> str:
    st = record.get("state", {})
    usd = record.get("cumulative", {}).get("usd", 0.0)
    line = (
        f"t{record.get('turn', 0):>3} [{_turn_bits(record)}] -> "
        f"{st.get('map_name', '?')} ({st.get('x', '?')},{st.get('y', '?')}) "
        f"face={st.get('facing', '?')} ${usd:.4f}"
    )
    if record.get("success"):
        line += "  <== SUCCESS"
    if full:
        text = (record.get("model", {}).get("text") or "").strip().replace("\n", " ")
        if text:
            line += f"\n      reasoning: {text[:200]}"
    return line


def format_run(run: Run, full: bool = False) -> str:
    m = run.meta
    lines = [
        f"run: {run.dir}",
        f"model={m.get('model', '?')} tier={m.get('tier', '?')} "
        f"prompt_v={m.get('system_prompt_version', '?')} tools={m.get('tools', '?')}",
        "-" * 64,
    ]
    lines += [format_turn(r, full) for r in run.records]
    lines.append("-" * 64)

    tr = token_report(run.records)
    lines.append(
        f"tokens: {tr['input_tokens']}+{tr['output_tokens']} "
        f"(~{tr['avg_input_per_turn']}+{tr['avg_output_per_turn']}/turn) "
        f"cost=${tr['cost_usd']}"
    )

    stuck = find_stuck_segments(run.records)
    if stuck:
        lines.append(f"stuck-loops ({len(stuck)}):")
        for start, end, count, label in stuck:
            lines.append(f"  turns {start}-{end}: {count} turns unchanged at {label}")
    else:
        lines.append("stuck-loops: none")

    if run.summary:
        s = run.summary
        verdict = "SUCCESS" if s.get("success") else f"STOPPED ({s.get('stop_reason')})"
        lines.append(
            f"verdict: {verdict} in {s.get('turns')} turns, "
            f"${s.get('usd')}, {s.get('wall_seconds')}s"
        )
    return "\n".join(lines)
