"""The agent seam — one `Agent` protocol, one `ModelConfig`, one registry.

The harness owns everything that must be identical across models: the
observation format (harness/observation.py), the conversation/history policy
(runner/history.py), the tool set (harness/tools.py), and the system prompt
(harness/prompts). An `Agent` is the thin remainder — translate the neutral
messages + tools into a provider call, return the tool call(s) and token usage.
No adapter may add context, tune the prompt, or reshape the observation; that
neutrality is the benchmark (CLAUDE.md).

M1 ships one adapter (Anthropic) and one model (Haiku). M3 adds OpenAI / Google
/ Ollama behind this same seam; the registry is where new models are declared.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from pokebench.harness.observation import Message
from pokebench.harness.tools import ToolSpec


@dataclass(frozen=True)
class ModelConfig:
    """Everything the harness needs to run and price one model.

    Prices are USD per 1M tokens (the units providers quote). Pinning
    `temperature` here — not per run — keeps sampling a controlled variable:
    the emulator is deterministic, so temperature is the intended source of the
    N=3 seed variance the scoring plan calls for.
    """

    name: str  # friendly key used on the CLI, e.g. "haiku"
    provider: str  # "anthropic" | "openai" | ...
    model_id: str  # provider model id, e.g. "claude-haiku-4-5"
    temperature: float
    max_output_tokens: int
    input_price_per_mtok: float
    output_price_per_mtok: float
    # Reasoning/thinking policy. The benchmark runs **reasoning enabled for
    # every model**, because there is no neutral alternative: OpenAI's Responses
    # API and Gemini both reason by default, while Anthropic's default (omitting
    # `thinking`) is *no* reasoning. Leaving each vendor "as shipped" therefore
    # silently grants deliberation to two vendors and denies it to the third —
    # a harness effect masquerading as a model result. Adapters whose provider
    # reasons by default ignore this flag; the Anthropic adapter sends
    # `thinking: {"type": "adaptive"}` when it is True. Recorded in meta.json so
    # runs under the old (Anthropic-reasoning-off) policy stay distinguishable.
    reasoning: bool = True
    # Explicit Anthropic-only thinking budget (Task, 2026-08-14 — CLAUDE.md "Two
    # live caveats" #1). `max_output_tokens=1024` was a runaway-consumption guard
    # (SECURITY.md LLM10), not a limit on deliberation — but Anthropic bills
    # extended thinking against `max_tokens`, so the guard silently became a
    # thinking cap for this one vendor: with `reasoning=True` and this field
    # unset, the adapter sends "adaptive" (open-ended) thinking against the
    # unmodified 1024, and on a real observation sonnet hit that ceiling on 39%
    # of turns, emitting no tool call on 37% of them. Setting this field gives
    # Anthropic alone an explicit thinking budget and raises the request's
    # `max_tokens` to fit budget + the unchanged 1024 answer allowance
    # (`agents/anthropic.py`); every other adapter ignores it — OpenAI/Google/
    # Ollama reason by default with no comparable per-call budget knob. `None`
    # (the default, and every existing `MODELS` entry) is the byte-for-byte-
    # unchanged path for every published row — see
    # `tests/test_anthropic_adapter.py`'s explicit "unset" assertion. Verified
    # live before this field existed: `tools/probe_thinking_budget.py` (one
    # call) confirmed `budget_tokens=1024` (the API's documented minimum) with
    # `max_tokens=2048` (budget + 1024) succeeds, and thinking tokens come back
    # broken out from answer tokens via `usage.output_tokens_details
    # .thinking_tokens` — so a future results table can report them separately.
    thinking_budget_tokens: int | None = None

    def cost(self, usage: TokenUsage) -> float:
        """USD cost of one turn's usage under this model's prices."""
        return (
            usage.input_tokens / 1_000_000 * self.input_price_per_mtok
            + usage.output_tokens / 1_000_000 * self.output_price_per_mtok
        )


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens

    def __add__(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
        )

    def to_dict(self) -> dict:
        return {"input_tokens": self.input_tokens, "output_tokens": self.output_tokens}


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    input: dict
    # Opaque, provider-specific state that must be echoed back verbatim when
    # this call is replayed in history. Gemini 3 requires it: a functionCall
    # replayed without its `thoughtSignature` is rejected with a 400. It is
    # deliberately untyped and never inspected by the harness -- it carries no
    # semantic content, so it does not alter the observation contract, and
    # adapters that need nothing simply leave it None.
    provider_state: dict | None = None


@dataclass
class AgentResponse:
    """One model turn, normalized across providers."""

    text: str  # any assistant text (reasoning / narration)
    tool_calls: list[ToolCall]
    usage: TokenUsage
    stop_reason: str | None = None
    # The unparsed provider response body, verbatim (Task A, 2026-08-11).
    # Additive and backward-compatible: every existing caller constructs
    # AgentResponse with keyword args, so this default costs them nothing.
    # Exists so a `parse_response` bug (a dropped field, a missed item type —
    # see the OpenAI "reasoning" item incident) can be diagnosed after the
    # fact instead of staying invisible forever, because nothing on disk
    # would otherwise contradict the parser. Never inlined into run.jsonl;
    # `RunTracer.record_turn(..., raw=resp.raw)` writes it to a side file,
    # opt-in via `save_raw` (disk cost across a long run is real). Contains
    # only the response BODY a provider returned — never a request, headers,
    # or a URL — so it cannot carry an API key: every adapter sends its key in
    # a header (see agents/_http.py's `redact()` docstring on why the query
    # string is the dangerous place), never in the payload or response.
    raw: dict | None = None


@runtime_checkable
class Agent(Protocol):
    """A model behind the fixed harness. `act` is a pure translation step."""

    config: ModelConfig

    def act(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        allow_parallel: bool = False,
    ) -> AgentResponse: ...


# --- Model registry -------------------------------------------------------
# Prices are USD per MTok, verified against each vendor's public pricing page on
# 2026-07-20. Add new models here — never branch on model id in loop/adapter
# code. Every entry pins `temperature`, which is the intended source of N=3 seed
# variance; before adding a model, confirm it still *accepts* temperature (newer
# Anthropic models reject it with a 400 — see HANDOFF.md M3 Blocker 2) and that
# it takes an image plus a tool call in the same request. All four models below
# were probed live for exactly that.

MODELS: dict[str, ModelConfig] = {
    "haiku": ModelConfig(
        name="haiku",
        provider="anthropic",
        model_id="claude-haiku-4-5",
        temperature=1.0,
        max_output_tokens=1024,
        input_price_per_mtok=1.0,
        output_price_per_mtok=5.0,
    ),
    "sonnet": ModelConfig(
        name="sonnet",
        provider="anthropic",
        model_id="claude-sonnet-4-6",
        temperature=1.0,
        max_output_tokens=1024,
        input_price_per_mtok=3.0,
        output_price_per_mtok=15.0,
    ),
    "gpt": ModelConfig(
        name="gpt",
        provider="openai",
        model_id="gpt-5.6-terra",
        temperature=1.0,
        max_output_tokens=1024,
        input_price_per_mtok=2.5,
        output_price_per_mtok=15.0,
    ),
    # The tier-comparable Google model (vs sonnet-4-6 / gpt-5.6-terra). The
    # free tier allowed only 20 requests total on this model, which blocked the
    # leg until billing was enabled on the key; the full M3 sweep then ran on
    # it (9 seeds, ~630 requests, 2026-07-20).
    "gemini": ModelConfig(
        name="gemini",
        provider="google",
        model_id="gemini-3.5-flash",
        temperature=1.0,
        max_output_tokens=1024,
        input_price_per_mtok=1.5,
        output_price_per_mtok=9.0,
    ),
    # Free-tier stand-in with its own quota bucket, verified live to do vision +
    # tools + temperature. A *lite* tier at ~1/10th the price of the models
    # above, so read its row as a weight class of its own, not as "Google's
    # answer to Sonnet". Kept as a separate entry rather than repointing
    # "gemini" so the paid run later ADDS a row instead of overwriting this one.
    "gemini-lite": ModelConfig(
        name="gemini-lite",
        provider="google",
        model_id="gemini-3.1-flash-lite",
        temperature=1.0,
        max_output_tokens=1024,
        input_price_per_mtok=0.25,
        output_price_per_mtok=1.5,
    ),
    # The local leg: served by Ollama on this machine, so no per-token cost.
    "qwen-local": ModelConfig(
        name="qwen-local",
        provider="ollama",
        model_id="qwen3.5:4b",
        temperature=1.0,
        max_output_tokens=1024,
        input_price_per_mtok=0.0,
        output_price_per_mtok=0.0,
    ),
}


def get_model(name: str) -> ModelConfig:
    try:
        return MODELS[name]
    except KeyError:
        raise KeyError(
            f"unknown model {name!r}; known models: {', '.join(sorted(MODELS))}"
        ) from None


def make_agent(config: ModelConfig, **kwargs) -> Agent:
    """Build the adapter for a model's provider.

    The one place providers are dispatched. Adapters are imported lazily so a
    missing optional dependency (or key) for one provider never breaks the
    others, and so the registry can be read without importing any HTTP client.
    """
    provider = config.provider
    if provider == "anthropic":
        from pokebench.agents.anthropic import AnthropicAgent

        return AnthropicAgent(config, **kwargs)
    if provider == "openai":
        from pokebench.agents.openai import OpenAIAgent

        return OpenAIAgent(config, **kwargs)
    if provider == "google":
        from pokebench.agents.google import GoogleAgent

        return GoogleAgent(config, **kwargs)
    if provider == "ollama":
        from pokebench.agents.ollama import OllamaAgent

        return OllamaAgent(config, **kwargs)
    raise KeyError(f"no adapter for provider {provider!r} (model {config.name!r})")
