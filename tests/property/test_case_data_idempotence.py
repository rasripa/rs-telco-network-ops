"""tests/property/test_case_data_idempotence.py

Property-based test for scripts/generate_case_data.py's `generate_case_batch`
(design.md Correctness Properties, Property 2: Idempotent-safe case
generation).

This is a dedicated test file (distinct from tests/property/test_case_data_*
files written by parallel task 3.2) so that concurrent property-test
subtasks targeting the same module never collide on a shared file.
"""

from __future__ import annotations

import random

from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

from scripts.generate_case_data import generate_case_batch

# --- strategies --------------------------------------------------------------

# Any non-empty, printable category label. Runbook categories are always a
# non-empty set per generate_case_batch's contract.
_category_strategy = st.text(
    alphabet=st.characters(min_codepoint=33, max_codepoint=126),
    min_size=1,
    max_size=12,
)

_runbook_categories_strategy = st.lists(
    _category_strategy, min_size=1, max_size=5, unique=True
)

# Pre-existing case_id values already present in the Case_Table, simulating
# a table that already has data from a prior generation run. Includes the
# empty set (first-ever run) as well as non-empty sets (a second/subsequent
# run against a table that already has data).
_existing_case_ids_strategy = st.sets(
    st.text(min_size=1, max_size=10), max_size=10
)


@st.composite
def _count_and_min_invalid(draw: st.DrawFn) -> tuple[int, int]:
    """Draw a valid (count, min_invalid) pair.

    Biased so that a substantial share of examples fall within the
    Requirement 1.2-mandated 15-20 inclusive batch-size range, while still
    covering edge cases (0, exactly min_invalid == count, and counts
    outside 15-20) across the rest of the input space.
    """
    count = draw(
        st.one_of(
            st.integers(min_value=15, max_value=20),  # Requirement 1.2 range
            st.integers(min_value=0, max_value=30),  # general/edge coverage
        )
    )
    min_invalid = draw(st.integers(min_value=0, max_value=count))
    return count, min_invalid


# --- test ---------------------------------------------------------------------


# Feature: network-ops-agent, Property 2: Idempotent-safe case generation
#
# For any pre-existing set of case ids (including the empty set) and any
# non-empty set of runbook categories, `generate_case_batch` returns between
# 15 and 20 records whose `case_id` values are pairwise distinct and
# disjoint from the pre-existing set, and includes at least 2 records whose
# `network_issue_category` is outside the runbook category set.
#
# Validates: Requirements 1.2, 1.3, 1.7, 1.8
@given(
    rng=st.randoms(use_true_random=False),
    runbook_categories=_runbook_categories_strategy,
    existing_case_ids=_existing_case_ids_strategy,
    count_and_min_invalid=_count_and_min_invalid(),
)
# Explicit boundary examples from Requirement 1.2/1.7: exactly 15 records
# with exactly 2 invalid, exactly 20 records with exactly 2 invalid, and a
# zero-count edge case against a non-empty pre-existing id set (simulating
# a second run against a table that already has data).
@example(
    rng=random.Random(1),
    runbook_categories=["broadband_outage"],
    existing_case_ids={"CASE-EXISTING1"},
    count_and_min_invalid=(15, 2),
)
@example(
    rng=random.Random(2),
    runbook_categories=["broadband_outage", "mobile_signal"],
    existing_case_ids=set(),
    count_and_min_invalid=(20, 2),
)
@example(
    rng=random.Random(3),
    runbook_categories=["broadband_outage"],
    existing_case_ids={"CASE-A", "CASE-B", "CASE-C"},
    count_and_min_invalid=(0, 0),
)
@settings(max_examples=150, suppress_health_check=[HealthCheck.large_base_example])
def test_generate_case_batch_is_idempotent_safe(
    rng: random.Random,
    runbook_categories: list[str],
    existing_case_ids: set[str],
    count_and_min_invalid: tuple[int, int],
) -> None:
    count, min_invalid = count_and_min_invalid

    # `existing_case_ids` must not be mutated by the call (it represents the
    # caller's view of the table's current contents before this run).
    existing_case_ids_snapshot = set(existing_case_ids)

    batch = generate_case_batch(
        rng=rng,
        runbook_categories=runbook_categories,
        existing_case_ids=existing_case_ids,
        count=count,
        min_invalid=min_invalid,
    )

    # The requested number of records is produced.
    assert len(batch) == count

    generated_ids = [record.case_id for record in batch]

    # All generated case_ids within the batch are pairwise unique.
    assert len(set(generated_ids)) == len(generated_ids)

    # No generated case_id collides with a pre-existing id -- this is the
    # idempotent-safe re-run guarantee: running the generator again against
    # a table that already has data must never produce a colliding id.
    assert set(generated_ids).isdisjoint(existing_case_ids_snapshot)

    # The caller-supplied set was not mutated.
    assert existing_case_ids == existing_case_ids_snapshot

    # At least `min_invalid` records have a network_issue_category outside
    # the supplied runbook category set (Requirement 1.7's floor of >= 2 is
    # exercised directly by the boundary @example cases above, which pin
    # min_invalid=2 for count in {15, 20}).
    category_set = set(runbook_categories)
    invalid_count = sum(
        1 for record in batch if record.network_issue_category not in category_set
    )
    assert invalid_count >= min_invalid
