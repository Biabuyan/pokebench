"""`pokebench site build` — a static leaderboard (`web/dist/index.html`) plus one
text-only replay page per published run (`web/dist/runs/<key>/index.html`).

**Stdlib only** (`html`, `json`, `shutil`, `pathlib`, `dataclasses`) — no Jinja2, no
framework, no build step, matching `viewer.py`'s established pattern. The dependency
set in `pyproject.toml` does not grow for this file.

**Render, do not recompute.** Every metric on a leaderboard row already comes out of
`metrics/results.py::aggregate` (medians, `seed_values`, `exclusions`, provenance
fields). This module's only job is to lay that data out honestly in HTML — it must
never derive a number `metrics/` did not already compute.

**Zero screenshots ship publicly, structurally, not by discipline.** `build_site`
copies exactly three files per run (`run.jsonl`, `meta.json`, `summary.json` — see
`_PUBLISHABLE_FILES`); nothing in this module ever opens, globs, or references a
`turn_*.png`, and `render_run_page` never emits an `<img>` tag or a `/shot/`-style
path. This is the M4 Stage 1/2 decision, not the M1 `SCREENSHOT_SCALE=3` /
`RunTracer.record_turn` contract — those stay untouched; this module simply never
reads what they wrote to disk.

**Every model-derived string is untrusted content.** `model.text` and the Tier-1
`notes` scratchpad are free text a model produced, and on a public static page that is
an HTML-injection vector. `viewer.py`'s client JS gets this for free via
`.textContent`; this module is server-side static generation, so there is no browser
DOM doing it for us — every such value is routed through `_esc()` (`html.escape`)
before it reaches an f-string. `tests/test_site.py` pins this with a synthetic
`<script>`/`onerror=` payload.
"""

from __future__ import annotations

import copy
import html
import json
import re
import shutil
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from pokebench.replay import load_run
from pokebench.viewer import state_payload

# Only these three files make a replay page renderable (`replay.load_run` reads no
# others) -- notably NOT `raw/` (the optional per-turn provider-response dump: real
# disk weight nobody reads server-side) and NEVER a `turn_*.png`.
_PUBLISHABLE_FILES = ("run.jsonl", "meta.json", "summary.json")

S3_SCENARIO_ID = "s3_viridian_forest"

# "S3 (0/15)... never solved, no scripted oracle" is a settled HANDOFF finding, not a
# guess made here -- see CLAUDE.md's M3 summary. A bare "0.0%" on that row would read
# as an ordinary loss instead of an unresolved question about the scenario itself.
#
# This sentence used to BE every S3 row's SUCCESS cell -- ~40 words wrapped to ~18
# lines in a ~140px-wide column, repeated once per S3 row, making each S3 row taller
# than the six rows above it combined. Same fix as `_model_cell`'s reasoning-off
# marker: `_success_cell` now renders only a compact `ceiling probe` marker with this
# text in its `title`, and `render_index` states it once in the legend beneath the
# table (see the `_success_cell`/legend-building code below) -- never as running
# prose inside a table cell.
CEILING_PROBE_NOTE = (
    "ceiling probe, not a ranking result — no scripted oracle exists for this "
    "scenario, the success predicate has never fired in any traced run, and "
    "winnability under the current anchor state is unverified."
)

# `reasoning_provenance == "off_unrecorded"` means the flag provably bound and was off
# (see `metrics/score.py::_reasoning_provenance`) -- distinct from "not_applicable"
# (the vendor ignores the flag, so its absence says nothing). Conflating the two would
# either falsely flag gpt/gemini rows or silently hide the one row that IS a caveat.
REASONING_OFF_NOTE = "reasoning OFF — not a like-for-like comparison"

# BOTH provenances mean "reasoning was off"; they differ only in how well the trace
# evidences it (`recorded_off` recorded the flag, `off_unrecorded` infers it from a
# provably-bound flag). Keying the marker on `off_unrecorded` alone inverted the
# disclosure on the live leaderboard (found 2026-08-17): sonnet's S1-S3 rows carried
# the caveat and its S4 row -- the one scenario it won, and the row with the STRONGER
# provenance -- did not, which reads as "the win was the like-for-like one." Excludes
# `not_applicable` (vendor ignores the flag, so its absence says nothing) and
# `recorded_on`/`unknown`.
_REASONING_OFF_PROVENANCES = frozenset({"off_unrecorded", "recorded_off"})

# The public repo -- linked from the collapsed provenance sections instead of inlining
# results_traces.txt's full text (see CLAUDE.md's 2026-08-16 site.py restructure: a
# raw file dump above the table pushed the leaderboard itself off the first screen).
# A plain anchor href, never fetched by this page -- doesn't touch the "no external
# requests" contract, which is about resources this page loads, not links a reader
# may choose to follow.
_TRACES_FILE_URL = "https://github.com/biabuyan/pokebench/blob/main/results_traces.txt"


def _esc(value) -> str:
    """`html.escape` every value before it reaches an f-string. Called on every
    interpolated value in this module, model-derived or not: it is free for the
    non-model values and it is the one thing that must never be skipped for the
    model-derived ones (see module docstring)."""
    return html.escape(str(value), quote=True)


def _fmt_num(v) -> str:
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


def _pct(v) -> str:
    """`0.9`/`None` -> `"90%"`/`"-"`. Only ever fed an already-computed rate from
    `metrics/` (see module docstring) -- never a fraction this module derives itself.
    """
    if not isinstance(v, (int, float)):
        return "-"
    return f"{v:.0%}"


# --- results.json row rendering -----------------------------------------------------

# The seed-strip minimum/maximum bar height, in px, inside a fixed 20px-tall strip --
# tuned so a single-tile difference (e.g. 1 vs 2) is still visibly a bar, not a sliver,
# while the tallest bar in any strip still fits the row.
_SEED_BAR_MIN_PX = 3
_SEED_BAR_MAX_PX = 18


def _spread_cell(median, values: list | None, successes: list | None = None) -> str:
    """The median, plus -- unless the seeds agree -- a per-seed strip: one small bar
    per seed, height scaled to that seed's value and colour keyed to whether that seed
    succeeded. This is the structural version of the sonnet-S1 finding (`tiles_explored`
    `[4, 18, 2]`): a median of 4 quoted alone has already misled a reader once (see
    CLAUDE.md) -- three unequal bars cannot be misread as a single number the way a
    bare median can. Degenerate series (`[7, 7, 7]`) render as a plain median: a strip
    over identical seeds would be noise, not honesty.

    `successes` is `seed_values["success"]` from the same row, positionally aligned
    with `values` when both come from `metrics/results.py::aggregate` (both are built
    from the same ordered list of valid seeds) -- passed through only to colour the
    bars, never to compute anything `metrics/` did not already compute.
    """
    label = "-" if median is None else _esc(_fmt_num(median))
    value_html = f'<span class="cell-value">{label}</span>'
    if not values:
        return value_html
    non_none = [v for v in values if v is not None]
    if len(non_none) < 2 or len(set(non_none)) <= 1:
        return value_html

    vmax = max(abs(v) for v in non_none) or 1
    aligned_successes = successes if successes and len(successes) == len(values) else None
    blocks = []
    labels = []
    for i, v in enumerate(values):
        if v is None:
            labels.append("n/a")
            blocks.append(
                f'<span class="seed-block seed-block--none" '
                f'style="height:{_SEED_BAR_MAX_PX}px" title="{_esc(f"seed {i}: n/a")}">'
                "</span>"
            )
            continue
        labels.append(_fmt_num(v))
        frac = abs(v) / vmax
        h = round(_SEED_BAR_MIN_PX + (_SEED_BAR_MAX_PX - _SEED_BAR_MIN_PX) * frac)
        outcome_cls = ""
        outcome_word = ""
        if aligned_successes is not None:
            if aligned_successes[i] is True:
                outcome_cls, outcome_word = " seed-block--ok", ", success"
            elif aligned_successes[i] is False:
                outcome_cls, outcome_word = " seed-block--fail", ", failed"
        title = _esc(f"seed {i}: {_fmt_num(v)}{outcome_word}")
        blocks.append(
            f'<span class="seed-block{outcome_cls}" style="height:{h}px" title="{title}">'
            "</span>"
        )
    aria = _esc(f"per-seed values: {', '.join(labels)}")
    strip = f'<span class="seed-strip" role="img" aria-label="{aria}">{"".join(blocks)}</span>'
    return f"{value_html}{strip}"


def _stop_reasons_cell(stop_reasons: dict | None) -> str:
    if not stop_reasons:
        return "-"
    return _esc(", ".join(f"{k}:{v}" for k, v in sorted(stop_reasons.items())))


def _exclusions_cell(exclusions: list[dict] | None) -> str:
    if not exclusions:
        return "-"
    items = []
    for e in exclusions:
        reason = _esc(e.get("reason", "?"))
        detail = _esc(e.get("detail", ""))
        items.append(f'<li class="caveat-marker"><strong>{reason}</strong>: {detail}</li>')
    return '<ul class="exclusions">' + "".join(items) + "</ul>"


def _run_link_anchor(text: str, link: dict) -> str:
    """One `<a>` whose visible text is short (a seed position or attempt number)
    and whose `title` carries the full `<timestamp>/seedN` label a reader would
    need to tell two runs of the same seed apart -- see `_run_links_cell`."""
    href = _esc(link.get("href", "#"))
    title = _esc(link.get("label", text))
    return f'<a href="{href}" title="{title}">{_esc(text)}</a>'


def _run_links_cell(links: list[dict] | None, seeds_valid: int | None = None) -> str:
    """Compact per-seed replay links -- `0 · 1 · 2`, not the full `<timestamp>/
    seedN` path repeated as the anchor's own visible text (identical within a
    row, since the timestamp never varies, and long enough on its own to wrap
    every row across two lines; see CLAUDE.md's 2026-08-16 density pass). The
    descriptive label stays recoverable via `title` on each anchor.

    `seeds_valid` (the row's own already-computed field, never re-derived here)
    decides how many of `links` render at full weight: a retried sweep leaves
    every attempt in results_traces.txt's active block, so a row can carry more
    links than seeds that actually contributed to its numbers -- sonnet's S4 row
    is 12 links against 3 valid seeds. The links beyond `seeds_valid` collapse
    behind a `<details>` disclosure instead of being enumerated as more bare
    digits at the same visual weight as the ones the row's numbers came from;
    each stays its own real link, just one click further away.
    """
    if not links:
        return "-"
    n = seeds_valid if isinstance(seeds_valid, int) and seeds_valid > 0 else len(links)
    primary, extra = links[:n], links[n:]

    primary_html = " · ".join(_run_link_anchor(str(i), link) for i, link in enumerate(primary))
    out = f'<span class="seed-links">{primary_html}</span>'
    if extra:
        extra_html = " · ".join(
            _run_link_anchor(f"attempt {i}", link) for i, link in enumerate(extra, start=1)
        )
        plural = "" if len(extra) == 1 else "s"
        out += (
            f'<details class="run-links-extra"><summary>+{len(extra)} more '
            f"attempt{plural}</summary>{extra_html}</details>"
        )
    return out


def _success_cell(row: dict) -> str:
    """`sonnet`'s S3 row gets `▲ ceiling probe` (the `▲` is the shared
    `.caveat-marker::before` glyph) plus the per-row seed count -- never a bare
    percentage (see `CEILING_PROBE_NOTE`'s comment: dressing an unfalsifiable
    predicate up as a score is exactly the "bare 0/15" framing this cell exists to
    replace). The full sentence used to BE this cell's text; it now lives only in
    this marker's `title` and in the legend `render_index` renders directly beneath
    the table when any row is S3 -- never as running prose inside the cell itself.
    The seed count stays inline (unlike the sentence, it genuinely varies row to
    row, so folding it into the once-stated legend would lose information).
    """
    valid = row.get("seeds_valid", row.get("seeds", 0))
    if row.get("scenario") == S3_SCENARIO_ID:
        return (
            f'<span class="caveat-marker ceiling-probe" title="{_esc(CEILING_PROBE_NOTE)}">'
            "ceiling probe</span>"
            f'<div class="caveat-detail">{_esc(valid)} seed(s) probed.</div>'
        )
    rate = row.get("success_rate", 0.0)
    out = f'<span class="cell-value">{rate:.0%}</span> <small>({_esc(valid)} seed(s))</small>'
    excluded = row.get("seeds_excluded") or 0
    if excluded:
        # A per-row exclusion count, visible at a glance next to the score it
        # qualifies -- not only in the exclusions column further right, which a
        # reader skimming success rates could miss entirely.
        plural = "" if excluded == 1 else "s"
        out += (
            f' <span class="caveat-marker excluded-badge">{excluded} seed{plural} excluded</span>'
        )
    return out


def _model_cell(row: dict) -> str:
    """`sonnet` -- or, for a reasoning-off row, `sonnet ▲ reasoning off` (the `▲`
    is the shared `.caveat-marker::before` glyph). The full caveat sentence used
    to BE this cell's text and set every row's height (see CLAUDE.md's 2026-08-16
    density pass); it now lives only in this marker's `title` and in the legend
    `render_index` renders directly beneath the table when any row carries it --
    never as running prose inside the cell itself.
    """
    model = _esc(row.get("model", "?"))
    if row.get("reasoning_provenance") in _REASONING_OFF_PROVENANCES:
        return (
            f'{model} <span class="caveat-marker reasoning-off" '
            f'title="{_esc(REASONING_OFF_NOTE)}">reasoning off</span>'
        )
    return model


def _render_row(row: dict) -> str:
    seed_values = row.get("seed_values") or {}
    successes = seed_values.get("success")
    cells = [
        _esc(row.get("scenario", "?")),
        _model_cell(row),
        _esc(row.get("tier", "?")),
        _success_cell(row),
        _stop_reasons_cell(row.get("stop_reasons")),
        _spread_cell(row.get("median_turns"), seed_values.get("turns"), successes),
        _spread_cell(row.get("median_cost_usd"), seed_values.get("cost_usd"), successes),
        _spread_cell(
            row.get("median_tiles_explored"), seed_values.get("tiles_explored"), successes
        ),
        _pct(row.get("median_idle_rate")),
        _exclusions_cell(row.get("exclusions")),
        _run_links_cell(row.get("run_links"), row.get("seeds_valid")),
    ]
    tds = "".join(f"<td>{c}</td>" for c in cells)
    return f"<tr>{tds}</tr>"


# --- the DMG identity ----------------------------------------------------------------
#
# Four greens, one escape hatch. `--ink`/`--mid`/`--leaf`/`--pale` are the Game Boy's
# own LCD ramp (darkest to lightest); `--bg`/`--panel` are the LCD substrate itself
# (the "off" pixel colour a real DMG screen sits on, panels one step lighter). Every
# rule in this file stays inside those six colours EXCEPT `--caveat` and its two
# derived tones -- the one hue a Game Boy could never display, reserved exclusively
# for the things this benchmark refuses to let a reader miss: the S3 ceiling-probe
# note, the reasoning-off marker, and exclusion counts/reasons. If a future change
# wants a new colour for something that is not a caveat, that is a sign the new thing
# should be reusing one of the four greens instead, not a sign to add a seventh
# colour -- see CLAUDE.md before doing either.
_SHARED_CSS = """
  :root {
    --ink: #0f380f;      /* darkest green -- primary text, borders, dark fills */
    --mid: #306230;      /* secondary green -- rules, muted fills, failed bars */
    --leaf: #8bac0f;     /* accent green -- headers, links, successful bars */
    --pale: #9bbc0f;     /* lightest green -- header text, hover fills */
    --bg: #c5cfa1;       /* page surface: the LCD's own off-pixel green */
    --panel: #d7dfb8;    /* one step lighter -- cards, table, code blocks */
    --caveat: #a8391c;   /* the one colour outside the DMG ramp -- see above */
    --caveat-panel: #e3c9ad;
    --focus: var(--ink);
  }

  * { box-sizing: border-box; }

  html { -webkit-text-size-adjust: 100%; }

  body {
    margin: 0;
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
    background: var(--bg);
    color: var(--ink);
    line-height: 1.5;
  }

  /* 84rem (not the original 76rem): with the density-pass fixes above, the
     table's real content width needs a bit more than 76rem gave it to stay
     single-line at 1280px -- 76rem left ~64px unused on each side at that
     width while REPLAYS' last link (and, on a reasoning-off row, MODEL) still
     wrapped/clipped. 84rem uses the full 1280px viewport instead of wasting
     it, without ballooning line-length badly on very large screens. */
  main, header, footer { max-width: 84rem; margin: 0 auto; padding: 0 1rem; }

  a { color: var(--ink); text-decoration-thickness: 1px; text-underline-offset: 2px; }
  a:hover { color: var(--caveat); }
  a:focus-visible, button:focus-visible, summary:focus-visible {
    outline: 3px solid var(--focus); outline-offset: 2px;
  }
  @media (prefers-reduced-motion: no-preference) {
    a, .seed-block { transition: color 120ms ease, background-color 120ms ease; }
  }

  header.masthead {
    background: var(--ink); color: var(--pale);
    padding: 1.25rem 1rem; margin-bottom: 1.5rem;
  }
  header.masthead > div { max-width: 84rem; margin: 0 auto; }
  header.masthead h1 {
    margin: 0; font-size: 1.35rem; font-weight: 700;
    letter-spacing: 0.12em; text-transform: uppercase;
  }
  header.masthead .meta { margin: 0.35rem 0 0; color: var(--leaf); font-size: 0.85rem; }
  header.masthead .lede { margin: 0.6rem 0 0; color: var(--pale); font-size: 1rem;
                           font-weight: 600; }
  .back-link { display: inline-block; margin: 1rem 0 0.25rem; }

  h1 { font-size: 1.3rem; letter-spacing: 0.06em; }
  h2 {
    font-size: 0.95rem; text-transform: uppercase; letter-spacing: 0.08em;
    color: var(--ink); border-bottom: 1px solid var(--mid); padding-bottom: 0.35rem;
    margin: 2rem 0 0.75rem;
  }

  section.panel, .panel {
    background: var(--panel); border: 1px solid var(--mid);
    border-radius: 2px; padding: 1rem 1.1rem; margin: 1rem 0;
  }

  /* -- provenance sections: below the table, closed by default (see CLAUDE.md's
     2026-08-16 site.py restructure) -- the count/heading in <summary> is the
     one-line teaser, full content is one click away, never above the table. */
  details.panel > summary {
    cursor: pointer; list-style-position: outside;
  }
  details.panel > summary h2 {
    display: inline; margin: 0; border-bottom: none; padding-bottom: 0;
  }
  details.panel[open] > summary { margin-bottom: 0.75rem; }
  .source-link { font-size: 0.85rem; opacity: 0.85; margin-top: 0.6rem; }

  /* -- the legend directly beneath the table (never inside a closed <details>:
     see CLAUDE.md's density-pass note) explaining what a caveat marker means,
     for the rows that carry one. */
  .legend { font-size: 0.85rem; margin: 0.6rem 0 0; }

  pre {
    background: var(--panel); border: 1px solid var(--mid); border-radius: 2px;
    padding: 0.7rem 0.9rem; overflow-x: auto; white-space: pre-wrap;
    font-size: 0.85rem; margin: 0.5rem 0 0;
  }
  code { background: var(--panel); padding: 0 0.2rem; }

  .table-scroll { overflow-x: auto; border: 1px solid var(--mid); border-radius: 2px; }
  table { border-collapse: collapse; width: 100%; min-width: 62rem; background: var(--panel); }
  th, td { border: 1px solid var(--mid); padding: 0.5rem 0.65rem; text-align: left;
           vertical-align: top; font-size: 0.88rem; }
  th {
    background: var(--ink); color: var(--pale);
    text-transform: uppercase; letter-spacing: 0.06em; font-size: 0.72rem;
    position: sticky; top: 0;
  }
  tbody tr:hover { background: var(--pale); }
  .cell-value { font-weight: 700; }

  /* -- MODEL is the one column table-layout:auto still squeezes below its own
     preferred width (the wide free-text columns -- SUCCESS's S3 ceiling-probe
     paragraph, STOP REASONS -- win the balancing act otherwise), wrapping the
     now-short reasoning-off marker anyway. Every other column already fits on
     one line once 1 and 2 are fixed, so this is the one exception, not a
     blanket rule -- see CLAUDE.md's density-pass note. */
  td:nth-child(2) { white-space: nowrap; }

  /* -- the seed strip: one bar per seed, height by value, colour by outcome -- */
  .seed-strip {
    display: inline-flex; align-items: flex-end; gap: 2px;
    height: 18px; margin-left: 0.5rem; vertical-align: -4px;
  }
  .seed-block { width: 6px; background: var(--mid); display: inline-block; }
  .seed-block--ok { background: var(--leaf); }
  .seed-block--fail { background: var(--ink); }
  .seed-block--none { background: transparent; border: 1px dashed var(--mid); }

  /* -- replay links: `0 · 1 · 2`, not a wrapped `<timestamp>/seedN` path per
     link (see CLAUDE.md's density pass) -- the full label lives in `title`. */
  .seed-links { white-space: nowrap; }
  .seed-links a { text-decoration-style: dotted; }
  .run-links-extra {
    display: inline-block; margin-left: 0.4rem; font-size: 0.82em; opacity: 0.8;
    white-space: normal; /* the REPLAYS column is nowrap; the expanded extras
                             list (up to 9 attempts) should still be free to
                             wrap rather than force the row wider still. */
  }
  .run-links-extra summary { cursor: pointer; }
  .run-links-extra[open] summary { margin-bottom: 0.2rem; }

  /* -- the caveat system: the ONE place colour escapes the four greens -- */
  .caveat-marker { color: var(--caveat); }
  .caveat-marker::before { content: "\\25B2  "; font-size: 0.85em; } /* ▲ */
  .caveat { border-left: 3px solid var(--caveat); background: var(--caveat-panel);
            padding: 0.5rem 0.7rem; border-radius: 2px; }
  .caveat strong { color: var(--caveat); }
  .caveat-detail { color: var(--ink); font-size: 0.85em; opacity: 0.8; margin-top: 0.2rem; }
  .reasoning-off { font-weight: 700; }
  .ceiling-probe { font-weight: 700; }
  .excluded-badge { font-size: 0.82em; white-space: nowrap; }
  #exclusions-curated {
    border-left: 3px solid var(--caveat); background: var(--caveat-panel);
  }
  .superseded-note { color: var(--ink); opacity: 0.75; }
  ul.exclusions { margin: 0; padding-left: 1.1rem; }
  ul.exclusions li { margin: 0.2rem 0; }
  ul.exclusions li strong { color: var(--caveat); }

  /* -- one structured row per EXCLUDED-block reason, not a <pre> of the source -- */
  .exclusion-entries { list-style: none; margin: 0.75rem 0; padding: 0;
                        display: flex; flex-direction: column; gap: 0.6rem; }
  .exclusion-entry details { margin-top: 0.4rem; font-size: 0.8em; }
  .exclusion-entry details summary { cursor: pointer; }
  .exclusion-entry pre { margin-top: 0.3rem; font-size: 0.85em; }

  footer { margin: 2rem auto 2.5rem; font-size: 0.8rem; color: var(--ink); opacity: 0.7; }

  /* -- replay pages: the mock LCD screen where a screenshot would be -- */
  .screen {
    background: var(--ink); color: var(--pale); border: 1px solid var(--ink);
    outline: 4px solid var(--mid); outline-offset: -4px;
    padding: 1.25rem 1.4rem; margin: 1rem 0 1.5rem; image-rendering: pixelated;
  }
  .screen p { margin: 0; font-size: 0.88rem; }
  .screen code { background: var(--mid); color: var(--pale); }

  .turn {
    border-bottom: 1px solid var(--mid); padding: 0.6rem 0; font-size: 0.88rem;
  }
  .turn .hdr { color: var(--ink); font-weight: 700; }
  .turn .text, .turn .notes { color: var(--ink); opacity: 0.85; white-space: pre-wrap;
                               margin-top: 0.15rem; }
  .turn.success { background: var(--leaf); }
  .turn.success .hdr, .turn.success .text, .turn.success .notes { color: var(--ink); opacity: 1; }

  @media (max-width: 640px) {
    header.masthead h1 { font-size: 1.1rem; }
    main, header, footer { padding: 0 0.6rem; }
    th, td { padding: 0.4rem 0.5rem; font-size: 0.8rem; }
  }
"""


# --- top-level commentary rendering: the table leads, this collapses below it -------
#
# 2026-08-16: the leaderboard's whole first screen used to be provenance prose --
# results_traces.txt's intro and EXCLUDED block, dumped through a `<pre>`, before a
# reader ever saw a row of the table. Fixed by moving the table first (see
# `render_index`) and, here, by no longer inlining the source file's full text at
# all: `_extract_commands` pulls just the runnable command(s) out of the intro
# block, and `_exclusion_entry_row` renders each EXCLUDED-block reason as its own
# row instead of the whole block as one opaque blob. Both sections link to
# `_TRACES_FILE_URL` for the complete prose rather than reproducing it.


def _summary_line(rows: list[dict]) -> str:
    """One true sentence about what the table contains -- the "stronger opening"
    the masthead was missing, sourced from data already sitting on each row
    (identity fields plus `seeds_valid`), not a new benchmark metric. Counting how
    many distinct models/scenarios appear and summing an existing per-row field is
    presentational, the same kind of text-shape counting `_count_excluded` already
    does in this module -- it is not `metrics/` work happening twice.

    **The model count is split by `cap_turns` on purpose.** A bare count of distinct
    models reads "6", while every comparative claim -- README, blog post, CV -- reads
    "5", because `haiku`'s rows are the earlier fixed-turn-budget milestone
    (`cap_turns` 300/400/600) and the head-to-head set is turn-matched at 100.
    Reporting one number for both invites a reader who checks to conclude the project
    contradicts itself, which is the opposite of what a leaderboard whose whole premise
    is "the model is the only variable" can afford. The rule this encodes is stated in
    `CLAUDE.md` ("Do not mix the haiku rows into the M3 table ... Filter on
    `cap_turns`") and in `blog/the-harness-giveth.md` ("kept in the artifact and
    excluded from the head-to-head table deliberately").
    """
    if not rows:
        return ""
    models = {r.get("model") for r in rows if r.get("model")}
    scenarios = {r.get("scenario") for r in rows if r.get("scenario")}
    valid_seeds = sum(r.get("seeds_valid") or 0 for r in rows)
    # The model count is split by `cap_turns` deliberately -- see this function's
    # docstring. Derived from the rows, never hardcoded: the comparison cap is
    # whichever `cap_turns` the most rows share, and any model with no row at that
    # cap is reported separately as a baseline rather than folded into the headline.
    caps = Counter(r.get("cap_turns") for r in rows if r.get("cap_turns") is not None)
    matched: set = set()
    if caps:
        main_cap = caps.most_common(1)[0][0]
        matched = {
            r.get("model")
            for r in rows
            if r.get("cap_turns") == main_cap and r.get("model")
        }
    off_cap = models - matched
    if matched:
        head = (
            f"{len(matched)} turn-matched model{'s' if len(matched) != 1 else ''} × "
            f"{len(scenarios)} scenario{'s' if len(scenarios) != 1 else ''}"
        )
    else:
        head = (
            f"{len(models)} model{'s' if len(models) != 1 else ''} × "
            f"{len(scenarios)} scenario{'s' if len(scenarios) != 1 else ''}"
        )
    if off_cap:
        head += (
            f", plus {len(off_cap)} baseline model"
            f"{'s' if len(off_cap) != 1 else ''} on an earlier turn budget"
        )
    return (
        f"{head}: {len(rows)} scored cell{'s' if len(rows) != 1 else ''} from "
        f"{valid_seeds} valid seed-run{'s' if valid_seeds != 1 else ''} — read the "
        "caveat beside any row that isn't a bare score."
    )


def _extract_commands(text: str) -> list[str]:
    """The runnable `pokebench ...` command line(s) inside a reproduce-note block
    (backslash-continuation lines joined), and nothing else -- not the surrounding
    rationale paragraphs. Lets the leaderboard show a real code block instead of
    results_traces.txt's whole multi-paragraph intro through a `<pre>`.
    """
    commands: list[str] = []
    current: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if current:
            current.append(line)
            if line.endswith("\\"):
                continue
            commands.append("\n".join(current))
            current = []
            continue
        if line.startswith("python -m uv run pokebench"):
            if line.endswith("\\"):
                current = [line]
            else:
                commands.append(line)
    if current:
        commands.append("\n".join(current))
    return commands


def _exclusion_entry_row(entry: dict) -> str:
    """One structured `<li>` per EXCLUDED-block comment group -- reason text plus
    (collapsed further, since these are file paths, not the reason itself) the
    trace directories it covers. Reuses `.caveat` styling, the one hue this module
    reserves for things a reader must not miss (see the DMG-identity CSS comment).
    """
    count = entry.get("count") or len(entry.get("paths") or [])
    plural = "" if count == 1 else "s"
    reason = _esc(entry.get("reason", ""))
    paths = entry.get("paths") or []
    paths_html = ""
    if paths:
        n = len(paths)
        lines = "\n".join(_esc(p) for p in paths)
        paths_html = (
            f"<details><summary>{n} run director{'y' if n == 1 else 'ies'}</summary>"
            f"<pre>{lines}</pre></details>"
        )
    return (
        '<li class="exclusion-entry caveat">'
        f"<strong>{count} seed{plural} excluded</strong> — {reason}"
        f"{paths_html}"
        "</li>"
    )


def render_index(results_doc: dict) -> str:
    """One HTML table from `results.json`'s rows (schema 3+), with the table as the
    page's hero -- visible immediately under the masthead, no scrolling required.

    `results_doc` is exactly `results.json`'s shape, plus five optional keys a
    caller may layer on top -- `reproduce_note`, `exclusion_note`,
    `exclusion_entries`, `exclusion_seed_count` and `superseded_group_count` (all
    sourced from `results_traces.txt` by `build_site`, never authored here), and
    `run_links` on individual rows. None of the required fields, and none of the
    optional ones, are recomputed: they are read, reshaped for display (see
    `_extract_commands`/`_exclusion_entry_row`), and escaped.

    2026-08-16 restructure: the table used to render AFTER two provenance panels
    that dumped results_traces.txt's intro and EXCLUDED block through raw `<pre>`
    tags -- long enough that the table itself never appeared in the first ~2200px
    of the page. Fixed two ways: the table now renders first, and the provenance
    panels are `<details>` (closed by default, one click to expand) holding only
    the essentials (a real command, structured exclusion reasons) plus a link to
    the source file instead of its full text. Row-level honesty -- the S3
    ceiling-probe caveat, the reasoning-off marker, seed-spread strips, per-row
    exclusion badges -- is untouched: it lives inside `_render_row` and stays
    exactly as prominent as before.
    """
    rows = results_doc.get("rows", [])
    generated = _esc(results_doc.get("generated") or "?")
    schema = _esc(results_doc.get("schema", "?"))
    lede = _esc(_summary_line(rows))

    sections = []
    reproduce_note = results_doc.get("reproduce_note")
    if reproduce_note:
        commands = _extract_commands(reproduce_note)
        commands_html = "".join(f"<pre><code>{_esc(c)}</code></pre>" for c in commands)
        sections.append(
            '<details class="panel" id="reproduce">'
            "<summary><h2>How this table was produced / reproduce it offline</h2>"
            "</summary>"
            "<p>No ROM, network, or API key required:</p>"
            f"{commands_html}"
            '<p class="source-link">Full provenance, schema history, and '
            f'regeneration notes: <a href="{_esc(_TRACES_FILE_URL)}">'
            "results_traces.txt</a> in the repo.</p>"
            "</details>"
        )
    exclusion_note = results_doc.get("exclusion_note")
    exclusion_entries = results_doc.get("exclusion_entries")
    if exclusion_note or exclusion_entries:
        count = results_doc.get("exclusion_seed_count")
        group_count = results_doc.get("superseded_group_count")
        heading = (
            f"{count} seeds were excluded during curation, here's why"
            if count is not None
            else "Seeds excluded during curation, here's why"
        )
        # Data-driven, not "these 9": a hand-typed number here is exactly what let
        # this sentence and _count_excluded's real return value silently disagree
        # before this fix (12 vs. 9, see CLAUDE.md).
        count_phrase = f"these {_esc(count)}" if count is not None else "these"
        seed_entries = [e for e in (exclusion_entries or []) if e.get("kind") == "seed"]
        if seed_entries:
            # The structured path: one row per reason, not the whole block as a
            # single opaque blob.
            body_html = '<ul class="exclusion-entries">' + "".join(
                _exclusion_entry_row(e) for e in seed_entries
            ) + "</ul>"
        elif exclusion_note:
            # Fallback for a caller that supplied exclusion_note directly without
            # going through parse_traces_commentary's structured entries (e.g. a
            # hand-built doc) -- still collapsed, never above the table.
            body_html = f"<pre>{_esc(exclusion_note)}</pre>"
        else:
            body_html = ""
        # Rendered as a SEPARATE paragraph, not folded into the heading/count above,
        # because the two are different units: seeds judged invalid evidence vs.
        # whole run groups that were superseded (replaced, never judged invalid).
        # Conflating them is the bug this section exists to not reintroduce.
        superseded_html = ""
        if group_count:
            superseded_html = (
                '<p class="superseded-note">Separately, '
                f"<strong>{_esc(group_count)}</strong> earlier single-seed run "
                "group(s) were superseded by the later M2 sweep -- replaced, not "
                "invalid evidence; not counted in the total above.</p>"
            )
        sections.append(
            '<details class="panel" id="exclusions-curated">'
            f'<summary><h2 class="caveat-marker">{_esc(heading)}</h2></summary>'
            "<p>Every row below already reports <code>seeds_excluded</code> per its own "
            f"(model, scenario, tier) group -- {count_phrase} seeds were curated out as "
            "invalid evidence before scoring even ran, so no row's own exclusion count "
            "reflects them.</p>"
            f"{body_html}"
            f"{superseded_html}"
            '<p class="source-link">Full provenance, verbatim: '
            f'<a href="{_esc(_TRACES_FILE_URL)}">results_traces.txt</a> in the repo.</p>'
            "</details>"
        )

    row_html = "\n".join(_render_row(r) for r in rows)
    sections_html = "\n".join(sections)

    # A legend directly beneath the table, uncollapsed -- each paragraph only
    # rendered when at least one row actually carries that marker, so a table with
    # neither reasoning-off nor S3 rows mentions neither (pinned by
    # test_reasoning_off_legend_absent_when_no_row_has_the_marker and
    # test_s3_ceiling_probe_legend_absent_when_no_s3_row). This is what keeps both
    # caveats discoverable without a click, now that neither `_model_cell` nor
    # `_success_cell` spells its full sentence out in every marked row's own cell.
    legend_parts = []
    if any(r.get("reasoning_provenance") == "off_unrecorded" for r in rows):
        legend_parts.append(
            '<p class="legend"><span class="caveat-marker reasoning-off">'
            f'reasoning off</span> beside a model name means {_esc(REASONING_OFF_NOTE)} '
            "for that row.</p>"
        )
    if any(r.get("scenario") == S3_SCENARIO_ID for r in rows):
        legend_parts.append(
            '<p class="legend"><span class="caveat-marker ceiling-probe">ceiling probe'
            f'</span> in the success column of every <code>{_esc(S3_SCENARIO_ID)}</code> '
            f"row means {_esc(CEILING_PROBE_NOTE)}</p>"
        )
    legend_html = "\n".join(legend_parts)

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PokéBench leaderboard</title>
<style>{_SHARED_CSS}</style>
</head>
<body>
<header class="masthead"><div>
<h1>PokéBench leaderboard</h1>
<p class="meta">schema {schema} · generated {generated}</p>
<p class="lede">{lede}</p>
</div></header>
<main>
<h2>Results</h2>
<div class="table-scroll">
<table>
<thead><tr>
<th>scenario</th><th>model</th><th>tier</th><th>success</th><th>stop reasons</th>
<th>turns</th><th>cost ($)</th><th>tiles explored</th><th>idle rate</th>
<th>exclusions (this row)</th><th>replays</th>
</tr></thead>
<tbody>
{row_html}
</tbody>
</table>
</div>
{legend_html}
{sections_html}
</main>
<footer>Stdlib-generated static page, no tracking, no external requests. Bars above are a
per-seed strip: height by value, colour by outcome (lighter = success). Anything coloured
outside the four Game Boy greens is a caveat -- read it.</footer>
</body>
</html>
"""


# --- per-run text replay pages --------------------------------------------------


def _back_to_index_href(run_key: str) -> str:
    """`web/dist/runs/<run_key>/index.html` back to `web/dist/index.html`. `run_key`
    may itself contain slashes (it mirrors the source trace directory structure), so
    the number of `../` hops depends on its depth, not a fixed constant."""
    ups = run_key.count("/") + 2  # +1 per extra path segment, +1 for the "runs/" dir
    return "../" * ups + "index.html"


def _render_turn(turn: dict) -> str:
    executed = ", ".join(turn.get("executed") or []) or "no-op"
    state = turn.get("state") or {}
    state_str = (
        f"{state.get('map_name', '?')} ({state.get('x', '?')},{state.get('y', '?')}) "
        f"face={state.get('facing', '?')}"
    )
    cumulative = turn.get("cumulative_usd", 0.0) or 0.0
    hdr = (
        f"t{_esc(turn.get('turn', 0))} [{_esc(executed)}] -&gt; {_esc(state_str)} "
        f"${cumulative:.4f}"
    )
    if turn.get("success"):
        hdr += "  &lt;== SUCCESS"

    css_class = "turn success" if turn.get("success") else "turn"
    body = f'<div class="{css_class}"><div class="hdr">{hdr}</div>'

    text = (turn.get("model_text") or "").strip()
    if text:
        body += f'<div class="text">{_esc(text)}</div>'

    notes = turn.get("notes")
    if notes:
        body += f'<div class="notes"><strong>notes:</strong> {_esc(notes)}</div>'

    body += "</div>"
    return body


def render_run_page(run_dir: str | Path, run_key: str) -> str:
    """One run's turn log as static text -- built on `viewer.state_payload`
    (`replay.load_run` underneath it), the exact same per-turn shape `pokebench
    watch` renders client-side. The screenshot pane is replaced unconditionally: this
    function never reads `has_screenshots`/`latest_screenshot` from the payload and
    never emits an `<img>` tag, regardless of what is sitting on disk next to
    `run_dir` (see module docstring and `test_render_run_page_never_references_
    real_screenshots_on_disk`).
    """
    payload = state_payload(run_dir)
    meta = payload.get("meta") or {}
    summary = payload.get("summary")
    turns = payload.get("turns") or []

    status_line = "status: unfinished (no summary.json in this trace)"
    if summary is not None:
        verdict = "SUCCESS" if summary.get("success") else f"STOPPED ({summary.get('stop_reason')})"
        status_line = (
            f"status: {verdict} in {summary.get('turns', '?')} turns, "
            f"${summary.get('usd', '?')}, {summary.get('wall_seconds', '?')}s wall clock"
        )

    turn_html = "\n".join(_render_turn(t) for t in turns)
    back_href = _back_to_index_href(run_key)

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PokéBench replay — {_esc(run_key)}</title>
<style>{_SHARED_CSS}</style>
</head>
<body>
<header class="masthead"><div>
<h1>{_esc(run_key)}</h1>
<p class="meta">
model: {_esc(meta.get('model', '?'))} ·
scenario: {_esc(meta.get('scenario', '?'))} ·
tier: {_esc(meta.get('tier', '?'))} ·
{_esc(status_line)}
</p>
</div></header>
<main>
<p class="back-link"><a href="{back_href}">&larr; back to leaderboard</a></p>
<div class="screen no-shots">
<p>No screenshots are published on this site: Pokémon Red frames are not
redistributed here, the same "bring your own, hash-verified ROM; we ship
nothing derived from it" policy that keeps the ROM itself out of this repo.
Run <code>pokebench watch &lt;run-dir&gt;</code> locally, against your own ROM, to see
this run's screen alongside its reasoning.</p>
</div>
<h2>Turn log</h2>
<div class="turns">
{turn_html}
</div>
</main>
<footer>Stdlib-generated static page, no tracking, no external requests.</footer>
</body>
</html>
"""


# --- results_traces.txt parsing --------------------------------------------------


def _strip_comment_block(lines: list[str]) -> str:
    """`# foo` -> `foo`, blank comment lines kept as paragraph breaks, leading/
    trailing blank lines trimmed. Applied to a slice of `results_traces.txt`'s own
    lines -- this never authors new prose, only reformats the file's existing
    comments for display."""
    out = []
    for ln in lines:
        s = ln.strip()
        if s == "#" or s == "":
            out.append("")
            continue
        if s.startswith("#"):
            s = s[1:]
            if s.startswith(" "):
                s = s[1:]
        out.append(s)
    while out and out[0] == "":
        out.pop(0)
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out)


def _brace_expand_count(path_str: str) -> int:
    """1, or the item count of a shell-style brace list (`seed{0,1,2}` -> 3,
    `{152600,153859}` -> 2) -- the same way a human reading `results_traces.txt`
    would count entries packed onto one line."""
    if "{" in path_str and "}" in path_str:
        inside = path_str[path_str.index("{") + 1 : path_str.index("}")]
        return len([p for p in inside.split(",") if p.strip()])
    return 1


def _classify_excluded_line(path_str: str) -> str:
    """'seed' for a line naming an individual excluded seed run (`seed0`,
    `seed{0,1,2}`, or an underscore-prefixed variant like
    `_aborted_outputcap_seed0`) -- these are the 9 audited-invalid seeds. 'group'
    for a line naming a whole pre-seed bench-run directory with no seed component
    at all (e.g. `.../20260719-{152600,153859}`) -- the 3 superseded pre-M2 haiku
    run groups, which were replaced, not judged invalid.

    Structural, not textual, on purpose: this does not look for the "Superseded"
    comment wording next to those lines (see `results_traces.txt`), because prose
    can be reworded or a paragraph reordered without anyone touching this parser.
    It looks at the path shape instead, which is guaranteed by how bench output is
    actually laid out on disk -- a bench run either has `seedN` subdirectories or
    it predates seed-level runs entirely and doesn't. That invariant is much harder
    to break by accident than a comment header's exact wording.
    """
    return "seed" if re.search(r"seed(\d|\{)", path_str) else "group"


def _count_excluded(lines: list[str]) -> tuple[int, int]:
    """(excluded_seed_count, superseded_group_count) from an EXCLUDED block's
    trace-path comment lines. Purely mechanical -- see module docstring on "render,
    do not recompute": this is the one place site.py *does* compute numbers, and it
    computes them from the file's own text via `_classify_excluded_line`, not a
    hand-typed constant, precisely so neither figure can silently drift from the
    commentary sitting right next to it.

    These are two different UNITS and must stay two return values, not one summed
    total: "seeds excluded as invalid evidence" and "whole run groups superseded,
    never judged invalid" are not the same kind of thing. Summing every brace-
    expanded `runs/` comment line regardless of kind is exactly the bug this
    function replaces -- it counted 12 against results_traces.txt's audited 9,
    because it expanded the 2 superseded-group lines' brace-listed *timestamps*
    as if they were seeds.
    """
    seed_total = 0
    group_total = 0
    for raw in lines:
        s = raw.strip()
        if not s.startswith("#"):
            continue
        s = s[1:].strip()
        if not s.startswith("runs/"):
            continue
        n = _brace_expand_count(s)
        if _classify_excluded_line(s) == "seed":
            seed_total += n
        else:
            group_total += n
    return seed_total, group_total


def _parse_excluded_entries(lines: list[str]) -> list[dict]:
    """Structured rows from an EXCLUDED block: one entry per group of consecutive
    `# runs/...` path-comment lines plus the explanatory comment paragraph that
    follows them, up to the next blank comment line. Same source lines and the
    same path-shape classification `_count_excluded` uses (`_classify_excluded_line`
    / `_brace_expand_count`) -- this reshapes the same text into rows a caller can
    render as a list instead of dumping the whole block through a `<pre>`; it
    computes no number `_count_excluded` doesn't already compute independently
    (summing `count` across `kind == "seed"` entries agrees with
    `_count_excluded`'s `excluded_seed_count`, by construction, not by sharing
    state -- see the two tests that pin both against the same fixture).

    A trailing prose paragraph with no leading `runs/...` line (results_traces.txt
    carries one, explaining why S4's excluded seeds are not re-listed as bare
    lines here) is not tied to specific paths and yields no entry -- there is
    nowhere structured for it to go; it stays reachable only via the linked
    source file.
    """
    groups: list[list[str]] = []
    current: list[str] = []
    for raw in lines:
        if "Deliberately EXCLUDED" in raw:
            continue
        s = raw.strip()
        if not s.startswith("#"):
            continue
        s = s[1:].strip()
        if not s:
            if current:
                groups.append(current)
                current = []
            continue
        current.append(s)
    if current:
        groups.append(current)

    entries = []
    for group in groups:
        paths = [ln for ln in group if ln.startswith("runs/")]
        if not paths:
            continue
        reason = " ".join(ln for ln in group if not ln.startswith("runs/")).strip()
        kind = "seed" if any(_classify_excluded_line(p) == "seed" for p in paths) else "group"
        count = sum(_brace_expand_count(p) for p in paths)
        entries.append({"paths": paths, "reason": reason, "kind": kind, "count": count})
    return entries


def parse_traces_commentary(text: str) -> dict:
    """`results_traces.txt` -> the active trace-dir list plus its own prose, split
    into the intro block (provenance / regen command / audit note) and the
    "Deliberately EXCLUDED" block. Pure text parsing, no filesystem access -- what
    the returned `trace_dirs` point to may or may not exist on this machine
    (`runs/` is gitignored); resolving that is `build_site`'s job, not this one's.
    """
    lines = text.splitlines()

    trace_dirs = [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]

    intro_lines: list[str] = []
    for ln in lines:
        if ln.strip().startswith("# ---"):
            break
        intro_lines.append(ln)

    excluded_lines: list[str] = []
    capturing = False
    for ln in lines:
        if "Deliberately EXCLUDED" in ln:
            capturing = True
        if capturing:
            excluded_lines.append(ln)

    excluded_seed_count, superseded_group_count = _count_excluded(excluded_lines)

    return {
        "trace_dirs": trace_dirs,
        "reproduce_note": _strip_comment_block(intro_lines),
        "exclusion_note": _strip_comment_block(excluded_lines),
        "exclusion_seed_count": excluded_seed_count,
        # NOT part of exclusion_seed_count -- see _classify_excluded_line. These
        # directories were superseded (replaced by later seeded runs), never
        # judged invalid evidence; conflating the two units produced 12 where the
        # audited figure is 9 (CLAUDE.md).
        "superseded_group_count": superseded_group_count,
        # Structured version of exclusion_note -- what render_index actually
        # displays now (see its 2026-08-16 restructure comment); exclusion_note is
        # kept too, as a fallback for a caller that builds a doc without going
        # through this function.
        "exclusion_entries": _parse_excluded_entries(excluded_lines),
    }


# --- copying trace artifacts + orchestration --------------------------------------


def run_key_for(trace_path: str | Path) -> str:
    """`runs/bench/s1-model-t0/ts/seed0` -> `bench/s1-model-t0/ts/seed0` -- the
    directory this run publishes under inside `web/dist/runs/`. Strips a leading
    `runs` segment (the gitignored trace root every path in `results_traces.txt`
    shares); anything else is passed through unchanged so the key stays a stable,
    collision-free mirror of the original path.

    Any drive/root anchor is stripped first and unconditionally, even for paths
    that never had a `runs` prefix (e.g. an absolute path used directly). Leaving
    one in is a real Windows `pathlib` footgun, not a theoretical one: `runs_out /
    key` treats a key that still looks absolute (`"C:/Users/..."`) as a fresh
    anchor and silently discards `runs_out` entirely instead of nesting under it,
    so a copy step meant to write into `web/dist/runs/<key>/` writes into the
    *source* trace directory instead. Caught by
    `test_cli_site_build_writes_index_and_run_pages_without_pngs`.
    """
    p = Path(str(trace_path).replace("\\", "/"))
    parts = list(p.parts)
    if p.anchor:
        parts = parts[1:]
    if parts and parts[0].lower() == "runs":
        parts = parts[1:]
    return "/".join(parts)


def copy_run_artifacts(src_dir: Path, dest_dir: Path) -> list[str]:
    """Copy exactly `run.jsonl` / `meta.json` / `summary.json` -- see
    `_PUBLISHABLE_FILES`. Never a `turn_*.png`: there is no glob for one here, so a
    screenshot cannot reach `dest_dir` by any code path in this function."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    copied = []
    for name in _PUBLISHABLE_FILES:
        src = src_dir / name
        if src.is_file():
            shutil.copy2(src, dest_dir / name)
            copied.append(name)
    return copied


@dataclass
class SiteBuildReport:
    out_dir: Path
    rows: int
    run_pages: int
    missing_traces: list[str] = field(default_factory=list)


def build_site(
    results_path: str | Path,
    out_dir: str | Path,
    traces_path: str | Path | None = None,
) -> SiteBuildReport:
    """Render `web/dist/index.html` from `results.json`, and (if `traces_path` is
    given and exists) one replay page per trace directory it lists, under
    `web/dist/runs/<key>/`.

    `traces_path` is optional and missing-tolerant on purpose: `results.json` alone
    is enough to build a leaderboard (the format the M2/M3 pipeline has always
    produced), and a fresh clone with no local `runs/` still gets a working
    `pokebench site build` -- it just gets a table with no replay links and no
    curation commentary, reported via `missing_traces` rather than a hard failure
    (`runs/` is gitignored; a trace listed in `results_traces.txt` legitimately may
    not exist on this machine).
    """
    results_path = Path(results_path)
    out_dir = Path(out_dir)
    doc = json.loads(results_path.read_text(encoding="utf-8"))

    trace_dirs: list[str] = []
    commentary: dict = {}
    if traces_path is not None:
        traces_path = Path(traces_path)
        if traces_path.exists():
            commentary = parse_traces_commentary(traces_path.read_text(encoding="utf-8"))
            trace_dirs = commentary.get("trace_dirs", [])

    out_dir.mkdir(parents=True, exist_ok=True)
    runs_out = out_dir / "runs"

    links_by_key: dict[tuple, list[dict]] = {}
    missing: list[str] = []
    run_pages = 0
    for raw in trace_dirs:
        src = Path(raw)
        if not src.exists():
            missing.append(raw)
            continue
        run = load_run(src)
        key = run_key_for(raw)
        dest = runs_out / key
        copy_run_artifacts(src, dest)
        page_html = render_run_page(src, key)
        (dest / "index.html").write_text(page_html, encoding="utf-8")
        run_pages += 1

        meta = run.meta or {}
        row_key = (meta.get("model"), meta.get("scenario"), meta.get("tier"))
        segs = key.split("/")
        label = "/".join(segs[-2:]) if len(segs) >= 2 else key
        # ORDER FRAGILITY (documented, not fixed here -- see
        # test_run_links_preserve_trace_dirs_order_not_seed_number_order): this list
        # is built by plain append, in `trace_dirs` (i.e. results_traces.txt) file
        # order, per (model, scenario, tier). `pokebench score`'s CLI (`cli.py`'s
        # `groups[...].append`) builds `seed_values`' per-seed arrays the same way,
        # over the same file's lines -- so today run_links[i] and
        # seed_values["turns"][i] line up. Nothing enforces that pairing explicitly
        # on either side; if either module ever sorts/re-keys by seed number instead
        # of file order, a run_link would silently point at the wrong seed_values
        # entry. Keep both as plain ordered appends over the same source list.
        links_by_key.setdefault(row_key, []).append(
            {"label": label, "href": f"runs/{key}/index.html"}
        )

    enriched = copy.deepcopy(doc)
    if commentary.get("reproduce_note"):
        enriched["reproduce_note"] = commentary["reproduce_note"]
    if commentary.get("exclusion_note"):
        enriched["exclusion_note"] = commentary["exclusion_note"]
        enriched["exclusion_seed_count"] = commentary.get("exclusion_seed_count")
        enriched["superseded_group_count"] = commentary.get("superseded_group_count")
        enriched["exclusion_entries"] = commentary.get("exclusion_entries")
    for row in enriched.get("rows", []):
        row_key = (row.get("model"), row.get("scenario"), row.get("tier"))
        row["run_links"] = links_by_key.get(row_key, [])

    (out_dir / "index.html").write_text(render_index(enriched), encoding="utf-8")

    return SiteBuildReport(
        out_dir=out_dir,
        rows=len(enriched.get("rows", [])),
        run_pages=run_pages,
        missing_traces=missing,
    )
