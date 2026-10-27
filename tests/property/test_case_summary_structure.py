"""tests/property/test_case_summary_structure.py

Property-based test for agent/case_analyzer.py's `build_case_summary`
structural completeness -- design.md Correctness Properties, Property 9.

NOTE: This is a distinct test file from
tests/property/test_case_summary_isolation.py (task 12.9, Property 10),
which is a parallel task that also targets `build_case_summary` but
covers cross-record data isolation instead of per-record structural
completeness. Both tests target the same function but live in separate
files by design so the two parallel test-writing tasks never collide on a
shared file.
"""

from __future__ import annotations

import string

from hypothesis import example, given, settings
from hypothesis import strategies as st

from agent.case_analyzer import build_case_summary
from agent.models import CaseRecord, KBRetrievalItem, ValidityDetermination

_VALID_STATUSES = ["Open", "In Progress", "Resolved", "Closed"]

# A restricted alphabet (letters, digits, spaces) for freeform text used
# inside a synthesized runbook chunk. Excluding markdown-significant
# characters ("#", "-", "*", "+", ".", ")") keeps generated prose/step
# text from being accidentally misparsed as a heading or list-item marker
# by `extract_root_cause_and_fix`'s line-oriented parser, so the test can
# reliably assert on a non-empty, order-preserved fix-step list without
# also having to duplicate that function's parsing rules here.
_WORD_ALPHABET = string.ascii_letters + string.digits + " "

_word_text = (
    st.text(alphabet=_WORD_ALPHABET, min_size=1, max_size=30)
    .map(str.strip)
    .filter(lambda s: s != "")
)


def _case_record_strategy() -> st.SearchStrategy[CaseRecord]:
    """Build arbitrary, structurally valid `CaseRecord`s covering the
    fields `build_case_summary` copies verbatim into the `CaseSummary`."""
    return st.builds(
        CaseRecord,
        case_id=st.text(min_size=1, max_size=16),
        customer_name=st.text(min_size=1, max_size=30),
        customer_account_id=st.text(min_size=1, max_size=30),
        issue_description=st.text(min_size=1, max_size=60),
        network_issue_category=st.text(min_size=1, max_size=20),
        service_location_id=st.text(min_size=1, max_size=16),
        status=st.sampled_from(_VALID_STATUSES),
        created_at=st.just("2024-01-01T00:00:00Z"),
        updated_at=st.just("2024-01-02T00:00:00Z"),
    )


def _runbook_chunk_text(root_cause_sentence: str, steps: list[str]) -> str:
    """Synthesize a realistic runbook chunk with a labeled root-cause
    section and a labeled remediation section, so
    `extract_root_cause_and_fix` (invoked internally by
    `build_case_summary` for a valid determination) reliably derives a
    non-empty root cause and a non-empty, order-preserving fix-step list
    from it, matching the structure the Runbook_Generator's rendered
    documents are expected to produce."""
    lines = ["## Root Cause", root_cause_sentence, "", "## Remediation Steps"]
    lines.extend(f"- {step}" for step in steps)
    return "\n".join(lines)


@st.composite
def _case_and_determination(
    draw: st.DrawFn,
) -> tuple[CaseRecord, ValidityDetermination]:
    """Draw a `(CaseRecord, ValidityDetermination)` pair covering both the
    valid-with-matched-item and invalid-with-no-matched-item cases."""
    case = draw(_case_record_strategy())
    rationale = draw(st.text(min_size=1, max_size=80))
    is_valid = draw(st.booleans())

    if not is_valid:
        determination = ValidityDetermination(
            valid=False,
            matched_item=None,
            rationale=rationale,
        )
        return case, determination

    root_cause_sentence = draw(_word_text)
    steps = draw(st.lists(_word_text, min_size=1, max_size=5))
    source_uri = draw(st.text(min_size=1, max_size=30))
    score = draw(
        st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False)
    )

    matched_item = KBRetrievalItem(
        text=_runbook_chunk_text(root_cause_sentence, steps),
        score=score,
        source_uri=source_uri,
        issue_category=case.network_issue_category,
    )
    determination = ValidityDetermination(
        valid=True,
        matched_item=matched_item,
        rationale=rationale,
    )
    return case, determination


def _make_case(case_id: str = "case-1") -> CaseRecord:
    return CaseRecord(
        case_id=case_id,
        customer_name="Jamie Rivera",
        customer_account_id="AnyTel-000123",
        issue_description="No internet connectivity since this morning.",
        network_issue_category="connectivity_outage",
        service_location_id="loc-42",
        status="Open",
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-02T00:00:00Z",
    )


# Feature: network-ops-agent, Property 9: Case summary structural completeness
#
# For any `CaseRecord` and any `ValidityDetermination` (valid or invalid)
# with correspondingly supplied root cause/fix, `build_case_summary`
# returns a `CaseSummary` exposing `case_id`, `customer_name`,
# `issue_description`, and `validity` as distinct, individually
# addressable fields; when `valid` is true, `root_cause`, `fix_steps`, and
# `runbook_reference` are all populated; when `valid` is false, all three
# are `None` and the summary's rationale states that no runbook-supported
# resolution applies.
#
# Validates: Requirements 5.6, 7.1, 7.2, 7.3
@given(case_and_determination=_case_and_determination())
@settings(max_examples=100)
# Deterministic edge cases: a minimal valid determination with exactly one
# remediation step, and a plain invalid determination with no matched
# item, to make the property's two branches easy to verify at a glance
# alongside the generated-input coverage.
@example(
    (
        _make_case("case-valid-minimal"),
        ValidityDetermination(
            valid=True,
            matched_item=KBRetrievalItem(
                text="## Root Cause\nDegraded signal at the local node.\n\n"
                "## Remediation Steps\n- Restart the router",
                score=0.9,
                source_uri="runbooks/connectivity-outage.md",
                issue_category="connectivity_outage",
            ),
            rationale="Category matched; case determined to be valid.",
        ),
    )
)
@example(
    (
        _make_case("case-invalid-minimal"),
        ValidityDetermination(
            valid=False,
            matched_item=None,
            rationale="Category did not match any retrieved runbook content.",
        ),
    )
)
def test_case_summary_structural_completeness(
    case_and_determination: tuple[CaseRecord, ValidityDetermination],
) -> None:
    case, determination = case_and_determination

    summary = build_case_summary(case, determination)

    # Requirements 7.1, 7.3: case_id/customer_name/issue_description are
    # copied verbatim from `case` as distinct, individually addressable
    # fields.
    assert summary.case_id == case.case_id
    assert summary.customer_name == case.customer_name
    assert summary.issue_description == case.issue_description

    # Requirements 5.4 (via CaseSummary), 7.1: validity is expressed as
    # exactly "valid" or "invalid", matching the determination.
    assert summary.validity in ("valid", "invalid")
    assert summary.validity == ("valid" if determination.valid else "invalid")

    # Rationale is always populated.
    assert isinstance(summary.rationale, str)
    assert summary.rationale != ""

    if determination.valid:
        # Requirement 7.2: root cause, fix steps, and runbook reference
        # are all included for a valid case.
        assert summary.root_cause is not None
        assert summary.fix_steps is not None
        assert isinstance(summary.fix_steps, list)
        assert len(summary.fix_steps) > 0
        assert summary.runbook_reference is not None
    else:
        # Requirement 5.6/7.2: all three are omitted (None) for an
        # invalid case, and the rationale states that no
        # runbook-supported resolution applies.
        assert summary.root_cause is None
        assert summary.fix_steps is None
        assert summary.runbook_reference is None
        assert "no runbook-supported resolution applies" in summary.rationale.lower()
