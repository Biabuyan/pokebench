"""Minimal JSON-over-HTTP poster shared by the non-Anthropic adapters.

The M3 adapters talk to their providers over plain HTTP rather than three
vendor SDKs. That keeps `anthropic` the only hard dependency (httpx already
ships with it), keeps the wire shape visible in this repo instead of hidden
behind a client library, and — the point that matters for the benchmark — lets
a test inject a fake poster so no adapter test needs a network or a key.

Retries live here because rate limits are a *transport* fact, not a model fact:
a 429 says nothing about whether the model can play Pokémon, so retrying it is
not per-model tuning. Without this, a free-tier Gemini key (20 requests/minute)
kills a run around turn 7 — which is exactly what happened on 2026-07-20. The
turn-matched sweep design means the extra wall-clock costs nothing scientific;
only the turn budget is held equal across models.
"""

from __future__ import annotations

import random
import time
from typing import Protocol, runtime_checkable

DEFAULT_TIMEOUT_S = 300.0

# 429 = rate limited; 5xx = provider-side wobble. Both are worth retrying.
RETRY_STATUSES = frozenset({408, 409, 425, 429, 500, 502, 503, 504})
MAX_RETRIES = 6
MAX_BACKOFF_S = 90.0


class ProviderHTTPError(RuntimeError):
    """A non-2xx provider response, carrying the body so the cause is visible.

    Raised instead of httpx's own error because that one stringifies the full
    URL — and Google's REST API takes the key as a `?key=` query parameter, so
    an httpx traceback would print the API key into logs and terminals. The URL
    is truncated at `?` here; never put a secret back into this message.
    """


def redact(url: str) -> str:
    """Drop any query string, which is where API keys hide."""
    return url.split("?", 1)[0]


def parse_retry_delay(headers: dict, body: str) -> float | None:
    """Seconds the provider asked us to wait, if it said so.

    Honours the standard `Retry-After` header and Google's in-body
    `"retryDelay": "31.3s"`, so we wait exactly as long as told rather than
    guessing with backoff.
    """
    for key in ("retry-after", "Retry-After"):
        raw = headers.get(key)
        if raw:
            try:
                return float(raw)
            except ValueError:
                pass
    marker = '"retryDelay"'
    if marker in body:
        try:
            tail = body.split(marker, 1)[1].split('"', 2)[1]
            return float(tail.rstrip("s"))
        except (IndexError, ValueError):
            pass
    return None


def backoff_delay(attempt: int, headers: dict, body: str) -> float:
    """Provider-requested delay if available, else exponential with jitter."""
    asked = parse_retry_delay(headers, body)
    if asked is not None:
        return min(asked + 0.5, MAX_BACKOFF_S)
    return min(2.0**attempt + random.uniform(0, 1), MAX_BACKOFF_S)


@runtime_checkable
class JsonPoster(Protocol):
    """POST a JSON body, return the decoded JSON response."""

    def __call__(self, url: str, payload: dict, headers: dict | None = None) -> dict: ...


class HttpxPoster:
    """Real poster, with retry on rate limits and transient provider errors.

    `_send` is the single seam a test overrides to script status codes without
    touching the network.
    """

    def __init__(
        self,
        timeout: float = DEFAULT_TIMEOUT_S,
        max_retries: int = MAX_RETRIES,
        sleep=time.sleep,
        log=None,
    ):
        self.timeout = timeout
        self.max_retries = max_retries
        self._sleep = sleep
        self._log = log
        self._client = None

    def _send(self, url: str, payload: dict, headers: dict):
        """One request -> (status, headers, text, json_or_None)."""
        import httpx

        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout)
        resp = self._client.post(url, json=payload, headers=headers)
        body_json = None
        if resp.status_code < 400:
            body_json = resp.json()
        return resp.status_code, dict(resp.headers), resp.text, body_json

    def _transport_errors(self):
        """Connection-level failures worth retrying (DNS, resets, timeouts).

        These never produce an HTTP status, so the status-code path below can't
        see them. A DNS blip took out a 650-minute sweep leg overnight on
        2026-07-21 (`getaddrinfo failed`) — an unattended multi-hour benchmark
        must ride out a transient network drop rather than die on it.
        """
        import httpx

        return (httpx.TransportError, OSError)

    def __call__(self, url: str, payload: dict, headers: dict | None = None) -> dict:
        last = None
        retryable = self._transport_errors()
        for attempt in range(self.max_retries + 1):
            try:
                status, resp_headers, text, body = self._send(url, payload, headers or {})
            except retryable as e:
                if attempt >= self.max_retries:
                    raise ProviderHTTPError(
                        f"connection error from {redact(url)} after "
                        f"{self.max_retries} retries: {type(e).__name__}: {e}"
                    ) from e
                delay = backoff_delay(attempt, {}, "")
                if self._log:
                    self._log(
                        f"  {type(e).__name__} from {redact(url)}; "
                        f"retry {attempt + 1}/{self.max_retries} in {delay:.1f}s"
                    )
                self._sleep(delay)
                continue
            if status < 400:
                return body
            last = (status, text)
            if status in RETRY_STATUSES and attempt < self.max_retries:
                delay = backoff_delay(attempt, resp_headers, text)
                if self._log:
                    self._log(
                        f"  HTTP {status} from {redact(url)}; "
                        f"retry {attempt + 1}/{self.max_retries} in {delay:.1f}s"
                    )
                self._sleep(delay)
                continue
            break
        status, text = last
        raise ProviderHTTPError(f"HTTP {status} from {redact(url)}: {text[:800]}")
