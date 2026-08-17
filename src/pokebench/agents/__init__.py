"""Provider adapters (M1/M3): Anthropic, OpenAI, Google, Ollama.

One `Agent(model_config)` seam (`base.py`). Every adapter receives the identical
observation package (screenshot + structured state block per tool tier) and
returns tool calls — no per-model prompt tuning, no extra context. Token and
cost metering is implemented here per provider.

M1: `base` (protocol + `ModelConfig` + registry) and `anthropic` (Messages API).
OpenAI / Google / Ollama adapters land in M3 behind the same seam.
"""
