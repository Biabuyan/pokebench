"""Scenario runner.

Enforces hard per-run turn/token/dollar/wall-clock caps in code (OWASP LLM10
unbounded consumption) — the caps live in the runner, not in the prompt.

M1: `loop` (the observe→decide→execute→trace episode + caps) and `history`
(the rolling-window fairness policy). The N-seed executor and results.json
builder land in M2.
"""
