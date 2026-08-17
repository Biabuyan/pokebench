# Sandboxed runs (Docker)

> **Status: built and the egress control verified 2026-08-11** (Docker Engine 29.6.2,
> Compose v5.3.1) — `docker compose build && up` succeeds, and the "Verify" section below
> was run and passed: the allowlisted host is reachable through the proxy, a non-listed
> host is refused by the proxy (`403 Filtered`, confirmed in the proxy's own log), and a
> raw socket dialing out directly (bypassing `HTTPS_PROXY`) gets `Network is unreachable`
> — the `sandbox` network genuinely has no gateway, so this isn't merely an env-var
> convention an agent process could ignore. **What is still unverified:** no
> `pokebench run`/`bench`/`replay` has ever actually executed inside the container — only
> the manual `sh` probes below have. It is opt-in either way: a direct `pokebench run` on
> the host bypasses the sandbox entirely.

Runs the emulator + agent in a container with **no direct internet egress** —
the only outbound path is an allowlisting proxy that permits HTTPS **only** to
`api.anthropic.com`. This is the mitigation named in `SECURITY.md` (LLM06 /
Rule-of-Two): the game container can never become an exfiltration leg.

```
              docker-compose
   ┌────────────────────────────────────────────┐
   │  agent  ──(HTTPS_PROXY)──►  proxy  ──►  api.anthropic.com   ✅
   │  (sandbox net,             (sandbox +      (allowlisted)
   │   internal: true —          egress nets)
   │   no gateway out)   ──────────────────────►  anything else  ❌ denied
   └────────────────────────────────────────────┘
```

## Prerequisites

- Docker Desktop / Engine running.
- Your ROM at `roms/pokemon_red.gb` and the S1 anchor at
  `scenarios/states/s1_bedroom.state` (both stay on the host — mounted
  read-only, never baked into the image).
- `ANTHROPIC_API_KEY` in your shell (compose reads it from the environment):
  - PowerShell: `$env:ANTHROPIC_API_KEY = (Get-ItemProperty 'HKCU:\Environment' -Name ANTHROPIC_API_KEY).ANTHROPIC_API_KEY`
  - bash: `export ANTHROPIC_API_KEY=...`

## Build

```
docker compose build
```

## Run a scenario (sandboxed)

The `agent` entrypoint is the `pokebench` CLI, so pass a subcommand + flags:

```
docker compose run --rm agent run --scenario scenarios/s1_exit_pallet.yaml --model haiku --tier 0
```

Traces land in `./runs/` on the host (that directory is a writable mount).
Inspect one without any network:

```
docker compose run --rm agent replay
```

Stop the proxy when done:

```
docker compose down
```

## Verify the sandbox actually blocks egress

The agent should reach the Anthropic API and **nothing else**, and it should not be
able to get out any other way. Three checks, all runnable at zero cost (no API key,
no billed request) and all actually run on 2026-08-11 with the results noted below.

**1. Allow path** — an unauthenticated POST to `api.anthropic.com` costs nothing (the
API rejects it before billing) but proves the proxy actually bridges to the real host:

```
docker compose run --rm --entrypoint sh agent -c \
  "python3 - <<'PY'
import urllib.request as u
op = u.build_opener(u.ProxyHandler({'https': 'http://proxy:8888'}))
req = u.Request('https://api.anthropic.com/v1/messages', data=b'{}', method='POST',
                 headers={'content-type': 'application/json'})
try:
    op.open(req, timeout=15)
except u.HTTPError as e:
    print('reached endpoint:', e.code, e.read(200))
PY"
```

Observed: `reached endpoint: 401 b'{"type":"error","error":{"type":"authentication_error",
"message":"x-api-key header is required"}...'` — a real response from Anthropic, no key sent.

**2. Deny path** — a non-allowlisted host should be refused by the proxy (403), not by
absence of network (that would also "pass" a naive test while proving nothing):

```
docker compose run --rm --entrypoint sh agent -c \
  "python3 - <<'PY'
import urllib.request as u
op = u.build_opener(u.ProxyHandler({'https': 'http://proxy:8888'}))
try:
    op.open('https://example.com', timeout=8); print('REACHED example.com (BAD)')
except Exception as e:
    print('blocked as expected:', type(e).__name__, e)
PY"
```

Observed: `blocked as expected: URLError <urlopen error Tunnel connection failed: 403
Filtered>`, corroborated in `docker compose logs proxy`: `Proxying refused on filtered
domain "example.com"` right next to `Established connection to host "api.anthropic.com"`
for check 1 — the proxy is discriminating on hostname, not failing uniformly.
`api.openai.com` and `generativelanguage.googleapis.com` were checked the same way and
are also refused, confirming the gap noted in the Notes section below.

**3. Bypass path** — can a process in the container skip the proxy and dial out
directly? This is the check that distinguishes an *enforced* control from an
*advisory* one (an `HTTPS_PROXY` env var only helps if every process honors it):

```
docker compose run --rm --entrypoint sh agent -c \
  "python3 -c \"import socket; socket.create_connection(('1.1.1.1',443),timeout=5)\" || echo 'no direct egress (good)'
   python3 -c \"import socket; socket.create_connection(('8.8.8.8',53),timeout=5)\" || echo 'no direct egress (good)'"
```

Observed: both attempts raised `OSError: [Errno 101] Network is unreachable` — not a
connection refusal, a routing failure. `docker network inspect pokebench_sandbox` shows
`"Internal": true` with no gateway, so this is enforced by the network topology, not by
whether a given process chooses to respect `HTTPS_PROXY`.

Together: reachable + authenticated-response from Anthropic (1), refused-by-hostname
from the proxy for everyone else (2), and no route out that skips the proxy (3)
demonstrate the allowlist is both real and enforced. **Not covered by this:** an actual
`pokebench run`/`bench`/`replay` executing inside the container — these three checks use
`--entrypoint sh` to probe the network directly and never invoke the `pokebench` CLI.

## Notes

- Only Anthropic is allowlisted (`docker/proxy/filter`). Targeting a different
  endpoint (Bedrock/Vertex) means adding its host there — keep it minimal.
- CI does not build/run this image (it needs no ROM/key to test the harness);
  the sandbox is for local and deploy use.
