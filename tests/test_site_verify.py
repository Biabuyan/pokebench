"""`pokebench site verify` — the post-deploy guard.

Exists because of a real 22-day outage (2026-08-18 → 2026-09-09). `vercel --prod`
was run from the repo root instead of `web/dist`. The repo-root `.gitignore` has
`web/dist/`, so the CLI stripped the one directory holding the site, uploaded the
source tree instead, and reported success in 1s. `/` served a 404 and
`/pyproject.toml`, `/src/pokebench/cli.py` and three untracked working JSONs served
200. Nothing in the deploy procedure could tell that apart from a good deploy.

So the guard asserts BOTH halves of that incident: the site is actually there and
byte-identical to what was built locally, and the repo tree is NOT there. Offline
like the rest of the suite — `fetch` is injected, exactly as `agents/_http.py`'s
`JsonPoster` is injected so no adapter test needs a network.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pokebench.cli import main
from pokebench.site import MUST_NOT_PUBLISH, build_site, verify_deployment

BASE = "https://pokebench-snowy.vercel.app"


def _doc() -> dict:
    return {
        "schema": 5,
        "generated": "2026-08-16T01:48:55",
        "rows": [
            {
                "model": "gemini",
                "scenario": "s1_exit_pallet",
                "tier": 0,
                "cap_turns": 100,
                "seeds_valid": 3,
                "success_rate": 1.0,
            }
        ],
    }


def _built(tmp_path: Path) -> Path:
    """A real built site on disk — the thing a correct deploy uploads."""
    results = tmp_path / "results.json"
    results.write_text(json.dumps(_doc()), encoding="utf-8")
    out = tmp_path / "dist"
    build_site(results, out)
    return out


def _serving(dist: Path, *, override: dict | None = None):
    """A fake deployment that mirrors `dist`, with per-path overrides.

    `override` maps a site-relative path to a literal `(status, body)` — that is how
    each test injects one specific way a deploy can be wrong.
    """
    override = override or {}

    def fetch(url: str) -> tuple[int, bytes]:
        assert url.startswith(BASE), url
        rel = url[len(BASE) :].lstrip("/") or "index.html"
        if rel in override:
            return override[rel]
        target = dist / rel
        if target.is_file():
            return 200, target.read_bytes()
        return 404, b"The page could not be found"

    return fetch


def test_verify_passes_on_a_faithful_deployment(tmp_path: Path):
    dist = _built(tmp_path)
    report = verify_deployment(BASE, dist, fetch=_serving(dist))
    assert report.ok
    assert report.failures == []


def test_verify_catches_the_root_404_that_went_unnoticed_for_22_days(tmp_path: Path):
    # The exact 2026-08-18 signature: Vercel answers, deployment is READY, `/` is 404.
    dist = _built(tmp_path)
    fetch = _serving(dist, override={"index.html": (404, b"The page could not be found")})
    report = verify_deployment(BASE, dist, fetch=fetch)
    assert not report.ok
    assert any("404" in f and "/" in f for f in report.failures)


def test_verify_catches_a_200_that_serves_the_wrong_bytes(tmp_path: Path):
    # A stale deploy is worse than a down one: it looks fine. Status alone can't see it.
    dist = _built(tmp_path)
    fetch = _serving(dist, override={"index.html": (200, b"<!doctype html><title>old</title>")})
    report = verify_deployment(BASE, dist, fetch=fetch)
    assert not report.ok
    assert any("does not match" in f for f in report.failures)


@pytest.mark.parametrize("leaked", MUST_NOT_PUBLISH)
def test_verify_catches_a_repo_root_deploy_by_its_own_leaked_files(tmp_path: Path, leaked: str):
    # The other half of the incident. Every one of these serving 200 means the deploy
    # root was the repo, not web/dist.
    dist = _built(tmp_path)
    fetch = _serving(dist, override={leaked: (200, b"leaked")})
    report = verify_deployment(BASE, dist, fetch=fetch)
    assert not report.ok
    assert any(leaked in f for f in report.failures)


def test_verify_checks_replay_links_not_just_the_index(tmp_path: Path):
    # 79 replay pages are most of the site's value; index.html can be fine while the
    # runs/ tree failed to upload.
    results = tmp_path / "results.json"
    results.write_text(json.dumps(_doc()), encoding="utf-8")
    traces = tmp_path / "traces.txt"
    run = tmp_path / "runs" / "s1_exit_pallet-gemini-t0" / "20260101-000000" / "seed0"
    run.mkdir(parents=True)
    (run / "run.jsonl").write_text(
        json.dumps({"turn": 1, "action": {"buttons": ["up"]}}) + "\n", encoding="utf-8"
    )
    # `replay.load_run` reads meta from meta.json, not from a run.jsonl record --
    # build_site keys run_links off it, so without this the row gets no links at all.
    (run / "meta.json").write_text(
        json.dumps({"model": "gemini", "scenario": "s1_exit_pallet", "tier": 0}),
        encoding="utf-8",
    )
    traces.write_text(str(run) + "\n", encoding="utf-8")
    out = tmp_path / "dist"
    build_site(results, out, traces)

    link = next(iter(_links(out)))
    fetch = _serving(out, override={link: (404, b"gone")})
    report = verify_deployment(BASE, out, fetch=fetch)
    assert not report.ok
    assert any(link in f for f in report.failures)


def _links(dist: Path) -> list[str]:
    import re

    html = (dist / "index.html").read_text(encoding="utf-8")
    return sorted(set(re.findall(r'href="(runs/[^"]+)"', html)))


def test_verify_reports_how_much_it_actually_checked(tmp_path: Path):
    # A guard that silently checked nothing would be worse than none at all.
    dist = _built(tmp_path)
    report = verify_deployment(BASE, dist, fetch=_serving(dist))
    assert report.checked_paths >= 1 + len(MUST_NOT_PUBLISH)


def test_verify_never_touches_the_network_by_default_in_tests(tmp_path: Path):
    # Offline discipline: the default fetch is real, so a test that forgot to inject
    # one must fail loudly rather than dial out.
    dist = _built(tmp_path)

    def boom(url: str):
        raise AssertionError(f"network call escaped: {url}")

    report = verify_deployment(BASE, dist, fetch=_serving(dist))
    assert report.ok
    with pytest.raises(AssertionError):
        verify_deployment(BASE, dist, fetch=boom)


def test_cli_site_verify_exits_nonzero_when_the_deploy_is_broken(
    tmp_path: Path, monkeypatch, capsys
):
    dist = _built(tmp_path)
    fetch = _serving(dist, override={"index.html": (404, b"nope")})
    monkeypatch.setattr("pokebench.site._default_fetch", fetch)

    code = main(["site", "verify", "--url", BASE, "--dist", str(dist)])

    assert code == 1
    assert "FAIL" in capsys.readouterr().err


def test_cli_site_verify_exits_zero_when_the_deploy_is_good(tmp_path: Path, monkeypatch, capsys):
    dist = _built(tmp_path)
    monkeypatch.setattr("pokebench.site._default_fetch", _serving(dist))

    code = main(["site", "verify", "--url", BASE, "--dist", str(dist)])

    assert code == 0
    assert "OK" in capsys.readouterr().out


def test_cli_site_verify_fails_when_dist_was_never_built(tmp_path: Path, capsys):
    # Verifying against a directory with no index.html would otherwise "pass" by
    # comparing nothing to nothing.
    code = main(["site", "verify", "--url", BASE, "--dist", str(tmp_path / "nope")])
    assert code == 2
    assert "not found" in capsys.readouterr().err
