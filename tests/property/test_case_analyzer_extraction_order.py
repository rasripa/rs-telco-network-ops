"""tests/property/test_case_analyzer_extraction_order.py

Property-based test for agent/case_analyzer.py's `extract_root_cause_and_fix`
-- design.md Correctness Properties, Property 8.

NOTE: This is a distinct test file from
tests/property/test_case_analyzer_extraction_isolation.py (task 12.5,
Property 7), which is a parallel task covering the *same*
`extract_root_cause_and_fix` function but focused on isolation (no content
leaking in from a non-matching item or from an unrelated section) instead.
Both tasks target `extract_root_cause_and_fix` but live in separate files
by design so the two parallel test-writing tasks never collide on a shared
file.

This file targets "order and content preservation" of the `fix_steps`
list specifically: for a "## Remediation Steps" numbered-list section
containing N distinct, randomly generated fix-step sentences in a
specific authored order, the `fix_steps` list returned by
`extract_root_cause_and_fix` must have exactly the same length, exactly
the same content (each step matching its authored sentence verbatim,
after list-marker stripping), and -- critically -- the same relative
order as authored, not merely the same set (Requirement 6.3).
"""

from __future__ import annotations

import string

from hypothesis import given, settings
from hypothesis import strategies as st

from agent.case_analyzer import extract_root_cause_and_fix
from agent.models import KBRetrievalItem

# Words are drawn from a lowercase-letters-only alphabet. This keeps
# generated sentences free of characters (digits, "#", "-", "*", "+", ":")
# that the parser's heading/list-marker regexes treat specially, so every
# generated sentence is unambiguously a single ordinary list-item line once
# rendered under a "N. " numbered marker.
_word = st.text(alphabet=string.ascii_lowercase, min_size=3, max_size=8)


def _step_sentence() -> st.SearchStrategy[str]:
    """A single-line "sentence" strategy: 2-6 random lowercase words and a
    trailing period, with no fixed/hand-picked content, so the property is
    exercised over generally varied fix-step text rather than one example.
    """
    return st.lists(_word, min_size=2, max_size=6).map(lambda words: " ".join(words) + ".")


def _build_remediation_markdown(
    fix_steps: list[str],
    root_cause_lines: list[str] | None = None,
    title: str = "Test Runbook",
) -> str:
    """Render a minimal runbook chunk with a "## Remediation Steps" section
    rendered as a numbered list, mirroring the heading structure
    `scripts/generate_runbooks.py` writes and that
    `agent/case_analyzer.py`'s `extract_root_cause_and_fix` is written to
    parse via its heading-classification pass. An optional
    "## Root Cause Criteria" bulleted section is included beforehand to
    confirm the fix-step section boundary is still respected when another
    recognized section precedes it.
    """
    lines: list[str] = [f"# {title}", "", "## Description", "", "Some description prose.", ""]
    if root_cause_lines:
        lines.append("## Root Cause Criteria")
        lines.append("")
        for line in root_cause_lines:
            lines.append(f"- {line}")
        lines.append("")
    lines.append("## Remediation Steps")
    lines.append("")
    for index, step in enumerate(fix_steps, start=1):
        lines.append(f"{index}. {step}")
    lines.append("")
    return "\n".join(lines)


def _make_item(text: str) -> KBRetrievalItem:
    return KBRetrievalItem(
        text=text,
        score=0.5,
        source_uri="s3://runbooks/test-runbook.md",
        issue_category="test-category",
    )


@st.composite
def _ordered_fix_steps_item(
    draw: st.DrawFn,
) -> tuple[list[str], KBRetrievalItem]:
    """Draw a `(fix_steps, KBRetrievalItem)` pair: `fix_steps` is a list of
    1-12 distinct, randomly generated sentences in a specific (randomly
    drawn, not necessarily sorted) order, and the `KBRetrievalItem`'s
    `text` renders exactly that ordered list under "## Remediation Steps".
    An optional "## Root Cause Criteria" section (0-3 distinct lines) is
    also drawn to confirm its presence doesn't disturb fix-step ordering.
    """
    fix_steps = draw(st.lists(_step_sentence(), min_size=1, max_size=12, unique=True))
    root_cause_lines = draw(st.lists(_step_sentence(), min_size=0, max_size=3, unique=True))
    text = _build_remediation_markdown(fix_steps, root_cause_lines)
    return fix_steps, _make_item(text)


@st.composite
def _shuffled_fix_steps_item(
    draw: st.DrawFn,
) -> tuple[list[str], KBRetrievalItem]:
    """Draw a `(shuffled_steps, KBRetrievalItem)` pair: a base set of 2-10
    distinct sentences is drawn, then independently permuted via
    `st.permutations` into `shuffled_steps`, which is the exact order
    rendered into the source text. Because the permutation is drawn
    independently of any "natural" ordering of the sentence content
    (e.g. alphabetical), this specifically exercises that
    `extract_root_cause_and_fix` reproduces *this* authored order and not
    some other canonical ordering of the same set of steps.
    """
    base_steps = draw(st.lists(_step_sentence(), min_size=2, max_size=10, unique=True))
    shuffled_steps = list(draw(st.permutations(base_steps)))
    text = _build_remediation_markdown(shuffled_steps)
    return shuffled_steps, _make_item(text)


# Feature: network-ops-agent, Property 8: Fix recommendation order and content preservation
#
# For a "## Remediation Steps" numbered-list section containing N distinct,
# randomly generated fix-step sentences in a specific authored order, the
# `fix_steps` list returned by `extract_root_cause_and_fix` has exactly the
# same length as the authored list, contains exactly the same content
# (each authored sentence present verbatim, after list-marker stripping),
# and preserves the exact same relative order as authored -- not merely
# the same set of steps.
#
# Validates: Requirements 6.3
@given(ordered=_ordered_fix_steps_item())
@settings(max_examples=100)
def test_fix_steps_preserve_length_content_and_order(
    ordered: tuple[list[str], KBRetrievalItem],
) -> None:
    fix_steps, item = ordered

    _, extracted_fix_steps = extract_root_cause_and_fix(item)

    # Same length: no step introduced, dropped, or merged.
    assert len(extracted_fix_steps) == len(fix_steps)

    # Same content, same order: the extracted list is exactly the authored
    # list (list-marker stripping already applied by the parser), not just
    # a permutation or subset of it.
    assert extracted_fix_steps == fix_steps

    # Every extracted step is traceable back to a literal authored
    # sentence -- no step is fabricated or altered.
    for step in extracted_fix_steps:
        assert step in fix_steps

    # Explicit relative-order check: for every pair of distinct authored
    # steps, their relative order in the extracted list matches their
    # relative order as authored. (Implied by the exact-equality assertion
    # above, but asserted independently here since this is the crux of
    # the property under test.)
    for i in range(len(fix_steps)):
        for j in range(i + 1, len(fix_steps)):
            assert extracted_fix_steps.index(fix_steps[i]) < extracted_fix_steps.index(
                fix_steps[j]
            )


# Feature: network-ops-agent, Property 8: Fix recommendation order and content preservation
#
# For any independently-drawn permutation of a set of distinct fix-step
# sentences, `extract_root_cause_and_fix` reproduces *that specific*
# authored order verbatim -- confirming the returned order is derived from
# the source text's own ordering, not from some other canonical ordering
# (e.g. alphabetical, or the order steps were first drawn) of the same
# underlying set of steps.
#
# Validates: Requirements 6.3
@given(shuffled=_shuffled_fix_steps_item())
@settings(max_examples=100)
def test_fix_steps_preserve_arbitrary_shuffled_order(
    shuffled: tuple[list[str], KBRetrievalItem],
) -> None:
    shuffled_steps, item = shuffled

    _, extracted_fix_steps = extract_root_cause_and_fix(item)

    assert len(extracted_fix_steps) == len(shuffled_steps)
    assert extracted_fix_steps == shuffled_steps
    assert set(extracted_fix_steps) == set(shuffled_steps)
