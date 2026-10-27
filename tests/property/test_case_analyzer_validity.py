"""tests/property/test_case_analyzer_validity.py

Property-based test for agent/case_analyzer.py's `determine_validity`
(design.md Correctness Properties, Property 5).

NOTE: This is a distinct test file from
tests/property/test_case_analyzer_rationale.py (task 12.3, Property 6),
which covers `determine_validity`'s `rationale` content instead of its
`valid` boolean. Both tests target the same `determine_validity` function
but live in separate files by design so the two parallel test-writing
tasks never collide on a shared file.
"""

from __future__ import annotations

from hypothesis import example, given, settings
from hypothesis import strategies as st

from agent.case_analyzer import determine_validity
from agent.models import CaseRecord, KBRetrievalItem

_VALID_STATUSES = ["Open", "In Progress", "Resolved", "Closed"]

# Category text used for both the case's network_issue_category and
# KBRetrievalItem.issue_category values, so that overlap is a realistic,
# frequent occurrence rather than a near-impossible coincidence.
_category_text = st.text(min_size=1, max_size=20)


def _case_record_strategy() -> st.SearchStrategy[CaseRecord]:
    """Build arbitrary, structurally valid `CaseRecord`s. Only
    `network_issue_category` is relevant to `determine_validity`; the
    remaining fields are filled with arbitrary-but-valid values."""
    return st.builds(
        CaseRecord,
        case_id=st.text(min_size=1, max_size=12),
        customer_name=st.text(min_size=1, max_size=20),
        customer_account_id=st.text(min_size=1, max_size=20),
        issue_description=st.text(min_size=1, max_size=50),
        network_issue_category=_category_text,
        service_location_id=st.text(min_size=1, max_size=12),
        status=st.sampled_from(_VALID_STATUSES),
        created_at=st.just("2024-01-01T00:00:00Z"),
        updated_at=st.just("2024-01-02T00:00:00Z"),
    )


@st.composite
def _case_and_retrieval_items(draw: st.DrawFn) -> tuple[CaseRecord, list[KBRetrievalItem]]:
    """Draw a `(CaseRecord, list[KBRetrievalItem])` pair whose items'
    `issue_category` values are drawn from a mix of: the case's own
    category (guaranteeing overlap is exercised), `None`, and unrelated
    category text (guaranteeing non-overlap is exercised too)."""
    case = draw(_case_record_strategy())
    category = case.network_issue_category

    item_category_strategy = st.one_of(
        st.none(),
        st.just(category),
        _category_text.filter(lambda c: c != category),
    )

    items = draw(
        st.lists(
            st.builds(
                KBRetrievalItem,
                text=st.text(max_size=40),
                score=st.floats(
                    min_value=0.0,
                    max_value=1.0,
                    allow_nan=False,
                    allow_infinity=False,
                ),
                source_uri=st.text(min_size=1, max_size=20),
                issue_category=item_category_strategy,
            ),
            max_size=6,
        )
    )
    return case, items


def _make_case(category: str) -> CaseRecord:
    return CaseRecord(
        case_id="case-1",
        customer_name="Jamie Rivera",
        customer_account_id="AnyTel-000123",
        issue_description="No internet connectivity since this morning.",
        network_issue_category=category,
        service_location_id="loc-42",
        status="Open",
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-02T00:00:00Z",
    )


def _make_item(issue_category: str | None, score: float = 0.5) -> KBRetrievalItem:
    return KBRetrievalItem(
        text="Some runbook chunk text.",
        score=score,
        source_uri="s3://bucket/runbook.md",
        issue_category=issue_category,
    )


# Feature: network-ops-agent, Property 5: Validity determination follows the category-overlap rule
#
# For any `CaseRecord` and any list of `KBRetrievalItem`s (including an
# empty list and lists where zero, one, or many items have `issue_category`
# equal to the case's `network_issue_category`), `determine_validity`
# returns `valid = True` if and only if at least one item's `issue_category`
# equals the case's `network_issue_category`, and returns exactly one of
# the two values `valid`/`invalid`.
#
# Validates: Requirements 5.2, 5.3, 5.4
@given(case_and_items=_case_and_retrieval_items())
@settings(max_examples=100)
# Deterministic edge cases to guarantee coverage beyond what randomized
# generation alone would reliably hit: empty retrieval list, a single
# non-matching item, a single matching item, and an item whose category is
# None (must not be mistaken for a match, including against an empty-string
# case category).
@example((_make_case("connectivity_outage"), []))
@example((_make_case("connectivity_outage"), [_make_item("mobile_signal")]))
@example((_make_case("connectivity_outage"), [_make_item("connectivity_outage")]))
@example((_make_case("connectivity_outage"), [_make_item(None)]))
@example(
    (
        _make_case("connectivity_outage"),
        [
            _make_item("mobile_signal", score=0.9),
            _make_item("connectivity_outage", score=0.1),
            _make_item(None, score=0.5),
        ],
    )
)
@example((_make_case(""), [_make_item(None)]))
def test_validity_follows_category_overlap_rule(
    case_and_items: tuple[CaseRecord, list[KBRetrievalItem]],
) -> None:
    case, retrieval_items = case_and_items

    determination = determine_validity(case, retrieval_items)

    # Requirement 5.4: valid is expressed as exactly one of two values.
    # In Python, `bool` inherently has exactly two possible values, but we
    # assert the type explicitly rather than assuming it, since the
    # property must hold in fact and not merely by construction.
    assert isinstance(determination.valid, bool)
    assert determination.valid is True or determination.valid is False

    # Requirements 5.2, 5.3: valid iff at least one item's issue_category
    # exactly matches the case's network_issue_category.
    expected_valid = any(
        item.issue_category == case.network_issue_category
        for item in retrieval_items
    )
    assert determination.valid == expected_valid

    if expected_valid:
        # The matched item, when present, must itself be one of the
        # matching items (never a non-matching or unrelated item).
        assert determination.matched_item is not None
        assert (
            determination.matched_item.issue_category
            == case.network_issue_category
        )
        assert determination.matched_item in retrieval_items
    else:
        assert determination.matched_item is None
