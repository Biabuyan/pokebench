# Egress-allowlisting proxy for the PokéBench sandbox.
#
# The agent container has no direct internet route; its only egress is through
# this proxy, which permits HTTPS CONNECT *only* to the hosts in `filter`
# (the Anthropic API). Everything else is denied. This is what stops the game
# container from ever becoming an exfiltration leg (SECURITY.md / LLM06).
FROM alpine:3.20

RUN apk add --no-cache tinyproxy

COPY tinyproxy.conf /etc/tinyproxy/tinyproxy.conf
COPY filter /etc/tinyproxy/filter

EXPOSE 8888
CMD ["tinyproxy", "-d", "-c", "/etc/tinyproxy/tinyproxy.conf"]
