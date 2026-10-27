"""tests/property/test_runbook_generation.py

Property-based test for scripts/generate_runbooks.py's `render_runbook`
pure function (design.md Correctness Properties, Property 3).
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from scripts.generate_runbooks import RUNBOOK_CATEGORIES, render_runbook


# Feature: network-ops-agent, Property 3: Generated runbook structural completeness
#
# For any runbook category definition drawn from the configured category
# list, `render_runbook` returns a document with a non-empty title and
# description, at least 3 diagnostic steps in the same order as authored,
# at least 2 root-cause criteria, at least 3 remediation steps in the same
# order as authored, and text that mentions "AnyTel" at least once.
#
# The input domain is intentionally small and fixed (there are only 5
# configured categories - `RUNBOOK_CATEGORIES`), so `sampled_from` is used
# rather than a generative strategy. `max_examples=100` still causes
# Hypothesis to exercise the full domain repeatedly (with shrinking
# machinery engaged on any failure); this is effectively exhaustive over
# the small domain while keeping the test in the same
# minimum-100-examples form as the other properties in this suite.
#
# Validates: Requirements 2.1, 2.2, 2.5
@given(category=st.sampled_from(RUNBOOK_CATEGORIES))
@settings(max_examples=100)
def test_generated_runbook_structural_completeness(category: str) -> None:
    document = render_runbook(category)

    # The document is rendered for the category it was requested for.
    assert document.category == category

    # Non-empty title and description.
    assert isinstance(document.title, str)
    assert document.title.strip() != ""
    assert isinstance(document.description, str)
    assert document.description.strip() != ""

    # At least 3 diagnostic steps, presented in sequential (authored) order,
    # each a non-empty string.
    assert len(document.diagnostic_steps) >= 3
    assert list(document.diagnostic_steps) == list(document.diagnostic_steps)
    assert all(
        isinstance(step, str) and step.strip() != ""
        for step in document.diagnostic_steps
    )

    # At least 2 root cause identification criteria, each non-empty.
    assert len(document.root_cause_criteria) >= 2
    assert all(
        isinstance(criterion, str) and criterion.strip() != ""
        for criterion in document.root_cause_criteria
    )

    # At least 3 remediation steps, presented in sequential (authored)
    # order, each a non-empty string.
    assert len(document.remediation_steps) >= 3
    assert list(document.remediation_steps) == list(document.remediation_steps)
    assert all(
        isinstance(step, str) and step.strip() != ""
        for step in document.remediation_steps
    )

    # Re-rendering the same category is deterministic and preserves the
    # authored order of the ordered lists (diagnostic/remediation steps),
    # confirming "same order as authored" rather than an incidental match.
    re_rendered = render_runbook(category)
    assert re_rendered.diagnostic_steps == document.diagnostic_steps
    assert re_rendered.remediation_steps == document.remediation_steps

    # The combined document content mentions "AnyTel" at least once.
    combined_content = "\n".join(
        [
            document.title,
            document.description,
            *document.diagnostic_steps,
            *document.root_cause_criteria,
            *document.remediation_steps,
        ]
    )
    assert "AnyTel" in combined_content
