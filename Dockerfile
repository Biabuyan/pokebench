# Headless PokéBench harness image.
#
# The ROM and save states are NEVER baked in — they are mounted read-only at
# runtime (see docker-compose.yml and .dockerignore). Runs as a non-root user.
# This container has no direct internet egress under docker-compose; its only
# outbound path is the allowlisting proxy (see docker/proxy/).
FROM python:3.12-slim

# libsdl2 for PyBoy. The window is "null" (headless), but the SDL2 runtime is
# loaded on import; the bundled pysdl2-dll usually suffices — this is insurance.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libsdl2-2.0-0 \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

WORKDIR /app
ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

# Install dependencies first for better layer caching. The project installs
# itself (hatchling), so it needs the metadata + sources present.
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY scenarios ./scenarios
RUN uv sync --frozen --no-dev

RUN useradd --create-home runner && chown -R runner /app
USER runner

# Subcommand is supplied at run time, e.g.
#   docker compose run --rm agent run --model haiku --tier 0
ENTRYPOINT ["uv", "run", "--frozen", "pokebench"]
