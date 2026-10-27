"""tests/property/test_case_analyzer_rationale.py

Property-based test for agent/case_analyzer.py's `determine_validity`
rationale content -- design.md Correctness Properties, Property 6.

NOTE: This is a distinct test file from the parallel task's property test
for `determine_validity`'s category-overlap outcome (task 12.2, Property
5, e.g. tests/property/test_case_analyzer_validity.py). This file targets
only the *rationale text* produced by `determine_validity`, not its
`valid` boolean per se (though the two are cross-checked here).
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from agent.case_analyzer import determine_validity
from agent.models import CaseRecord, KBRetrievalItem

# Non-empty category-like strings used for both a case's
# `network_issue_category` and a retrieval item's `issue_category`.
#
# Categories containing the words "match"/"valid" (case-insensitive) are
# excluded: those words are also part of the rationale's own fixed
# phrasing ("matched", "did not match", "valid"/"invalid"), so allowing a
# category value to *itself* contain them would make the phrase-substring
# assertions below ambiguous about whether a hit came from the fixed
# wording or merely from the embedded category value. This keeps the test
# focused on the property's intent (does the rationale reference the
# category value, and does it correctly state the match outcome) rather
# than on this unrelated string-collision edge case.
_categories = st.text(min_size=1, max_size=20).filter(
    lambda s: s.strip() != ""
    and "match" not in s.lower()
    and "valid" not in s.lower()
)

# `source_uri` is embedded verbatim into the rationale text for a valid
# determination ("...matched ... (source: '{source_uri}'); ..."), so, for
# the same reason as `_categories` above, it is constrained to avoid the
# words the rationale's fixed phrasing itself uses ("match"/"valid"),
# preventing an incidental substring collision from making the phrase
# assertions ambiguous.
_source_uris = st.text(min_size=0, max_size=30).filter(
    lambda s: "match" not in s.lower() and "valid" not in s.lower()
)
_texts = st.text(min_size=0, max_size=50)
_scores = st.floats(
    min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False
)


def _case_records(category_strategy=_categories) -> st.SearchStrategy[CaseRecord]:
    """Build a minimal CaseRecord with only network_issue_category varied
    meaningfully; the other fields are fixed placeholders since
    `determine_validity` does not consult them."""
    return category_strategy.map(
        lambda category: CaseRecord(
            case_id="case-1",
            customer_name="Jane Doe",
            customer_account_id="AnyTel-000001",
            issue_description="Some issue description",
            network_issue_category=category,
            service_location_id="LOC-1",
            status="Open",
            created_at="2024-01-01T00:00:00Z",
            updated_at="2024-01-01T00:00:00Z",
        )
    )


def _kb_retrieval_items(
    issue_category_strategy: st.SearchStrategy[str | None],
) -> st.SearchStrategy[KBRetrievalItem]:
    return st.builds(
        KBRetrievalItem,
        text=_texts,
        score=_scores,
        source_uri=_source_uris,
        issue_category=issue_category_strategy,
    )


# A retrieval item's issue_category is either drawn from the same category
# strategy used for cases (so overlap is possible/likely), some unrelated
# text (so a non-overlapping category is possible), or None (unlabeled
# content, which can never match any case category).
_retrieval_item_categories = st.one_of(
    _categories,
    st.text(min_size=0, max_size=20),
    st.none(),
)

_retrieval_items_lists = st.lists(
    _kb_retrieval_items(_retrieval_item_categories),
    min_size=0,
    max_size=8,
)


# Feature: network-ops-agent, Property 6: Validity rationale accuracy
#
# For any CaseRecord and any list of KBRetrievalItems, the rationale
# produced by determine_validity contains the case's network_issue_category
# and states a match was found if and only if determine_validity's valid
# result is true for that same input.
#
# Validates: Requirements 5.5
@given(
    case=_case_records(),
    retrieval_items=_retrieval_items_lists,
)
@settings(max_examples=100)
def test_rationale_references_category_and_matches_valid_outcome(
    case: CaseRecord,
    retrieval_items: list[KBRetrievalItem],
) -> None:
    determination = determine_validity(case, retrieval_items)
    rationale = determination.rationale

    # The rationale always references the case's network_issue_category
    # by its literal string value, regardless of the outcome.
    assert case.network_issue_category in rationale

    # The rationale's stated match outcome is consistent with `valid`.
    # Phrasing is checked via a robust substring rather than exact string
    # equality, so the test tolerates minor wording changes to the
    # implementation while still pinning down the semantic content.
    #
    # NOTE: "matched"/"did not match" alone are unsafe discriminators here
    # because the current implementation's *invalid* rationale text itself
    # contains the substring "matched" (as part of "no runbook category
    # matched, so the case is determined to be invalid"). "valid"/"invalid"
    # are similarly unsafe in isolation because "invalid" contains "valid"
    # as a substring. The phrases below sidestep both traps: "to be valid"
    # is never a substring of "...to be invalid" (an "in" sits between
    # "be " and "valid" in the invalid phrasing), so exactly one of these
    # two phrases is present for any rationale text, independent of
    # whatever arbitrary category/source_uri text is embedded in it.
    stated_valid = "determined to be valid" in rationale
    stated_invalid = "determined to be invalid" in rationale

    # The rationale must state one outcome or the other (never neither,
    # never a contradictory mix of both).
    assert stated_valid or stated_invalid
    assert not (stated_valid and stated_invalid)

    if determination.valid:
        assert stated_valid
        assert not stated_invalid
    else:
        assert stated_invalid
        assert not stated_valid


# Focused sub-cases mirroring the property above, using explicit
# overlapping / non-overlapping / empty inputs, to make the property's
# intent easy to verify at a glance alongside the generated-input coverage.
@given(category=_categories, other_text=_texts, score=_scores)
@settings(max_examples=100)
def test_rationale_states_match_found_when_category_overlaps(
    category: str, other_text: str, score: float
) -> None:
    case = CaseRecord(
        case_id="case-2",
        customer_name="John Smith",
        customer_account_id="AnyTel-000002",
        issue_description="Another issue",
        network_issue_category=category,
        service_location_id="LOC-2",
        status="Open",
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-01T00:00:00Z",
    )
    matching_item = KBRetrievalItem(
        text=other_text,
        score=score,
        source_uri="runbooks/example.md",
        issue_category=category,
    )

    determination = determine_validity(case, [matching_item])

    assert determination.valid is True
    assert category in determination.rationale
    assert "matched" in determination.rationale
    assert "did not match" not in determination.rationale


@given(category=_categories, unrelated_category=_categories)
@settings(max_examples=100)
def test_rationale_states_no_match_found_when_no_category_overlaps(
    category: str, unrelated_category: str
) -> None:
    non_matching_item = KBRetrievalItem(
        text="unrelated runbook content",
        score=0.5,
        source_uri="runbooks/other.md",
        # Force a guaranteed non-match regardless of the (unlikely) case
        # where the two generated category strings happen to coincide.
        issue_category=unrelated_category + "-does-not-match",
    )
    case = CaseRecord(
        case_id="case-3",
        customer_name="Alex Lee",
        customer_account_id="AnyTel-000003",
        issue_description="Yet another issue",
        network_issue_category=category,
        service_location_id="LOC-3",
        status="Open",
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-01T00:00:00Z",
    )

    determination = determine_validity(case, [non_matching_item])

    assert determination.valid is False
    assert category in determination.rationale
    assert "did not match" in determination.rationale


@given(category=_categories)
@settings(max_examples=100)
def test_rationale_states_no_match_found_for_empty_retrieval_list(
    category: str,
) -> None:
    case = CaseRecord(
        case_id="case-4",
        customer_name="Sam Rivera",
        customer_account_id="AnyTel-000004",
        issue_description="Empty retrieval issue",
        network_issue_category=category,
        service_location_id="LOC-4",
        status="Open",
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-01T00:00:00Z",
    )

    determination = determine_validity(case, [])

    assert determination.valid is False
    assert category in determination.rationale
    assert "did not match" in determination.rationale
