"""`pokebench watch` — a live, read-only dual-pane viewer for a running (or
finished) agent episode.

**Architecture: a separate process that only reads.** It never drives PyBoy,
never opens the ROM, and never imports `runner.loop` / `agents.*` /
`harness.emulator` — it reads exactly what `RunTracer` already writes
(`run.jsonl`, flushed every turn, plus `turn_NNNN.png` alongside it) via
`replay.load_run`, the same offline parser `cmd_replay`/`cmd_score`/`cmd_sweep`
already reuse. This is what makes it structurally impossible for watching a
run to perturb it: there is no code path here that can reach the emulator.

Do NOT add a `--window` flag to `run`/`bench`/`sweep` instead of this file —
those three hardcode `window=False` on purpose (`Emulator.__init__` ties the
window flag to emulation speed), and a windowed run ticking in real time could
trip `max_wall_seconds` in `runner/loop.py`. A run that dies on its wall-clock
cap because someone was watching it is a corrupted measurement. Keeping the
viewer out-of-process, read-only, and polling the filesystem is what makes
that impossible instead of merely avoided.

No new dependencies: `http.server`, `webbrowser`, `json`, `threading`, `re`
are all stdlib. The project's dependency set (pyboy, pillow, numpy, pyyaml,
anthropic) does not grow for this.
"""

from __future__ import annotations

import json
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from pokebench.replay import Run, load_run

# How long with no new turn (and no summary.json) before a run is "stalled"
# rather than "live". Polling is every 1s client-side, so 30s is ~30 missed
# polls -- long enough to ride out a slow model call, short enough that a
# genuinely dead run (crashed process, killed terminal) gets flagged well
# before a human staring at a frozen frame starts to wonder on their own.
STALL_SECONDS = 30.0

# `turn_0001.png` .. `turn_9999.png` — exactly what RunTracer.record_turn
# writes. The /shot/ route only ever serves a name matching this; there is no
# general file-serving code path, so path traversal (`..`, absolute paths,
# alternate separators) is rejected by construction, not by blocklist.
SCREENSHOT_NAME = re.compile(r"turn_\d{4}\.png")


def _turn_payload(record: dict) -> dict:
    """One trace record -> the fields the viewer's turn log renders.

    Field-for-field off `RunTracer.record_turn`'s own keys (see
    `runner/loop.py`), not re-derived: `model_text` uses the same falsy check
    as `replay.format_turn` (gpt's adapter can write an empty string, not
    None, for a turn with no reasoning text), and `executed`/`notes_update`
    are read the same way `replay._turn_bits` reads them.
    """
    action = record.get("action") or {}
    model = record.get("model") or {}
    text = (model.get("text") or "").strip()
    return {
        "turn": record.get("turn", 0),
        "model_text": text,
        "executed": action.get("executed") or [],
        "notes": record.get("notes"),  # full Tier-1 scratchpad; absent at Tier 0
        "notes_update": record.get("notes_update"),  # this turn's {chars, truncated}
        "state": record.get("state", {}),
        "cost_usd": record.get("cost_usd", 0.0),
        "cumulative_usd": record.get("cumulative", {}).get("usd", 0.0),
        "success": bool(record.get("success")),
    }


def _screenshot_names(run: Run) -> list[str]:
    return sorted(p.name for p in run.dir.glob("turn_*.png") if SCREENSHOT_NAME.fullmatch(p.name))


def _run_status(run: Run, now: float | None = None, stall_seconds: float = STALL_SECONDS) -> dict:
    """A finished run looks identical to a broken one if all you show is the
    last frame: nothing moves either way. Distinguish the three cases a human
    actually needs:

    - "finished": summary.json exists -- `RunTracer.finish()` only writes it
      once the loop has genuinely ended, so its presence is a strong signal
      (already loaded by `load_run`, so this costs no extra file I/O).
    - "live": no summary yet, and run.jsonl was touched recently -- a turn is
      still landing on the expected cadence.
    - "stalled": no summary yet, and run.jsonl has been quiet for
      `stall_seconds` -- the process may have died. This is the one case that
      needs the filesystem mtime rather than data already in `run`; `now` is
      injectable so tests don't need a real sleep.
    """
    if run.summary is not None:
        s = run.summary
        return {
            "phase": "finished",
            "outcome": {
                "success": bool(s.get("success")),
                "stop_reason": s.get("stop_reason"),
                "turns": s.get("turns"),
                "usd": s.get("usd"),
            },
        }
    jsonl = run.dir / "run.jsonl"
    try:
        mtime = jsonl.stat().st_mtime
    except OSError:
        # No turns recorded yet at all -- a run that just started, not one
        # that has gone quiet. Nothing to measure staleness against.
        return {"phase": "live"}
    now = time.time() if now is None else now
    if now - mtime > stall_seconds:
        return {"phase": "stalled"}
    return {"phase": "live"}


def state_payload(
    run_dir: str | Path,
    since_turn: int = 0,
    now: float | None = None,
    stall_seconds: float = STALL_SECONDS,
) -> dict:
    """Everything the viewer's `/api/state` route needs for one poll.

    `since_turn` lets the browser ask for only what's new — offline and pure,
    reusable by anything (this is the seam the failing test targets). `now`/
    `stall_seconds` exist only to make `_run_status`'s staleness check
    testable without a real sleep; callers (the HTTP route) never pass them.
    """
    run = load_run(run_dir)
    turns = [_turn_payload(r) for r in run.records if r.get("turn", 0) > since_turn]
    shots = _screenshot_names(run)
    return {
        "meta": run.meta,
        "summary": run.summary,
        "turns": turns,
        "has_screenshots": bool(shots),
        "latest_screenshot": shots[-1] if shots else None,
        "status": _run_status(run, now=now, stall_seconds=stall_seconds),
    }


def _make_handler(run_dir: Path) -> type[BaseHTTPRequestHandler]:
    run_dir = Path(run_dir)

    class Handler(BaseHTTPRequestHandler):
        # Quiet the default per-request access log (stdout noise on every
        # poll) without touching error reporting -- send_error/log_error are
        # untouched, so a real failure is still visible. This is UI hygiene,
        # not the stderr-filtering anti-pattern CLAUDE.md warns about: no
        # outcome ("this run succeeded") is decided anywhere near this method.
        def log_message(self, format: str, *args) -> None:  # noqa: A002
            pass

        def _send_json(self, payload: dict, status: int = 200) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 (stdlib method name)
            from urllib.parse import parse_qs, urlparse

            parsed = urlparse(self.path)
            path = parsed.path

            if path == "/":
                body = _PAGE_HTML.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if path == "/api/state":
                since = int(parse_qs(parsed.query).get("since", ["0"])[0])
                self._send_json(state_payload(run_dir, since_turn=since))
                return

            if path.startswith("/shot/"):
                name = path[len("/shot/") :]
                # Reject anything that isn't a bare filename matching the
                # trace's own naming scheme -- no directory separators, no
                # "..", nothing outside run_dir. This is a strict allowlist,
                # not sanitization: any rejection just 404s.
                if "/" in name or "\\" in name or not SCREENSHOT_NAME.fullmatch(name):
                    self.send_error(404, "not found")
                    return
                shot_path = run_dir / name
                if not shot_path.is_file():
                    self.send_error(404, "not found")
                    return
                body = shot_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            self.send_error(404, "not found")

    return Handler


def serve(run_dir: str | Path, port: int = 8765) -> ThreadingHTTPServer:
    """Build (but do not start) the viewer's HTTP server.

    Bound to 127.0.0.1 ONLY -- trace, screenshot, and cost data must never be
    reachable from the local network. Caller runs `.serve_forever()`; `port=0`
    lets the OS pick a free port (used by tests).
    """
    handler = _make_handler(Path(run_dir))
    return ThreadingHTTPServer(("127.0.0.1", port), handler)


# ~60 lines of plain JS polling /api/state; two flex panes (screenshot left,
# scrolling turn log right); no build step, no framework, no CDN fetch — the
# whole UI is this one string.
_PAGE_HTML = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>PokéBench watch</title>
<style>
  body { margin: 0; font-family: monospace; background: #111; color: #eee; }
  header { padding: 8px 12px; background: #222; border-bottom: 1px solid #444;
           display: flex; gap: 16px; flex-wrap: wrap; }
  #status.live { color: #9f9; }
  #status.finished { color: #7fd; }
  #status.stalled { color: #f77; font-weight: bold; }
  #error-strip { display: none; padding: 6px 12px; background: #511; color: #fdd;
                 border-bottom: 1px solid #944; white-space: pre-wrap; }
  #panes { display: flex; height: calc(100vh - 40px); }
  #left, #right { overflow: auto; }
  #left { flex: 0 0 50%; display: flex; align-items: center; justify-content: center;
          background: #000; }
  #left img { max-width: 100%; max-height: 100%; image-rendering: pixelated; }
  #placeholder { color: #666; }
  #right { flex: 1; padding: 8px 12px; }
  .turn { border-bottom: 1px solid #333; padding: 6px 0; }
  .turn .hdr { color: #7fd; }
  .turn .text { color: #ccc; white-space: pre-wrap; }
  .turn.success { background: #143214; }
  .turn.render-error .hdr { color: #f77; }
</style>
</head>
<body>
<header>
  <span id="model">model: -</span>
  <span id="scenario">scenario: -</span>
  <span id="tier">tier: -</span>
  <span id="turn">turn: -</span>
  <span id="cost">cost: -</span>
  <span id="status">status: -</span>
</header>
<div id="error-strip"></div>
<div id="panes">
  <div id="left"><div id="placeholder">no screenshots for this run</div></div>
  <div id="right"></div>
</div>
<script>
let sinceTurn = 0;
let latestTurn = 0;

function fmtState(s) {
  if (!s) return "";
  return (s.map_name || "?") + " (" + s.x + "," + s.y + ") face=" + (s.facing || "?");
}

function renderTurn(t) {
  const div = document.createElement("div");
  div.className = "turn" + (t.success ? " success" : "");
  const hdr = document.createElement("div");
  hdr.className = "hdr";
  const executed = (t.executed && t.executed.length) ? t.executed.join(",") : "no-op";
  hdr.textContent = "t" + t.turn + " [" + executed + "] -> "
    + fmtState(t.state) + "  $" + Number(t.cumulative_usd).toFixed(4)
    + (t.success ? "  <== SUCCESS" : "");
  div.appendChild(hdr);
  if (t.model_text) {
    const p = document.createElement("div");
    p.className = "text";
    p.textContent = t.model_text;
    div.appendChild(p);
  }
  document.getElementById("right").appendChild(div);
}

// A malformed record must not abort the batch: render it as a visible stub
// (so a human sees turn N happened, even if it couldn't be displayed
// properly) instead of throwing partway through the for-loop below and
// silently dropping every turn after it.
function renderTurnFallback(t, err) {
  const div = document.createElement("div");
  div.className = "turn render-error";
  const hdr = document.createElement("div");
  hdr.className = "hdr";
  const turnNum = (t && Number.isFinite(Number(t.turn))) ? t.turn : "?";
  hdr.textContent = "t" + turnNum + " [unrenderable record: " + err.message + "]";
  div.appendChild(hdr);
  document.getElementById("right").appendChild(div);
}

function showError(msg) {
  const strip = document.getElementById("error-strip");
  strip.textContent = "poll error: " + msg;
  strip.style.display = "block";
}

function clearError() {
  const strip = document.getElementById("error-strip");
  strip.style.display = "none";
  strip.textContent = "";
}

function renderStatus(status) {
  const el = document.getElementById("status");
  if (!status) {
    el.textContent = "status: -";
    el.className = "";
    return;
  }
  if (status.phase === "finished") {
    const o = status.outcome || {};
    const verdict = o.success ? "SUCCESS" : "STOPPED (" + (o.stop_reason ?? "?") + ")";
    el.textContent = "status: FINISHED — " + verdict + ", " + (o.turns ?? "?")
      + " turns, $" + (o.usd ?? "?");
    el.className = "finished";
  } else if (status.phase === "stalled") {
    el.textContent = "status: STALLED — no new turns recently, run may have died";
    el.className = "stalled";
  } else {
    el.textContent = "status: LIVE";
    el.className = "live";
  }
}

function poll() {
  fetch("/api/state?since=" + sinceTurn)
    .then(r => {
      if (!r.ok) throw new Error("HTTP " + r.status + " from /api/state");
      return r.json();
    })
    .then(data => {
      clearError();
      const m = data.meta || {};
      document.getElementById("model").textContent = "model: " + (m.model || "-");
      document.getElementById("scenario").textContent = "scenario: " + (m.scenario || "-");
      document.getElementById("tier").textContent = "tier: " + (m.tier ?? "-");
      renderStatus(data.status);

      for (const t of (data.turns || [])) {
        try {
          renderTurn(t);
        } catch (err) {
          // One bad record must not blank the pane or stop later turns from
          // rendering -- log it for a developer and show a visible stub for
          // a user, then keep going.
          console.error("failed to render turn", t, err);
          renderTurnFallback(t, err);
        }
        const turnNum = Number(t && t.turn);
        if (Number.isFinite(turnNum)) {
          sinceTurn = Math.max(sinceTurn, turnNum);
          latestTurn = sinceTurn;
          document.getElementById("turn").textContent = "turn: " + latestTurn;
        }
        if (t && typeof t.cumulative_usd === "number") {
          document.getElementById("cost").textContent = "cost: $" + t.cumulative_usd.toFixed(4);
        }
      }
      if (data.turns && data.turns.length) {
        document.getElementById("right").scrollTop = document.getElementById("right").scrollHeight;
      }

      const left = document.getElementById("left");
      if (data.has_screenshots && data.latest_screenshot) {
        let img = document.getElementById("shot");
        if (!img) {
          left.innerHTML = "";
          img = document.createElement("img");
          img.id = "shot";
          left.appendChild(img);
        }
        img.src = "/shot/" + data.latest_screenshot + "?t=" + Date.now();
      }
    })
    .catch(err => {
      // Visible in the page, not just the console -- a fetch failure, a
      // server restart, or a malformed top-level payload must be obvious to
      // someone looking only at the page, not just dev tools. The next
      // setInterval tick tries again, so a transient failure self-heals.
      showError(err.message);
    });
}

poll();
setInterval(poll, 1000);
</script>
</body>
</html>
"""
