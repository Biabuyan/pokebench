"""`--reasoning {default,off}` and `--thinking-budget N` on `bench`/`sweep` --
live-trap / dead-code regressions.

`MODELS["sonnet"]` has no explicit `reasoning=False`; it inherits the
dataclass default `reasoning: bool = True`. Before `--reasoning` existed,
`pokebench sweep --model sonnet` would silently reproduce the 39%/37%
ceiling-hit/no-tool-call pathology (CLAUDE.md, "Two live caveats" #1) inside
the sweep's own retry loop, burning money on a broken leg with no way to opt
out from the CLI.

`--thinking-budget N` (Stage 2, 2026-08-14) exercises `ModelConfig.
thinking_budget_tokens`, which Stage 1 landed but nothing could set from the
CLI -- the field existed and `agents/anthropic.py` read it, but every run was
stuck on the registry default of `None` (adaptive thinking) regardless of
intent. Same shape as `--reasoning`: additive, `None` (the CLI default)
changes nothing, `dataclasses.replace` applied at the same point.

Tested at the EARLIEST `make_agent` call each command makes -- the fail-fast
"no adapter for this provider" check, which both `cmd_bench` and `cmd_sweep`
already perform before touching the ROM or the scenario's objective/success
fields. `make_agent` is monkeypatched to record the `ModelConfig` it receives
and then raise `KeyError`, the one exception both commands already catch as a
clean, early stop -- so the test never reaches real ROM/network code, and the
scenario fixture only needs the two keys read before that point (`anchor.
state_file`, and `id` for `sweep`). No ROM, network, or API key needed.
"""

from __future__ import annotations

from pathlib import Path

import pokebench.agents.base as agents_base
from pokebench.cli import main


def _write_scenario(tmp_path: Path) -> Path:
    state_file = tmp_path / "anchor.state"
    state_file.write_bytes(b"")  # existence is all cmd_bench/cmd_sweep check here
    scenario = tmp_path / "scenario.yaml"
    scenario.write_text(
        "id: s_test\nanchor:\n  state_file: anchor.state\n",
        encoding="utf-8",
    )
    return scenario


def _capture_and_abort(monkeypatch, captured: list):
    """Patch `agents.base.make_agent` to record the config it was called
    with, then raise `KeyError` -- both `cmd_bench` and `cmd_sweep` already
    catch this as a clean "no adapter" failure, so the test stops there
    instead of reaching ROM/network code."""

    def fake_make_agent(config, **kwargs):
        captured.append(config)
        raise KeyError("stop here (test double)")

    monkeypatch.setattr(agents_base, "make_agent", fake_make_agent)


def test_bench_reasoning_off_replaces_config_before_agent_is_constructed(
    tmp_path, monkeypatch
):
    scenario = _write_scenario(tmp_path)
    captured: list = []
    _capture_and_abort(monkeypatch, captured)

    rc = main(
        [
            "bench",
            "--scenario", str(scenario),
            "--model", "sonnet",
            "--reasoning", "off",
            "--results", str(tmp_path / "results.json"),
            "--out-dir", str(tmp_path / "runs"),
        ]
    )

    assert rc == 2  # aborted at the fail-fast make_agent call, by design
    assert len(captured) == 1
    assert captured[0].reasoning is False


def test_bench_reasoning_default_leaves_registry_value_untouched(tmp_path, monkeypatch):
    scenario = _write_scenario(tmp_path)
    captured: list = []
    _capture_and_abort(monkeypatch, captured)

    rc = main(
        [
            "bench",
            "--scenario", str(scenario),
            "--model", "sonnet",
            "--results", str(tmp_path / "results.json"),
            "--out-dir", str(tmp_path / "runs"),
        ]
    )  # no --reasoning flag at all

    assert rc == 2
    assert captured[0].reasoning is True  # sonnet's registry default, untouched


def test_sweep_reasoning_off_replaces_config_before_agent_is_constructed(
    tmp_path, monkeypatch
):
    scenario = _write_scenario(tmp_path)
    captured: list = []
    _capture_and_abort(monkeypatch, captured)

    rc = main(
        [
            "sweep",
            "--scenario", str(scenario),
            "--model", "sonnet",
            "--reasoning", "off",
            "--results", str(tmp_path / "results.json"),
            "--out-dir", str(tmp_path / "runs"),
            "--sweep-max-usd", "0.01",
        ]
    )

    assert rc == 2
    assert len(captured) == 1
    assert captured[0].reasoning is False


def test_sweep_reasoning_default_leaves_registry_value_untouched(tmp_path, monkeypatch):
    scenario = _write_scenario(tmp_path)
    captured: list = []
    _capture_and_abort(monkeypatch, captured)

    rc = main(
        [
            "sweep",
            "--scenario", str(scenario),
            "--model", "sonnet",
            "--results", str(tmp_path / "results.json"),
            "--out-dir", str(tmp_path / "runs"),
            "--sweep-max-usd", "0.01",
        ]
    )  # no --reasoning flag at all

    assert rc == 2
    assert captured[0].reasoning is True


# --- --thinking-budget N ----------------------------------------------------


def test_bench_thinking_budget_sets_config_before_agent_is_constructed(tmp_path, monkeypatch):
    scenario = _write_scenario(tmp_path)
    captured: list = []
    _capture_and_abort(monkeypatch, captured)

    rc = main(
        [
            "bench",
            "--scenario", str(scenario),
            "--model", "sonnet",
            "--thinking-budget", "4096",
            "--results", str(tmp_path / "results.json"),
            "--out-dir", str(tmp_path / "runs"),
        ]
    )

    assert rc == 2  # aborted at the fail-fast make_agent call, by design
    assert len(captured) == 1
    assert captured[0].thinking_budget_tokens == 4096


def test_bench_thinking_budget_omitted_leaves_it_none(tmp_path, monkeypatch):
    scenario = _write_scenario(tmp_path)
    captured: list = []
    _capture_and_abort(monkeypatch, captured)

    rc = main(
        [
            "bench",
            "--scenario", str(scenario),
            "--model", "sonnet",
            "--results", str(tmp_path / "results.json"),
            "--out-dir", str(tmp_path / "runs"),
        ]
    )  # no --thinking-budget flag at all

    assert rc == 2
    assert captured[0].thinking_budget_tokens is None  # the registry default, untouched


def test_sweep_thinking_budget_sets_config_before_agent_is_constructed(tmp_path, monkeypatch):
    scenario = _write_scenario(tmp_path)
    captured: list = []
    _capture_and_abort(monkeypatch, captured)

    rc = main(
        [
            "sweep",
            "--scenario", str(scenario),
            "--model", "sonnet",
            "--thinking-budget", "4096",
            "--results", str(tmp_path / "results.json"),
            "--out-dir", str(tmp_path / "runs"),
            "--sweep-max-usd", "0.01",
        ]
    )

    assert rc == 2
    assert len(captured) == 1
    assert captured[0].thinking_budget_tokens == 4096


def test_sweep_thinking_budget_omitted_leaves_it_none(tmp_path, monkeypatch):
    scenario = _write_scenario(tmp_path)
    captured: list = []
    _capture_and_abort(monkeypatch, captured)

    rc = main(
        [
            "sweep",
            "--scenario", str(scenario),
            "--model", "sonnet",
            "--results", str(tmp_path / "results.json"),
            "--out-dir", str(tmp_path / "runs"),
            "--sweep-max-usd", "0.01",
        ]
    )  # no --thinking-budget flag at all

    assert rc == 2
    assert captured[0].thinking_budget_tokens is None
