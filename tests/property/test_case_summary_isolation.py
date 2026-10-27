"""tests/property/test_case_summary_isolation.py

Property-based test for agent/case_analyzer.py's `build_case_summary`
-- design.md Correctness Properties, Property 10.

NOTE: This is a distinct test file from
tests/property/test_case_summary_structure.py (task 12.8, Property 9),
which is a parallel/prior task covering `build_case_summary`'s structural
completeness (which fields are populated vs. `None` for a single case).
This file targets a different concern -- "isolation across records" --
and lives in its own file by design so the two parallel/related
test-writing tasks never collide on a shared file.

This file builds two independently-generated `CaseRecord`s ("case A" and
"case B") with distinct `case_id`, `customer_name`, and
`issue_description`, and two independent `ValidityDetermination`s (each
carrying its own distinct `KBRetrievalItem` content when valid), calls
`build_case_summary` for each independently, and asserts that the summary
built for one case's data never contains content that only exists in the
other case's inputs -- confirming that building a summary for one case
never leaks or mixes in data belonging to another case record processed
in the same batch/session.

Isolation is verified via marker tokens: every piece of "case A" content
(case_id, customer_name, issue_description, category, root cause, fix
steps, runbook reference) is tagged with the literal substring
"MARKER_A", and every piece of "case B" content is tagged with
"MARKER_B". Filler content is drawn from a lowercase-only alphabet, which
cannot coincidentally produce an uppercase+underscore marker token, so any
observed occurrence of "MARKER_A" in case B's summary (or vice versa)
would unambiguously indicate a real isolation failure rather than a
coincidental string collision. This mirrors the marker-based isolation
technique used in tests/property/test_case_analyzer_extraction_isolation.py
for the related `extract_root_cause_and_fix` function.
"""

from __future__ import annotations

import string

from hypothesis import given, settings
from hypothesis import strategies as st

from agent.case_analyzer import build_case_summary
from agent.models import CaseRecord, CaseSummary, KBRetrievalItem, ValidityDetermination

_VALID_STATUSES = ["Open", "In Progress", "Resolved", "Closed"]

# Lowercase-only filler words. Marker tokens below use uppercase letters
# and underscores, characters this alphabet can never produce, so a
# marker can only ever appear in generated text because it was
# deliberately embedded as a label prefix -- never by coincidence.
_word = st.text(alphabet=string.ascii_lowercase, min_size=3, max_size=8)

_scores = st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False)


def _labeled_value(label: str) -> st.SearchStrategy[str]:
    """A string strategy producing values of the form
    "{label}_word_word_..." -- i.e. every drawn value is guaranteed to
    contain `label` as a literal substring, with 1-4 random lowercase
    filler words appended."""
    return st.lists(_word, min_size=1, max_size=4).map(
        lambda words: f"{label}_" + "_".join(words)
    )


@st.composite
def _case_summary_input(
    draw: st.DrawFn, tag: str
) -> tuple[CaseRecord, ValidityDetermination, str | None, list[str] | None]:
    """Draw a single `(CaseRecord, ValidityDetermination, root_cause,
    fix_steps)` tuple whose every piece of content is tagged with
    "MARKER_{tag}" (e.g. "MARKER_A" or "MARKER_B"). `valid` is drawn
    independently per call, so both a valid and an invalid case are
    exercised across the generated examples.

    When valid, the `ValidityDetermination` carries its own distinct
    `KBRetrievalItem` (also tagged with the same marker), and pre-computed
    `root_cause`/`fix_steps` are returned alongside so the test can assert
    the built summary's fields equal these exact values.
    """
    marker = f"MARKER_{tag}"

    case_id = draw(_labeled_value(f"{marker}_CASEID"))
    customer_name = draw(_labeled_value(f"{marker}_CUSTOMER"))
    issue_description = draw(_labeled_value(f"{marker}_ISSUE"))
    category = draw(_labeled_value(f"{marker}_CATEGORY"))

    case = CaseRecord(
        case_id=case_id,
        customer_name=customer_name,
        customer_account_id=f"AnyTel-{marker}",
        issue_description=issue_description,
        network_issue_category=category,
        service_location_id=f"{marker}_LOC",
        status=draw(st.sampled_from(_VALID_STATUSES)),
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-02T00:00:00Z",
    )

    valid = draw(st.booleans())

    if valid:
        source_uri = draw(_labeled_value(f"{marker}_RUNBOOK"))
        root_cause = draw(_labeled_value(f"{marker}_ROOTCAUSE"))
        fix_steps = draw(
            st.lists(
                _labeled_value(f"{marker}_FIXSTEP"),
                min_size=1,
                max_size=4,
                unique=True,
            )
        )
        matched_item = KBRetrievalItem(
            text=draw(_labeled_value(f"{marker}_TEXT")),
            score=draw(_scores),
            source_uri=source_uri,
            issue_category=category,
        )
        rationale = f"{marker}_RATIONALE referencing {category}"
        determination = ValidityDetermination(
            valid=True,
            matched_item=matched_item,
            rationale=rationale,
        )
        return case, determination, root_cause, fix_steps

    rationale = f"{marker}_RATIONALE invalid for {category}"
    determination = ValidityDetermination(
        valid=False,
        matched_item=None,
        rationale=rationale,
    )
    return case, determination, None, None


@st.composite
def _two_independent_case_summary_inputs(
    draw: st.DrawFn,
) -> tuple[
    tuple[CaseRecord, ValidityDetermination, str | None, list[str] | None],
    tuple[CaseRecord, ValidityDetermination, str | None, list[str] | None],
]:
    """Draw two independently-generated case summary inputs, tagged
    "A" and "B" respectively."""
    input_a = draw(_case_summary_input("A"))
    input_b = draw(_case_summary_input("B"))
    return input_a, input_b


def _string_fields(summary: CaseSummary) -> list[str]:
    """All of a `CaseSummary`'s populated string-valued fields, flattened
    into a single list (including each individual fix step, when
    present) -- the full set of places cross-record content could leak
    into."""
    fields = [summary.case_id, summary.customer_name, summary.issue_description]
    if summary.root_cause is not None:
        fields.append(summary.root_cause)
    if summary.runbook_reference is not None:
        fields.append(summary.runbook_reference)
    if summary.fix_steps is not None:
        fields.extend(summary.fix_steps)
    return fields


# Feature: network-ops-agent, Property 10: Case summary data isolation across records
#
# For any set of two or more distinct CaseRecords, the CaseSummary built
# for one record's case_id has case_id, customer_name, issue_description,
# root_cause, fix_steps, and runbook_reference values that equal that
# record's own analysis outputs, and contain none of the distinguishing
# field values (customer name, issue description, or derived findings)
# belonging to any of the other records in the set.
#
# Validates: Requirements 7.4, 7.5
@given(inputs=_two_independent_case_summary_inputs())
@settings(max_examples=100)
def test_case_summary_never_leaks_content_between_independent_records(
    inputs: tuple[
        tuple[CaseRecord, ValidityDetermination, str | None, list[str] | None],
        tuple[CaseRecord, ValidityDetermination, str | None, list[str] | None],
    ],
) -> None:
    (case_a, determination_a, root_cause_a, fix_steps_a) = inputs[0]
    (case_b, determination_b, root_cause_b, fix_steps_b) = inputs[1]

    # Case A and case B are distinct on every distinguishing field, by
    # construction of the "MARKER_A"/"MARKER_B" tagging above.
    assert case_a.case_id != case_b.case_id
    assert case_a.customer_name != case_b.customer_name
    assert case_a.issue_description != case_b.issue_description

    # build_case_summary is called independently for each case -- neither
    # call is passed any information about the other case.
    summary_a = build_case_summary(
        case_a, determination_a, root_cause=root_cause_a, fix_steps=fix_steps_a
    )
    summary_b = build_case_summary(
        case_b, determination_b, root_cause=root_cause_b, fix_steps=fix_steps_b
    )

    # Each summary's own-case fields correspond exactly to that same
    # case's own inputs (Requirement 7.4).
    for summary, case, determination, root_cause, fix_steps in (
        (summary_a, case_a, determination_a, root_cause_a, fix_steps_a),
        (summary_b, case_b, determination_b, root_cause_b, fix_steps_b),
    ):
        assert summary.case_id == case.case_id
        assert summary.customer_name == case.customer_name
        assert summary.issue_description == case.issue_description

        if determination.valid:
            assert summary.root_cause == root_cause
            assert summary.fix_steps == fix_steps
            assert determination.matched_item is not None
            assert summary.runbook_reference == determination.matched_item.source_uri
        else:
            assert summary.root_cause is None
            assert summary.fix_steps is None
            assert summary.runbook_reference is None

    # Sanity check that the assertions below are not vacuous: each
    # summary's own case_id really does carry its own marker.
    assert "MARKER_A" in summary_a.case_id
    assert "MARKER_B" in summary_b.case_id

    # Core isolation property (Requirement 7.5): summary A never contains
    # any content that only exists in case B's inputs, and vice versa.
    for value in _string_fields(summary_a):
        assert "MARKER_B" not in value
    for value in _string_fields(summary_b):
        assert "MARKER_A" not in value


def _fixed_case(tag: str, category: str = "connectivity_outage") -> CaseRecord:
    return CaseRecord(
        case_id=f"case-{tag}",
        customer_name=f"Customer {tag}",
        customer_account_id=f"AnyTel-{tag}",
        issue_description=f"Issue description for {tag}",
        network_issue_category=category,
        service_location_id=f"loc-{tag}",
        status="Open",
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-02T00:00:00Z",
    )


def _fixed_valid_determination(tag: str, category: str) -> ValidityDetermination:
    matched_item = KBRetrievalItem(
        text=f"Runbook chunk for {tag}",
        score=0.75,
        source_uri=f"runbooks/{tag}.md",
        issue_category=category,
    )
    return ValidityDetermination(
        valid=True,
        matched_item=matched_item,
        rationale=f"Category '{category}' matched for {tag}; case is valid.",
    )


def _fixed_invalid_determination(tag: str, category: str) -> ValidityDetermination:
    return ValidityDetermination(
        valid=False,
        matched_item=None,
        rationale=f"Category '{category}' did not match for {tag}; case is invalid.",
    )


def test_case_summary_isolation_when_both_cases_valid() -> None:
    """Deterministic edge case: both case A and case B are valid, each
    with their own distinct root cause/fix content, and neither summary
    should contain the other's findings."""
    case_a = _fixed_case("A", category="connectivity_outage")
    case_b = _fixed_case("B", category="mobile_signal")

    summary_a = build_case_summary(
        case_a,
        _fixed_valid_determination("A", "connectivity_outage"),
        root_cause="Fiber cut near the local exchange for A.",
        fix_steps=["Dispatch a technician for A.", "Restore the fiber splice for A."],
    )
    summary_b = build_case_summary(
        case_b,
        _fixed_valid_determination("B", "mobile_signal"),
        root_cause="Tower antenna misalignment for B.",
        fix_steps=["Realign the antenna for B.", "Re-run signal tests for B."],
    )

    assert summary_a.case_id == "case-A"
    assert summary_b.case_id == "case-B"
    assert summary_a.root_cause is not None and "for A" in summary_a.root_cause
    assert summary_b.root_cause is not None and "for B" in summary_b.root_cause
    assert "for B" not in summary_a.root_cause
    assert "for A" not in summary_b.root_cause
    for step in summary_a.fix_steps or []:
        assert "for B" not in step
    for step in summary_b.fix_steps or []:
        assert "for A" not in step
    assert "case-B" not in summary_a.case_id
    assert "case-A" not in summary_b.case_id


def test_case_summary_isolation_when_one_valid_and_one_invalid() -> None:
    """Deterministic edge case: case A is valid and case B is invalid (or
    vice versa) -- the invalid case's summary must have no root
    cause/fix/runbook reference at all (so nothing to leak into), and the
    valid case's findings must not appear anywhere in the invalid
    summary."""
    case_a = _fixed_case("A", category="connectivity_outage")
    case_b = _fixed_case("B", category="unmatched_category")

    summary_a = build_case_summary(
        case_a,
        _fixed_valid_determination("A", "connectivity_outage"),
        root_cause="Fiber cut near the local exchange for A.",
        fix_steps=["Dispatch a technician for A."],
    )
    summary_b = build_case_summary(
        case_b,
        _fixed_invalid_determination("B", "unmatched_category"),
    )

    assert summary_a.root_cause is not None
    assert summary_a.fix_steps is not None
    assert summary_a.runbook_reference is not None

    assert summary_b.root_cause is None
    assert summary_b.fix_steps is None
    assert summary_b.runbook_reference is None

    assert summary_a.root_cause is not None
    for value in _string_fields(summary_b):
        assert "case-A" not in value
        assert summary_a.root_cause not in value


def test_case_summary_isolation_when_both_cases_invalid() -> None:
    """Deterministic edge case: both cases are invalid -- both summaries
    should have no root cause/fix/runbook reference, and each summary's
    identifying fields should remain distinct from the other's."""
    case_a = _fixed_case("A", category="unmatched_category_a")
    case_b = _fixed_case("B", category="unmatched_category_b")

    summary_a = build_case_summary(
        case_a, _fixed_invalid_determination("A", "unmatched_category_a")
    )
    summary_b = build_case_summary(
        case_b, _fixed_invalid_determination("B", "unmatched_category_b")
    )

    for summary in (summary_a, summary_b):
        assert summary.root_cause is None
        assert summary.fix_steps is None
        assert summary.runbook_reference is None

    assert summary_a.case_id != summary_b.case_id
    assert summary_a.customer_name != summary_b.customer_name
    assert summary_a.issue_description != summary_b.issue_description
    for value in _string_fields(summary_a):
        assert "case-B" not in value
    for value in _string_fields(summary_b):
        assert "case-A" not in value
