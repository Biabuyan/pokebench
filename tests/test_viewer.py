"""`pokebench watch` — the live read-only dual-pane viewer (offline: no ROM/key).

`viewer.py` never imports `runner.loop` / `agents.*` / `harness.emulator` — it
only reads what `RunTracer` already wrote (`replay.load_run`) plus the
`turn_*.png` files sitting next to `run.jsonl`. This module tests the pure
`state_payload()` function and the stdlib HTTP server's three routes
end-to-end against a real (bound) socket, all against synthetic traces —
never a real run.
"""

from __future__ import annotations

import http.client
import json
import threading
from pathlib import Path

from PIL import Image

from pokebench.viewer import serve, state_payload
from tests.test_replay import _rec, _write_run


def test_state_payload_reports_turns_and_missing_screenshots(tmp_path):
    d = _write_run(
        tmp_path / "run1",
        [
            _rec(1, "aaa", 3, 6, executed=["up"], text="climbing the stairs"),
            _rec(2, "bbb", 7, 1, executed=["right"], success=True),
        ],
        meta={"model": "gemini", "tier": 0},
        summary={"success": True},
    )
    payload = state_payload(d)
    assert [t["turn"] for t in payload["turns"]] == [1, 2]
    assert payload["turns"][0]["model_text"] == "climbing the stairs"
    assert payload["has_screenshots"] is False
    assert payload["turns"][1]["success"] is True


def test_state_payload_since_turn_slices_to_new_turns_only(tmp_path):
    d = _write_run(
        tmp_path / "run1",
        [
            _rec(1, "aaa", 3, 6, executed=["up"]),
            _rec(2, "bbb", 4, 6, executed=["up"]),
            _rec(3, "ccc", 5, 6, executed=["up"]),
        ],
    )
    payload = state_payload(d, since_turn=1)
    assert [t["turn"] for t in payload["turns"]] == [2, 3]


def test_state_payload_detects_screenshots_and_reports_latest(tmp_path):
    d = _write_run(
        tmp_path / "run1",
        [_rec(1, "aaa", 3, 6, executed=["up"]), _rec(2, "bbb", 4, 6, executed=["up"])],
    )
    Image.new("RGB", (160, 144)).save(d / "turn_0001.png")
    Image.new("RGB", (160, 144)).save(d / "turn_0002.png")
    payload = state_payload(d)
    assert payload["has_screenshots"] is True
    assert payload["latest_screenshot"] == "turn_0002.png"


def test_state_payload_reads_run_jsonl_while_tracer_still_holds_it_open(tmp_path):
    """The one platform-specific assumption in the design: a reader can open
    and read `run.jsonl` on Windows while the writer (`RunTracer`) still has
    the same file open in append mode. Verified here with a synthetic writer
    rather than trusted."""
    d = tmp_path / "live_run"
    d.mkdir()
    (d / "meta.json").write_text(json.dumps({"model": "haiku", "tier": 0}), encoding="utf-8")
    writer = open(d / "run.jsonl", "a", encoding="utf-8")
    try:
        writer.write(json.dumps(_rec(1, "aaa", 3, 6, executed=["up"])) + "\n")
        writer.flush()

        payload = state_payload(d)  # read while writer's handle is still open
        assert [t["turn"] for t in payload["turns"]] == [1]

        writer.write(json.dumps(_rec(2, "bbb", 4, 6, executed=["up"])) + "\n")
        writer.flush()

        payload2 = state_payload(d)
        assert [t["turn"] for t in payload2["turns"]] == [1, 2]
    finally:
        writer.close()


def _free_port_server(run_dir: Path):
    httpd = serve(run_dir, port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, thread


def test_server_binds_localhost_only(tmp_path):
    d = _write_run(tmp_path / "run1", [_rec(1, "aaa", 3, 6, executed=["up"])])
    httpd, thread = _free_port_server(d)
    try:
        assert httpd.server_address[0] == "127.0.0.1"
    finally:
        httpd.shutdown()
        thread.join(timeout=5)


def test_server_routes_root_state_and_screenshot(tmp_path):
    d = _write_run(
        tmp_path / "run1",
        [_rec(1, "aaa", 3, 6, executed=["up"], text="hi"), _rec(2, "bbb", 4, 6, executed=["up"])],
        meta={"model": "gemini", "tier": 0},
    )
    Image.new("RGB", (160, 144)).save(d / "turn_0001.png")

    httpd, thread = _free_port_server(d)
    port = httpd.server_port
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)

        conn.request("GET", "/")
        resp = conn.getresponse()
        body = resp.read()
        assert resp.status == 200
        assert b"<html" in body.lower()

        conn.request("GET", "/api/state?since=0")
        resp = conn.getresponse()
        payload = json.loads(resp.read())
        assert resp.status == 200
        assert [t["turn"] for t in payload["turns"]] == [1, 2]

        conn.request("GET", "/shot/turn_0001.png")
        resp = conn.getresponse()
        assert resp.status == 200
        assert resp.getheader("Content-Type") == "image/png"
        assert len(resp.read()) > 0

        conn.close()
    finally:
        httpd.shutdown()
        thread.join(timeout=5)


def test_server_rejects_path_traversal_and_non_matching_filenames(tmp_path):
    d = _write_run(tmp_path / "run1", [_rec(1, "aaa", 3, 6, executed=["up"])])
    Image.new("RGB", (160, 144)).save(d / "turn_0001.png")
    secret = d.parent / "secret.txt"
    secret.write_text("do not serve me", encoding="utf-8")

    httpd, thread = _free_port_server(d)
    port = httpd.server_port
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)

        for path in ("/shot/..%2Fsecret.txt", "/shot/../secret.txt", "/shot/meta.json",
                     "/shot/notes.txt"):
            conn.request("GET", path)
            resp = conn.getresponse()
            resp.read()
            assert resp.status in (400, 404), path

        conn.close()
    finally:
        httpd.shutdown()
        thread.join(timeout=5)


def test_cli_watch_reports_missing_run_dir(tmp_path):
    from pokebench.cli import main

    rc = main(["watch", str(tmp_path / "nope"), "--no-open"])
    assert rc == 2


def test_cli_watch_starts_server_and_shuts_down_cleanly(tmp_path, monkeypatch):
    """Exercises the real CLI wiring (arg parsing, latest_run_dir fallback,
    server startup) without blocking forever: monkeypatch serve_forever to
    return immediately, as if the user hit Ctrl+C right away."""
    from pokebench.cli import main

    d = _write_run(tmp_path / "run1", [_rec(1, "aaa", 3, 6, executed=["up"])])

    from http.server import ThreadingHTTPServer

    original_forever = ThreadingHTTPServer.serve_forever
    calls = []

    def fake_forever(self):
        calls.append(self.server_address)
        self.server_close()

    monkeypatch.setattr(ThreadingHTTPServer, "serve_forever", fake_forever)
    try:
        rc = main(["watch", str(d), "--no-open", "--port", "0"])
    finally:
        monkeypatch.setattr(ThreadingHTTPServer, "serve_forever", original_forever)
    assert rc == 0
    assert calls and calls[0][0] == "127.0.0.1"


def test_state_payload_reports_finished_status_with_outcome(tmp_path):
    """A run directory with summary.json is 'finished' -- the viewer must show
    the outcome rather than leaving the left pane frozen with no explanation."""
    d = _write_run(
        tmp_path / "run1",
        [_rec(1, "aaa", 3, 6, executed=["up"]), _rec(2, "bbb", 7, 1, success=True)],
        meta={"model": "gemini", "tier": 0},
        summary={"success": True, "stop_reason": "success", "turns": 2, "usd": 0.05},
    )
    payload = state_payload(d)
    assert payload["status"]["phase"] == "finished"
    assert payload["status"]["outcome"] == {
        "success": True,
        "stop_reason": "success",
        "turns": 2,
        "usd": 0.05,
    }


def test_state_payload_reports_live_status_when_no_summary_and_recently_active(tmp_path):
    """No summary.json yet, and run.jsonl was just written -- still live."""
    d = _write_run(tmp_path / "run1", [_rec(1, "aaa", 3, 6, executed=["up"])])
    payload = state_payload(d)  # run.jsonl's mtime is "now"
    assert payload["status"]["phase"] == "live"


def test_state_payload_reports_stalled_status_when_no_summary_and_no_recent_turns(tmp_path):
    """No summary.json, and the last turn is old -- the run may have died
    without anyone noticing. `now` is injectable so this doesn't need a real
    sleep."""
    d = _write_run(tmp_path / "run1", [_rec(1, "aaa", 3, 6, executed=["up"])])
    stale_now = (d / "run.jsonl").stat().st_mtime + 3600
    payload = state_payload(d, now=stale_now, stall_seconds=30.0)
    assert payload["status"]["phase"] == "stalled"


def test_replay_mode_works_unmodified_on_a_finished_run(tmp_path):
    """Pointing `watch` at a finished run directory (no live writer, a
    summary.json present) must work identically -- state_payload doesn't care
    whether the run is still running."""
    d = _write_run(
        tmp_path / "finished",
        [_rec(1, "aaa", 3, 6, executed=["up"]), _rec(2, "bbb", 7, 1, success=True)],
        meta={"model": "haiku", "tier": 0},
        summary={"success": True, "stop_reason": "success", "turns": 2},
    )
    payload = state_payload(d)
    assert payload["summary"]["success"] is True
    assert len(payload["turns"]) == 2
