"""tests/property/test_case_analyzer_extraction_isolation.py

Property-based test for agent/case_analyzer.py's `extract_root_cause_and_fix`
-- design.md Correctness Properties, Property 7.

NOTE: This is a distinct test file from
tests/property/test_case_analyzer_extraction_order.py (task 12.6, Property
8), which is a parallel task covering the *same* `extract_root_cause_and_fix`
function but focused on fix-step order/content preservation instead. Both
tasks target `extract_root_cause_and_fix` but live in separate files by
design so the two parallel test-writing tasks never collide on a shared
file.

This file targets "isolation": the root cause and fix steps returned by
`extract_root_cause_and_fix` must be built exclusively from substrings of
the single `matched_item.text` passed in -- no words, sentences, or
information introduced from anywhere else (Requirements 6.1, 6.2). It
constructs structured markdown-like text using the exact section headings
`extract_root_cause_and_fix` looks for ("## Root Cause Criteria" /
"## Remediation Steps", matching the headings `scripts/generate_runbooks.py`
now renders), so the parser takes its "structured" code path rather than
one of its prose/list fallback paths.
"""

from __future__ import annotations

import string

from hypothesis import given, settings
from hypothesis import strategies as st

from agent.case_analyzer import extract_root_cause_and_fix
from agent.models import KBRetrievalItem

# Words are drawn from a lowercase-letters-only alphabet, deliberately
# excluding digits, underscores, and uppercase letters. This guarantees a
# randomly generated word can never literally equal (or contain as a
# substring) one of the uppercase/underscore "marker" tokens used below to
# distinguish one generated item's content from another's -- the marker
# tokens use characters (uppercase letters, "_") that this word alphabet
# cannot produce, so any observed occurrence of a marker in a function's
# output is unambiguously attributable to the item that marker was baked
# into, not to coincidental random overlap.
_word = st.text(alphabet=string.ascii_lowercase, min_size=3, max_size=8)


def _sentence_strategy(marker: str) -> st.SearchStrategy[str]:
    """A single-line "sentence" strategy: `marker` followed by 2-6 random
    lowercase words and a trailing period. Used to build both root-cause
    prose lines and remediation-step lines so each line's content is
    fully attributable to random `_word` draws plus the fixed `marker`,
    with no other source of text.
    """
    return st.lists(_word, min_size=2, max_size=6).map(
        lambda words: f"{marker} {' '.join(words)}."
    )


def _build_markdown(
    marker: str,
    root_cause_lines: list[str],
    fix_lines: list[str],
    title: str = "Test Runbook",
) -> str:
    """Render a minimal but realistically-structured runbook chunk.

    Mirrors the heading structure `scripts/generate_runbooks.py`'s
    `_render_markdown` now writes ("## Root Cause Criteria" as a bulleted
    list, "## Remediation Steps" as a numbered list), which is exactly the
    structure `agent/case_analyzer.py`'s `extract_root_cause_and_fix` is
    written to parse via its heading-classification pass.

    A "## Description" section (containing prose that also carries
    `marker`) is included between the title and the root-cause section so
    the test also exercises that description prose is correctly *not*
    absorbed into either the root cause or the fix steps -- it sits under
    an "other"-classified heading, so it must be excluded from both
    returned values even though it textually contains `marker`.
    """
    lines: list[str] = [
        f"# {title}",
        "",
        "## Description",
        "",
        f"{marker} description prose that must not appear in either result.",
        "",
        "## Root Cause Criteria",
        "",
    ]
    for line in root_cause_lines:
        lines.append(f"- {line}")
    lines.append("")
    lines.append("## Remediation Steps")
    lines.append("")
    for index, line in enumerate(fix_lines, start=1):
        lines.append(f"{index}. {line}")
    lines.append("")
    return "\n".join(lines)


@st.composite
def _structured_item(draw: st.DrawFn) -> tuple[list[str], list[str], KBRetrievalItem]:
    """Draw a `(root_cause_lines, fix_lines, KBRetrievalItem)` triple: a
    single `KBRetrievalItem` whose `text` is a structured markdown chunk
    built from `root_cause_lines` (one or more, rendered as a bulleted
    "## Root Cause Criteria" list) and `fix_lines` (one or more, rendered
    as a numbered "## Remediation Steps" list), plus the two source lists
    the test uses to compute the expected `extract_root_cause_and_fix`
    output.
    """
    marker = "LINE"
    root_cause_lines = draw(
        st.lists(_sentence_strategy(marker), min_size=1, max_size=3, unique=True)
    )
    fix_lines = draw(
        st.lists(_sentence_strategy(marker), min_size=1, max_size=6, unique=True)
    )
    score = draw(
        st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False)
    )
    source_uri = draw(st.text(min_size=1, max_size=30))
    issue_category = draw(st.one_of(st.none(), st.text(min_size=1, max_size=20)))

    text = _build_markdown(marker, root_cause_lines, fix_lines)
    item = KBRetrievalItem(
        text=text,
        score=score,
        source_uri=source_uri,
        issue_category=issue_category,
    )
    return root_cause_lines, fix_lines, item


# Feature: network-ops-agent, Property 7: Root cause and fix derive only from the matching runbook
#
# For structured runbook text combining a "## Root Cause Criteria" section
# and a "## Remediation Steps" section, the root cause and fix steps
# returned by `extract_root_cause_and_fix` are built exclusively from
# substrings of `matched_item.text`: every root-cause component and every
# fix step is a line that appears verbatim (after list-marker stripping)
# in the source text, in the source's relative order, and no extra text
# (from the description section or anywhere else) is introduced.
#
# Validates: Requirements 6.1, 6.2
@given(structured=_structured_item())
@settings(max_examples=100)
def test_root_cause_and_fix_are_built_only_from_source_text_substrings(
    structured: tuple[list[str], list[str], KBRetrievalItem],
) -> None:
    root_cause_lines, fix_lines, item = structured

    root_cause, fix_steps = extract_root_cause_and_fix(item)

    # The root cause is exactly the "; "-join of the authored root-cause
    # lines -- i.e. every component of the returned root cause is one of
    # the exact lines placed under "## Root Cause Criteria", and nothing
    # else (no words added, none dropped, none reordered).
    assert root_cause == "; ".join(root_cause_lines)
    for component in root_cause.split("; "):
        assert component in root_cause_lines
        # Every component must be traceable back to a literal line in the
        # source text (the bulleted "- {component}" line).
        assert component in item.text

    # The fix steps are exactly the authored fix lines, in the same order
    # they were authored (and thus the same order they appear in the
    # source text) -- no step introduced, dropped, or reordered.
    assert fix_steps == fix_lines
    for step in fix_steps:
        assert step in item.text

    # The description section's prose is never absorbed into either
    # result, even though it shares the same `marker` token as the root
    # cause/fix content and would otherwise be indistinguishable from it
    # by content alone.
    description_line = "description prose that must not appear in either result."
    assert description_line not in root_cause
    for step in fix_steps:
        assert description_line not in step


def _assert_output_traceable_only_to_own_item(
    own_marker: str,
    other_marker: str,
    root_cause: str,
    fix_steps: list[str],
) -> None:
    """Assert that `root_cause`/`fix_steps` (extracted from one item) carry
    only `own_marker`'s content and never `other_marker`'s -- i.e. the
    extraction for one item never mixed in content from a different item.
    """
    assert other_marker not in root_cause
    assert own_marker in root_cause
    for step in fix_steps:
        assert other_marker not in step
        assert own_marker in step


@st.composite
def _two_independent_structured_items(
    draw: st.DrawFn,
) -> tuple[KBRetrievalItem, KBRetrievalItem]:
    """Draw two independently-generated, structured `KBRetrievalItem`s
    ("item A" and "item B"), each tagged with its own marker token
    ("MARKER_A" / "MARKER_B") that -- by construction of the `_word`
    alphabet above -- cannot appear in the other item's randomly-generated
    content by coincidence.
    """

    def _draw_item(marker: str) -> KBRetrievalItem:
        root_cause_lines = draw(
            st.lists(_sentence_strategy(marker), min_size=1, max_size=3, unique=True)
        )
        fix_lines = draw(
            st.lists(_sentence_strategy(marker), min_size=1, max_size=6, unique=True)
        )
        text = _build_markdown(marker, root_cause_lines, fix_lines)
        return KBRetrievalItem(
            text=text,
            score=draw(
                st.floats(
                    min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False
                )
            ),
            source_uri=draw(st.text(min_size=1, max_size=30)),
            issue_category=draw(st.one_of(st.none(), st.text(min_size=1, max_size=20))),
        )

    item_a = _draw_item("MARKER_A")
    item_b = _draw_item("MARKER_B")
    return item_a, item_b


# Mirrors Property 7 above by testing isolation *across* two independently
# generated items: extracting from item A must never mix in content that
# only exists in item B's source text, and vice versa. This guards against
# any future regression (e.g. shared mutable state, module-level caching)
# that could leak content between calls, even though `extract_root_cause_
# and_fix` is currently a pure, stateless function of its single argument.
#
# Feature: network-ops-agent, Property 7: Root cause and fix derive only from the matching runbook
# Validates: Requirements 6.1, 6.2
@given(items=_two_independent_structured_items())
@settings(max_examples=100)
def test_extraction_never_mixes_content_between_independent_items(
    items: tuple[KBRetrievalItem, KBRetrievalItem],
) -> None:
    item_a, item_b = items

    root_cause_a, fix_steps_a = extract_root_cause_and_fix(item_a)
    root_cause_b, fix_steps_b = extract_root_cause_and_fix(item_b)

    _assert_output_traceable_only_to_own_item(
        "MARKER_A", "MARKER_B", root_cause_a, fix_steps_a
    )
    _assert_output_traceable_only_to_own_item(
        "MARKER_B", "MARKER_A", root_cause_b, fix_steps_b
    )

    # Every piece of item A's output must be a literal substring of item
    # A's own source text, and never of item B's source text (and
    # symmetrically for item B), directly confirming "item A's output
    # only contains content from item A's own source text".
    for component in root_cause_a.split("; "):
        assert component in item_a.text
        assert component not in item_b.text
    for step in fix_steps_a:
        assert step in item_a.text
        assert step not in item_b.text

    for component in root_cause_b.split("; "):
        assert component in item_b.text
        assert component not in item_a.text
    for step in fix_steps_b:
        assert step in item_b.text
        assert step not in item_a.text
