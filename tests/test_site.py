"""`pokebench site build` — the M4 static leaderboard + per-run text replay pages
(offline: no ROM/network/key, and no PNGs — see `site.py`'s module docstring).

Synthetic `results.json`-shaped dict literals throughout (never a real file), same
style as `tests/test_viewer.py`'s synthetic traces via `_rec`/`_write_run`. The three
honesty assertions below are the ones the plan calls out explicitly; the rest cover the
other Stage 1/2 requirements (exclusions, spread, commentary, run links) and the
HTML-injection regression the plan specifically flags as a real risk in this refactor.
"""

from __future__ import annotations

import json
from pathlib import Path

from PIL import Image

from pokebench.cli import main
from pokebench.site import (
    build_site,
    parse_traces_commentary,
    render_index,
    render_run_page,
    run_key_for,
)
from tests.test_replay import _rec, _write_run

# --- fixtures ----------------------------------------------------------------------


def _row(**overrides) -> dict:
    row = {
        "model": "sonnet",
        "scenario": "s1_exit_pallet",
        "tier": 0,
        "seeds": 3,
        "seeds_valid": 3,
        "seeds_excluded": 0,
        "exclusions": [],
        "cap_turns": 100,
        "reasoning": None,
        "reasoning_provenance": "off_unrecorded",
        "stop_reasons": {"max_turns": 3},
        "success_rate": 0.0,
        "success_at_cap": False,
        "median_steps_to_success": None,
        "median_turns": 100,
        "median_cost_usd": 1.25,
        "median_tiles_explored": 4,
        "median_idle_rate": 0.96,
        "seed_values": {
            "success": [False, False, False],
            "turns": [100, 100, 100],
            "cost_usd": [1.069503, 1.321272, 1.251453],
            "tiles_explored": [4, 18, 2],
            "idle_rate": [0.9596, 0.7374, 0.9798],
            "no_tool_call_rate": [0.0, 0.16, 0.0],
        },
    }
    row.update(overrides)
    return row


def _doc(*rows) -> dict:
    return {"schema": 4, "generated": "2026-08-11T00:00:00", "rows": list(rows)}


# --- the three failing tests the plan calls out -------------------------------------


def test_s3_row_renders_ceiling_probe_marker_not_a_bare_score():
    row = _row(
        scenario="s3_viridian_forest", model="gemini", reasoning_provenance="not_applicable"
    )
    html_out = render_index(_doc(row))
    assert "ceiling probe" in html_out
    assert "no scripted oracle" in html_out  # the full sentence -- now in the legend
    # The normal (non-caveat) success cell renders "<rate>% <small>(N seed(s))</small>"
    # -- that exact pattern must not appear for an s3 row; the marker replaces it
    # entirely rather than sitting next to a percentage that dresses it up as a score.
    assert "% <small>" not in html_out


def test_s3_ceiling_probe_marker_is_compact_full_sentence_moves_to_title_and_legend():
    """The full ~40-word caveat sentence used to BE the SUCCESS cell's text --
    wrapping to ~18 lines in a ~140px-wide column, repeated once per S3 row. It
    must now be a short marker in the cell (mirroring `_model_cell`'s
    reasoning-off marker), full sentence recoverable via `title` and stated once
    in the legend, never as running prose inside the cell's own `<span>`."""
    row = _row(
        scenario="s3_viridian_forest", model="gemini", reasoning_provenance="not_applicable"
    )
    html_out = render_index(_doc(row))

    marker_open = html_out.index('class="caveat-marker ceiling-probe"')
    span_start = html_out.rindex("<span", 0, marker_open)
    tag_close = html_out.index(">", marker_open)
    span_end = html_out.index("</span>", tag_close)
    marker_tag = html_out[span_start : tag_close + 1]
    marker_text = html_out[tag_close + 1 : span_end]

    # The visible marker text is short -- not the full sentence.
    assert "no scripted oracle" not in marker_text
    assert len(marker_text) <= 20
    # ...but the full sentence is still attached to that exact marker via `title`.
    assert "ceiling probe" in marker_tag
    assert "no scripted oracle" in marker_tag
    assert "winnability under the current anchor state is unverified" in marker_tag

    # The per-row seed count ("3 seed(s) probed") is the one fact that legitimately
    # varies row to row, so it survives inline in the cell (not folded into the
    # once-stated legend sentence). `_row()`'s default seeds_valid is 3.
    assert "3 seed(s) probed" in html_out


def test_s3_ceiling_probe_legend_appears_beneath_table_uncollapsed_when_s3_row_present():
    """The caveat must stay discoverable without leaving the page and without an
    extra click, exactly like the reasoning-off legend -- and it must say which
    rows it applies to, since it is no longer stated beside every one of them."""
    row = _row(
        scenario="s3_viridian_forest", model="gemini", reasoning_provenance="not_applicable"
    )
    doc = _doc(row)
    doc["reproduce_note"] = "python -m uv run pokebench score ... --out results.json"
    html_out = render_index(doc)

    table_end = html_out.index("</table>")
    first_details = html_out.index("<details", table_end)
    legend_region = html_out[table_end:first_details]
    assert "ceiling probe" in legend_region
    assert "no scripted oracle" in legend_region
    assert "winnability under the current anchor state is unverified" in legend_region
    assert "s3_viridian_forest" in legend_region  # which rows it applies to


def test_s3_ceiling_probe_legend_absent_when_no_s3_row():
    html_out = render_index(_doc(_row(scenario="s1_exit_pallet", model="gemini")))
    assert "no scripted oracle" not in html_out


def test_reasoning_off_unrecorded_gets_a_visible_marker_gpt_not_applicable_does_not():
    sonnet_row = _row(model="sonnet", reasoning_provenance="off_unrecorded")
    gpt_row = _row(
        model="gpt", scenario="s2_viridian_pokecenter", reasoning_provenance="not_applicable"
    )

    sonnet_html = render_index(_doc(sonnet_row))
    assert "reasoning OFF" in sonnet_html
    assert "not a like-for-like comparison" in sonnet_html

    gpt_html = render_index(_doc(gpt_row))
    assert "reasoning OFF" not in gpt_html


def test_recorded_off_also_gets_the_marker_not_only_off_unrecorded():
    """`recorded_off` means reasoning was off AND the trace recorded it -- strictly
    better evidence than `off_unrecorded`, which infers it. Flagging only the latter
    inverted the disclosure on the live leaderboard (found 2026-08-17): sonnet's
    S1-S3 rows (`off_unrecorded`, 0/3 each) carried the marker while its S4 row
    (`recorded_off`, 3/3) did not -- so the one scenario sonnet WON was the only one
    that looked like a like-for-like comparison, inviting the reader to conclude
    reasoning is what won it. Both provenances mean the same caveat applies."""
    row = _row(scenario="s4_viridian_mart", model="sonnet", reasoning_provenance="recorded_off")
    html_out = render_index(_doc(row))
    assert "reasoning OFF" in html_out
    assert "not a like-for-like comparison" in html_out


def test_reasoning_off_marker_is_compact_a_marker_not_a_sentence_in_the_cell():
    """The full caveat sentence used to BE the MODEL cell's text -- six lines of
    wrapped prose that set every row's height, including the ~20 rows with no
    caveat at all. It must now be a short marker in the cell (full sentence
    recoverable via `title`), never running prose inside the `<span>` itself."""
    row = _row(model="sonnet", reasoning_provenance="off_unrecorded")
    html_out = render_index(_doc(row))

    marker_open = html_out.index('class="caveat-marker reasoning-off"')
    span_start = html_out.rindex("<span", 0, marker_open)
    tag_close = html_out.index(">", marker_open)
    span_end = html_out.index("</span>", tag_close)
    marker_tag = html_out[span_start : tag_close + 1]
    cell_text = html_out[tag_close + 1 : span_end]

    # The visible text is short -- not the full sentence.
    assert "not a like-for-like comparison" not in cell_text
    assert len(cell_text) <= 20
    # ...but the full sentence is still attached to that exact marker via `title`.
    assert "reasoning OFF" in marker_tag
    assert "not a like-for-like comparison" in marker_tag


def test_reasoning_off_legend_appears_beneath_table_uncollapsed_when_marker_present():
    """The caveat must stay discoverable without leaving the page and without an
    extra click -- a legend directly under the table, not tucked inside a closed
    `<details>` provenance panel."""
    row = _row(model="sonnet", reasoning_provenance="off_unrecorded")
    doc = _doc(row)
    doc["reproduce_note"] = "python -m uv run pokebench score ... --out results.json"
    html_out = render_index(doc)

    table_end = html_out.index("</table>")
    first_details = html_out.index("<details", table_end)
    legend_region = html_out[table_end:first_details]
    assert "reasoning OFF" in legend_region
    assert "not a like-for-like comparison" in legend_region


def test_reasoning_off_legend_absent_when_no_row_has_the_marker():
    html_out = render_index(_doc(_row(model="gpt", reasoning_provenance="not_applicable")))
    assert "reasoning OFF" not in html_out


def test_seed_values_spread_rendered_not_only_median():
    row = _row(median_tiles_explored=4)  # seed_values.tiles_explored == [4, 18, 2]
    html_out = render_index(_doc(row))
    assert "18" in html_out  # the median alone (4) hides the bimodal spread
    assert "2" in html_out


# --- more Stage 1 honesty coverage ---------------------------------------------------


def test_degenerate_seed_values_do_not_render_a_spurious_spread():
    row = _row(
        seed_values={**_row()["seed_values"], "tiles_explored": [7, 7, 7]},
        median_tiles_explored=7,
    )
    html_out = render_index(_doc(row))
    assert "seeds: 7, 7, 7" not in html_out


def test_row_with_exclusions_renders_reason_and_detail_inline():
    row = _row(
        seeds_excluded=1,
        exclusions=[
            {
                "reason": "output_ceiling_truncation",
                "detail": "39% of turns hit the output ceiling",
                "stop_reason": "max_turns",
                "turns": 100,
                "evidence": {},
            }
        ],
    )
    html_out = render_index(_doc(row))
    assert "output_ceiling_truncation" in html_out
    assert "39% of turns hit the output ceiling" in html_out


def test_top_level_exclusion_commentary_and_seed_count_are_surfaced():
    doc = _doc(_row())
    doc["exclusion_note"] = "runs/foo/seed0 -- reasoning ON, hit the ceiling"
    doc["exclusion_seed_count"] = 9
    html_out = render_index(doc)
    assert "9 seeds were excluded during curation" in html_out
    assert "reasoning ON, hit the ceiling" in html_out


def test_reproduce_offline_section_is_surfaced_when_present():
    doc = _doc(_row())
    doc["reproduce_note"] = "python -m uv run pokebench score ... --out results.json"
    html_out = render_index(doc)
    assert "pokebench score" in html_out


def test_sections_absent_when_no_commentary_supplied():
    html_out = render_index(_doc(_row()))
    assert "were excluded during curation" not in html_out


# --- hierarchy restructure: the table leads, provenance moves below and collapses ---


def test_table_appears_before_provenance_sections_in_markup():
    doc = _doc(_row())
    doc["reproduce_note"] = "python -m uv run pokebench score ... --out results.json"
    doc["exclusion_note"] = "runs/foo/seed0 -- reasoning ON, hit the ceiling"
    doc["exclusion_seed_count"] = 9
    html_out = render_index(doc)

    table_pos = html_out.index("<table>")
    reproduce_pos = html_out.index("How this table was produced")
    exclusions_pos = html_out.index("were excluded during curation")
    assert table_pos < reproduce_pos
    assert table_pos < exclusions_pos


def test_reproduce_and_exclusion_sections_are_closed_details_not_open():
    doc = _doc(_row())
    doc["reproduce_note"] = "python -m uv run pokebench score ... --out results.json"
    doc["exclusion_note"] = "runs/foo/seed0 -- reasoning ON, hit the ceiling"
    doc["exclusion_seed_count"] = 9
    html_out = render_index(doc)

    assert '<details class="panel" id="reproduce">' in html_out
    assert '<details class="panel" id="exclusions-curated">' in html_out
    # Neither carries the `open` attribute -- closed by default, one click to expand.
    assert "<details open" not in html_out


def test_reproduce_section_shows_a_command_not_the_full_provenance_prose():
    doc = _doc(_row())
    doc["reproduce_note"] = (
        "Provenance for results.json.\n\n"
        "AUDITED 2026-08-04 by metrics/validity.py (build 1). The machine predicate "
        "reproduces this file's hand-curation exactly.\n\n"
        "  python -m uv run pokebench score $(grep -v '^#' results_traces.txt) --out "
        "results.json\n"
    )
    html_out = render_index(doc)

    assert "pokebench score" in html_out
    # The multi-paragraph rationale is not inlined any more -- only the runnable
    # command, plus a link out to the real file.
    assert "AUDITED 2026-08-04" not in html_out
    assert "results_traces.txt" in html_out


def test_exclusions_section_renders_structured_entries_not_a_raw_note_dump():
    entries = [
        {
            "paths": ["runs/bench/s1-sonnet-t0/ts/seed{0,1,2}"],
            "reason": "Sonnet with reasoning ON, hit the output ceiling.",
            "kind": "seed",
            "count": 3,
        },
        {
            "paths": ["runs/bench/s2-haiku-t0/other_ts/seed0", "runs/bench/s2-haiku-t0/x/seed1"],
            "reason": "Superseded single-seed haiku groups.",
            "kind": "group",
            "count": 2,
        },
    ]
    doc = _doc(_row())
    doc["exclusion_seed_count"] = 3
    doc["superseded_group_count"] = 2
    doc["exclusion_entries"] = entries
    # A raw note is ALSO present (build_site supplies both) -- it must not win out
    # over the structured entries in the default rendering.
    doc["exclusion_note"] = (
        "AUDITED 2026-08-04 by metrics/validity.py (build 1). Some long paragraph "
        "of provenance rationale that should not be inlined verbatim any more.\n"
        "runs/bench/s1-sonnet-t0/ts/seed{0,1,2}\n  Sonnet with reasoning ON, hit "
        "the output ceiling."
    )
    html_out = render_index(doc)

    assert "Sonnet with reasoning ON, hit the output ceiling." in html_out
    assert "3 seeds excluded" in html_out
    assert "AUDITED 2026-08-04" not in html_out


def test_masthead_shows_a_one_line_summary_derived_from_rows():
    doc = _doc(
        _row(model="gemini", scenario="s1_exit_pallet", seeds_valid=3),
        _row(model="sonnet", scenario="s2_viridian_pokecenter", seeds_valid=3),
    )
    html_out = render_index(doc)
    assert "2 models" in html_out
    assert "2 scenarios" in html_out
    assert "6 valid seed-run" in html_out


# --- parse_traces_commentary (pure text parsing, no filesystem) ---------------------

_SAMPLE_TRACES_TXT = """\
# Provenance for results.json.
#
# Regenerate with:
#   python -m uv run pokebench score $(grep -v '^#' results_traces.txt) --out results.json
#
# AUDITED 2026-08-11: 0 false positives on the included seeds.

# --- M3 sweep -----------------------------------------------------------
runs/bench/s1-model-t0/ts/seed0
runs/bench/s1-model-t0/ts/seed1
runs/bench/s1-model-t0/ts/seed2

# --- Deliberately EXCLUDED, and why --------------------------------------
#
# runs/bench/s1-model-t0/other_ts/seed{0,1,2}
#   reasoning ON, hit the ceiling.
#
# runs/bench/s1-model-t0/another_ts/seed0
#   wall-clock cap.
"""


def test_parse_traces_commentary_extracts_active_trace_dirs():
    parsed = parse_traces_commentary(_SAMPLE_TRACES_TXT)
    assert parsed["trace_dirs"] == [
        "runs/bench/s1-model-t0/ts/seed0",
        "runs/bench/s1-model-t0/ts/seed1",
        "runs/bench/s1-model-t0/ts/seed2",
    ]


def test_parse_traces_commentary_extracts_reproduce_note():
    parsed = parse_traces_commentary(_SAMPLE_TRACES_TXT)
    assert "pokebench score" in parsed["reproduce_note"]
    assert "AUDITED" in parsed["reproduce_note"]


def test_parse_traces_commentary_extracts_exclusion_note_and_counts_brace_expansion():
    parsed = parse_traces_commentary(_SAMPLE_TRACES_TXT)
    assert "reasoning ON, hit the ceiling" in parsed["exclusion_note"]
    assert "wall-clock cap" in parsed["exclusion_note"]
    # seed{0,1,2} expands to 3, plus the single seed0 line below it = 4.
    assert parsed["exclusion_seed_count"] == 4
    # No superseded-group lines (no seed<N>-less directory) in this sample.
    assert parsed["superseded_group_count"] == 0


# Structurally mirrors the real EXCLUDED block in results_traces.txt (audited
# 2026-08-04: 9 excluded seeds, plus 3 superseded pre-M2 haiku run *groups* that are
# NOT part of that 9 -- see CLAUDE.md). Naively summing every brace-expanded "runs/"
# comment line in this block (the pre-fix behaviour) yields 12, conflating "seed
# excluded as invalid evidence" with "whole run group superseded, never judged
# invalid" -- two different units. This fixture is synthetic (never reads the real
# file, matching this module's "synthetic fixtures throughout" convention) but
# reproduces its shape exactly: 7 seed-bearing comment lines expanding to 9 seeds,
# then 2 group comment lines (no seed<N> component) expanding to 3 groups.
_SAMPLE_REAL_SHAPED_EXCLUDED_TXT = """\
# --- M3 sweep -------------------------------------------------------------
runs/bench/s1-model-t0/ts/seed0

# --- Deliberately EXCLUDED, and why -----------------------------------------------------
#
# runs/bench/s1_exit_pallet-sonnet-t0/20260721-092407/seed{0,1,2}
# runs/bench/s2_viridian_pokecenter-sonnet-t0/20260721-103105/seed0
#   Sonnet with reasoning ON. Hit the output ceiling on 39% of turns.
#
# runs/bench/s2_viridian_pokecenter-sonnet-t0/20260728-101506/_aborted_outputcap_seed0
# runs/bench/s3_viridian_forest-sonnet-t0/20260728-102030/_aborted_outputcap_seed0
#   Killed at 31 and 1 turns once the truncation above was diagnosed mid-run.
#
# runs/bench/s1_exit_pallet-qwen-local-t0/20260721-091812/_discarded_wallclock_seed2
#   Died on the wall-clock cap before it was lifted for the local leg.
#
# runs/bench/s2_viridian_pokecenter-sonnet-t0/20260721-103105/seed1
# runs/bench/s3_viridian_forest-sonnet-t0/20260720-125035/seed1
#   Incomplete -- no summary.json.
#
# runs/bench/s2_viridian_pokecenter-haiku-t0/20260719-{152600,153859}
# runs/bench/s3_viridian_forest-haiku-t0/20260719-152740
#   Superseded single-seed haiku groups from before the M2 sweep.
"""


def test_parse_traces_commentary_separates_excluded_seeds_from_superseded_groups():
    parsed = parse_traces_commentary(_SAMPLE_REAL_SHAPED_EXCLUDED_TXT)
    # The audited figure (CLAUDE.md / HANDOFF): 9, never 12. 12 is what you get by
    # miscounting the 2 superseded-group lines' brace-expanded *timestamps* as seeds.
    assert parsed["exclusion_seed_count"] == 9
    assert parsed["superseded_group_count"] == 3


def test_parse_traces_commentary_extracts_structured_exclusion_entries():
    """One entry per comment-paragraph group in the EXCLUDED block -- the shape
    `render_index` needs to draw structured rows instead of a `<pre>` of the whole
    block. Same fixture, same audited totals as the seed/group-count test above,
    checked here at the per-entry level (4 seed-kind groups summing to 9, 1
    group-kind entry summing to 3)."""
    parsed = parse_traces_commentary(_SAMPLE_REAL_SHAPED_EXCLUDED_TXT)
    entries = parsed["exclusion_entries"]
    seed_entries = [e for e in entries if e["kind"] == "seed"]
    group_entries = [e for e in entries if e["kind"] == "group"]

    assert len(seed_entries) == 4
    assert sum(e["count"] for e in seed_entries) == 9
    assert len(group_entries) == 1
    assert group_entries[0]["count"] == 3
    assert "reasoning ON" in seed_entries[0]["reason"]


def test_render_index_renders_excluded_seeds_and_superseded_groups_distinctly():
    parsed = parse_traces_commentary(_SAMPLE_REAL_SHAPED_EXCLUDED_TXT)
    doc = _doc(_row())
    doc["exclusion_note"] = parsed["exclusion_note"]
    doc["exclusion_seed_count"] = parsed["exclusion_seed_count"]
    doc["superseded_group_count"] = parsed["superseded_group_count"]

    html_out = render_index(doc)

    # The two counts must both be visible, and visibly distinct -- never summed into
    # a single misleading "12 seeds were excluded" (the actual pre-fix defect: the
    # heading is the one place a merged count would surface).
    assert "9 seeds were excluded" in html_out
    assert "12 seeds" not in html_out
    # The 3 superseded groups get their own mention, worded so a reader cannot read
    # them as invalid evidence (they were replaced, not rejected).
    assert "3" in html_out
    lower = html_out.lower()
    assert "superseded" in lower
    assert "not invalid" in lower or "not judged invalid" in lower


# --- run_links density: short seed labels, not two-line timestamp paths -----------


def test_run_links_render_short_seed_labels_full_path_stays_in_title():
    """Each row used to show `<timestamp>/seedN` as the anchor's own visible text
    -- identical across every link in a row (the timestamp never varies) and long
    enough to wrap across two lines. Only the seed's position should be visible;
    the descriptive label stays recoverable via `title`."""
    row = _row(seeds_valid=3)
    row["run_links"] = [
        {"label": "20260720-115026/seed0", "href": "runs/bench/x/seed0/index.html"},
        {"label": "20260720-115026/seed1", "href": "runs/bench/x/seed1/index.html"},
        {"label": "20260720-115026/seed2", "href": "runs/bench/x/seed2/index.html"},
    ]
    html_out = render_index(_doc(row))

    cell_start = html_out.index('class="seed-links"')
    cell_end = html_out.index("</span>", cell_start)
    cell_html = html_out[cell_start:cell_end]

    # The long timestamp is gone from the *visible* anchor text...
    assert ">20260720-115026" not in cell_html
    # ...but every link is still present, each with its own href...
    assert cell_html.count("<a href=") == 3
    assert "seed0/index.html" in cell_html
    assert "seed1/index.html" in cell_html
    assert "seed2/index.html" in cell_html
    # ...and the full descriptive label survives as a recoverable tooltip.
    assert "20260720-115026/seed0" in cell_html  # inside title="..."


def test_run_links_extras_beyond_seeds_valid_collapse_instead_of_enumerating_flat():
    """The sonnet S4 case: 12 links against 3 valid seeds (every retry stays in
    results_traces.txt's active block). Naively rendering 12 bare digits reads as
    noise next to a row whose numbers came from only 3 of them. The links that
    line up with seeds_valid render at full weight; the rest collapse behind a
    disclosure, still individually clickable."""
    row = _row(seeds_valid=3)
    row["run_links"] = [
        {"label": f"ts{i}/seed0", "href": f"runs/bench/x/{i}/seed0/index.html"}
        for i in range(12)
    ]
    html_out = render_index(_doc(row))

    cell_start = html_out.index('class="seed-links"')
    primary_end = html_out.index("</span>", cell_start)
    primary_html = html_out[cell_start:primary_end]
    assert primary_html.count("<a href=") == 3

    rest_html = html_out[primary_end : html_out.index("</td>", primary_end)]
    assert "<details" in rest_html
    assert "+9 more" in rest_html
    assert rest_html.count("<a href=") == 9


def test_run_links_no_grouping_when_link_count_matches_seeds_valid():
    row = _row(seeds_valid=3)
    row["run_links"] = [
        {"label": f"ts/seed{i}", "href": f"runs/bench/x/seed{i}/index.html"} for i in range(3)
    ]
    html_out = render_index(_doc(row))
    cell_start = html_out.index('class="seed-links"')
    cell_end = html_out.index("</td>", cell_start)
    assert "<details" not in html_out[cell_start:cell_end]


# --- run_key_for -----------------------------------------------------------------


def test_run_key_for_strips_leading_runs_segment():
    assert run_key_for("runs/bench/s1-model-t0/ts/seed0") == "bench/s1-model-t0/ts/seed0"


def test_run_key_for_leaves_non_runs_prefixed_paths_alone():
    assert run_key_for("bench/s1-model-t0/ts/seed0") == "bench/s1-model-t0/ts/seed0"


# --- render_run_page: zero frames, plus the HTML-injection regression --------------


def test_render_run_page_shows_turns_and_the_no_screenshots_placeholder(tmp_path):
    d = _write_run(
        tmp_path / "run1",
        [
            _rec(1, "aaa", 3, 6, executed=["up"], text="climbing the stairs"),
            _rec(2, "bbb", 7, 1, executed=["right"], success=True),
        ],
        meta={"model": "gemini", "tier": 0, "scenario": "s1_exit_pallet"},
        summary={"success": True, "stop_reason": "success", "turns": 2, "usd": 0.05},
    )
    html_out = render_run_page(d, "bench/s1-gemini-t0/ts/seed0")
    assert "climbing the stairs" in html_out
    assert "SUCCESS" in html_out
    assert "pokebench watch" in html_out  # tells the reader how to get real frames
    assert "<img" not in html_out
    assert "/shot/" not in html_out


def test_render_run_page_never_references_real_screenshots_on_disk(tmp_path):
    """The frozen decision: zero frames ship publicly, even when the source trace
    directory sitting right next to this page has real PNGs in it."""
    d = _write_run(tmp_path / "run1", [_rec(1, "aaa", 3, 6, executed=["up"])])
    Image.new("RGB", (160, 144)).save(d / "turn_0001.png")
    html_out = render_run_page(d, "seed0")
    assert "<img" not in html_out
    assert "turn_0001.png" not in html_out


def test_render_run_page_escapes_model_text_script_injection(tmp_path):
    payload_text = "<script>alert(1)</script>"
    d = _write_run(tmp_path / "run1", [_rec(1, "aaa", 3, 6, executed=["up"], text=payload_text)])
    html_out = render_run_page(d, "seed0")
    assert "<script>" not in html_out
    assert "&lt;script&gt;" in html_out


def test_render_run_page_escapes_notes_onerror_injection(tmp_path):
    d = _write_run(tmp_path / "run1", [_rec(1, "aaa", 3, 6, executed=["up"])])
    lines = (d / "run.jsonl").read_text(encoding="utf-8").splitlines()
    rec = json.loads(lines[0])
    rec["notes"] = "<img src=x onerror=alert(1)>"  # Tier-1 scratchpad text
    (d / "run.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")

    html_out = render_run_page(d, "seed0")
    # The payload text ("onerror=alert(1)") surviving as inert text content is fine
    # and expected -- what must never survive is a live "<img ...>" tag, i.e. an
    # unescaped angle bracket. That is the actual injection vector.
    assert "<img" not in html_out
    assert "&lt;img" in html_out


# --- build_site: end-to-end, offline, no PNGs ever copied ---------------------------


def test_cli_site_build_writes_index_and_run_pages_without_pngs(tmp_path):
    run_dir = tmp_path / "runs" / "bench" / "s1-model-t0" / "20260101-000000" / "seed0"
    _write_run(
        run_dir,
        [
            _rec(1, "aaa", 3, 6, executed=["up"], text="hi"),
            _rec(2, "bbb", 7, 1, success=True),
        ],
        meta={"model": "gemini", "tier": 0, "scenario": "s1_exit_pallet"},
        summary={"success": True, "stop_reason": "success", "turns": 2, "usd": 0.05},
    )
    Image.new("RGB", (160, 144)).save(run_dir / "turn_0001.png")

    results_path = tmp_path / "results.json"
    row = _row(
        model="gemini", scenario="s1_exit_pallet", tier=0, reasoning_provenance="not_applicable"
    )
    results_path.write_text(json.dumps(_doc(row)), encoding="utf-8")

    # results_traces.txt lines are conventionally relative to the repo root (the
    # real file is invoked as `pokebench score $(cat results_traces.txt)` from
    # there), but pytest's cwd is not `tmp_path` -- use an absolute path so
    # `build_site` resolves it regardless of the process's working directory.
    abs_path = str(run_dir.resolve()).replace("\\", "/")
    traces_path = tmp_path / "results_traces.txt"
    traces_path.write_text(
        "# header\n#   python -m uv run pokebench score ...\n\n"
        "# --- sweep ---\n"
        f"{abs_path}\n"
        "\n# --- Deliberately EXCLUDED, and why ---\n"
        "# runs/other/seed0\n"
        "#   reasoning ON\n",
        encoding="utf-8",
    )

    out_dir = tmp_path / "web_dist"
    rc = main(
        [
            "site",
            "build",
            "--results",
            str(results_path),
            "--out",
            str(out_dir),
            "--traces",
            str(traces_path),
        ]
    )
    assert rc == 0
    assert (out_dir / "index.html").exists()

    key = run_key_for(abs_path)
    run_page_dir = out_dir / "runs" / key
    assert (run_page_dir / "index.html").exists()
    assert (run_page_dir / "run.jsonl").exists()
    assert (run_page_dir / "meta.json").exists()

    # No PNGs anywhere under the published output.
    assert list(out_dir.rglob("*.png")) == []

    index_html = (out_dir / "index.html").read_text(encoding="utf-8")
    assert f"runs/{key}/index.html" in index_html


def test_run_links_preserve_trace_dirs_order_not_seed_number_order(tmp_path):
    """`_run_links_cell`/`_spread_cell` render side by side in the same row, and a
    reader could reasonably assume `run_links[i]` lines up with `seed_values["turns"]
    [i]` -- nothing in the HTML enforces that pairing. What actually keeps them
    aligned today is that both `build_site` (this loop) and `pokebench score`'s
    per-(model, scenario, tier) grouping (`cli.py`'s `groups[...].append`) walk the
    *same* results_traces.txt-derived list, in file order, with a plain list-append;
    neither module sorts or re-keys by seed number. This test pins build_site's half
    of that invariant directly: feed trace_dirs in a deliberately non-ascending seed
    order and assert run_links surface in that same order, not sorted -- so a future
    change that sorts/re-groups `links_by_key` differently (silently mislabelling
    which replay link goes with which seed_values entry) fails this test loudly
    instead of shipping unnoticed."""
    for seed_no in (2, 0, 1):  # deliberately not seed0, seed1, seed2
        run_dir = tmp_path / "runs" / "bench" / "s1-gemini-t0" / "ts" / f"seed{seed_no}"
        _write_run(
            run_dir,
            [_rec(1, "aaa", 3, 6, executed=["up"])],
            meta={"model": "gemini", "tier": 0, "scenario": "s1_exit_pallet"},
        )

    traces_path = tmp_path / "results_traces.txt"
    traces_path.write_text(
        "\n".join(
            str((tmp_path / "runs" / "bench" / "s1-gemini-t0" / "ts" / f"seed{n}").resolve())
            .replace("\\", "/")
            for n in (2, 0, 1)
        )
        + "\n",
        encoding="utf-8",
    )

    results_path = tmp_path / "results.json"
    row = _row(model="gemini", scenario="s1_exit_pallet", tier=0)
    results_path.write_text(json.dumps(_doc(row)), encoding="utf-8")

    build_site(results_path, tmp_path / "dist", traces_path)
    index_html = (tmp_path / "dist" / "index.html").read_text(encoding="utf-8")

    pos2, pos0, pos1 = (index_html.index(f"seed{n}") for n in (2, 0, 1))
    assert pos2 < pos0 < pos1, "run_links order drifted from trace_dirs (file) order"


def test_build_site_reports_missing_trace_dirs_without_failing(tmp_path):
    results_path = tmp_path / "results.json"
    row = _row(model="gemini", scenario="s1_exit_pallet", tier=0)
    results_path.write_text(json.dumps(_doc(row)), encoding="utf-8")

    traces_path = tmp_path / "results_traces.txt"
    traces_path.write_text("runs/bench/does-not-exist/seed0\n", encoding="utf-8")

    report = build_site(results_path, tmp_path / "dist", traces_path)
    assert report.missing_traces == ["runs/bench/does-not-exist/seed0"]
    assert report.run_pages == 0
    assert (tmp_path / "dist" / "index.html").exists()


def test_build_site_index_uses_structured_exclusions_not_raw_provenance_dump(tmp_path):
    """End-to-end through `build_site` (the real code path, not a hand-built doc):
    the generated index.html shows the reproduce command and the exclusion reason,
    but not the multi-paragraph rationale prose that used to be dumped verbatim."""
    run_dir = tmp_path / "runs" / "bench" / "s1-gemini-t0" / "20260101-000000" / "seed0"
    _write_run(
        run_dir,
        [_rec(1, "aaa", 3, 6, executed=["up"], success=True)],
        meta={"model": "gemini", "tier": 0, "scenario": "s1_exit_pallet"},
        summary={"success": True, "stop_reason": "success", "turns": 1, "usd": 0.01},
    )

    results_path = tmp_path / "results.json"
    row = _row(
        model="gemini", scenario="s1_exit_pallet", tier=0, reasoning_provenance="not_applicable"
    )
    results_path.write_text(json.dumps(_doc(row)), encoding="utf-8")

    abs_path = str(run_dir.resolve()).replace("\\", "/")
    traces_path = tmp_path / "results_traces.txt"
    traces_path.write_text(
        "# Provenance for results.json.\n"
        "#\n"
        "# AUDITED 2026-08-04 by metrics/validity.py (build 1). Long rationale "
        "paragraph that must not appear inline on the leaderboard any more.\n"
        "#\n"
        "#   python -m uv run pokebench score $(grep -v '^#' results_traces.txt) "
        "--out results.json\n"
        "\n"
        "# --- sweep ---\n"
        f"{abs_path}\n"
        "\n# --- Deliberately EXCLUDED, and why ---\n"
        "#\n"
        "# runs/other/seed0\n"
        "#   reasoning ON, hit the ceiling.\n",
        encoding="utf-8",
    )

    out_dir = tmp_path / "web_dist"
    report = build_site(results_path, out_dir, traces_path)
    assert report.rows == 1

    index_html = (out_dir / "index.html").read_text(encoding="utf-8")
    assert "pokebench score" in index_html
    assert "reasoning ON, hit the ceiling." in index_html
    assert "AUDITED 2026-08-04" not in index_html
    assert "1 seed excluded" in index_html


def test_build_site_works_with_no_traces_file_at_all(tmp_path):
    """`--traces` is optional -- results.json alone must still build a table."""
    results_path = tmp_path / "results.json"
    row = _row(model="gemini", scenario="s1_exit_pallet", tier=0)
    results_path.write_text(json.dumps(_doc(row)), encoding="utf-8")

    report = build_site(results_path, tmp_path / "dist", traces_path=None)
    assert report.run_pages == 0
    assert (tmp_path / "dist" / "index.html").exists()


# --- repo hygiene: web/dist must be gitignored, mirroring runs/ --------------------


def test_web_dist_is_gitignored():
    gitignore = Path(__file__).resolve().parents[1] / ".gitignore"
    text = gitignore.read_text(encoding="utf-8")
    assert "web/dist" in text
