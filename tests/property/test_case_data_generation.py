"""tests/property/test_case_data_generation.py

Property-based test for scripts/generate_case_data.py's `build_case_record`
(and, by extension, `generate_case_batch`, which is built directly on top
of it) -- design.md Correctness Properties, Property 1.

NOTE: This is a distinct test file from tests/property/test_case_batch_*.py
(task 3.3, Property 2), which covers the count/uniqueness/min-invalid
properties of `generate_case_batch` instead.
"""

from __future__ import annotations

import random
import string
from datetime import datetime

from hypothesis import given, settings
from hypothesis import strategies as st

from scripts.generate_case_data import build_case_record, generate_case_batch

_VALID_STATUSES = {"Open", "In Progress", "Resolved", "Closed"}

# rng seeds -> deterministic random.Random instances.
_seeds = st.integers(min_value=0, max_value=2**32 - 1)

# Non-empty lists of distinct, non-empty runbook issue category names.
_runbook_categories = st.lists(
    st.text(
        alphabet=string.ascii_letters + string.digits + "_ /",
        min_size=1,
        max_size=24,
    ).map(str.strip).filter(lambda s: len(s) > 0),
    min_size=1,
    max_size=8,
    unique=True,
)

# Sets of pre-existing case_id strings (possibly empty) that the generated
# record's case_id must be guaranteed to avoid colliding with.
_existing_case_ids = st.sets(
    st.text(min_size=1, max_size=15),
    max_size=20,
)


def _parse_iso8601(value: str) -> datetime:
    """Parse a machine-parseable ISO-8601 timestamp, tolerating a trailing
    'Z' (UTC designator) that `datetime.fromisoformat` does not accept on
    all supported Python versions.
    """
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    return datetime.fromisoformat(normalized)


# Feature: network-ops-agent, Property 1: Generated case record completeness and consistency
#
# For any random number generator state, validity flag, and non-empty set
# of runbook categories, a case record produced by `build_case_record`
# contains a non-empty value for every required field, its
# `customer_account_id` contains the substring "AnyTel", its `created_at`
# is not later than its `updated_at`, and, when the validity flag is true,
# its `network_issue_category` is a member of the supplied runbook
# category set.
#
# Validates: Requirements 1.4, 1.5, 1.6
@given(
    seed=_seeds,
    valid=st.booleans(),
    runbook_categories=_runbook_categories,
    existing_case_ids=_existing_case_ids,
)
@settings(max_examples=100)
def test_build_case_record_completeness_and_consistency(
    seed: int,
    valid: bool,
    runbook_categories: list[str],
    existing_case_ids: set[str],
) -> None:
    rng = random.Random(seed)

    record = build_case_record(rng, valid, runbook_categories, existing_case_ids)

    # -- All required fields are populated and non-empty. -------------------
    assert record.case_id
    assert isinstance(record.case_id, str)
    assert record.customer_name
    assert record.customer_account_id
    assert record.issue_description
    assert record.network_issue_category
    assert record.service_location_id
    assert record.status
    assert record.created_at
    assert record.updated_at

    # -- Requirement 1.5: customer_account_id identifies AnyTel. ------------
    assert "AnyTel" in record.customer_account_id

    # -- Requirement 1.4: status is one of the four valid enum values. -----
    assert record.status in _VALID_STATUSES

    # -- Requirement 1.4: created_at <= updated_at, both valid ISO-8601. ----
    created_dt = _parse_iso8601(record.created_at)
    updated_dt = _parse_iso8601(record.updated_at)
    assert created_dt <= updated_dt

    # -- Requirement 1.6: valid records use a supplied runbook category. ---
    if valid:
        assert record.network_issue_category in set(runbook_categories)

    # -- case_id uniqueness guarantee against the supplied existing set. ---
    assert record.case_id not in existing_case_ids


# The same completeness/consistency guarantees hold for every record
# produced by `generate_case_batch`, which builds each record via
# `build_case_record` internally.
@given(
    seed=_seeds,
    runbook_categories=_runbook_categories,
    existing_case_ids=_existing_case_ids,
    count=st.integers(min_value=0, max_value=20),
)
@settings(max_examples=100)
def test_generate_case_batch_records_are_complete_and_consistent(
    seed: int,
    runbook_categories: list[str],
    existing_case_ids: set[str],
    count: int,
) -> None:
    rng = random.Random(seed)
    min_invalid = min(2, count)

    records = generate_case_batch(
        rng, runbook_categories, existing_case_ids, count, min_invalid=min_invalid
    )

    assert len(records) == count

    for record in records:
        assert record.case_id
        assert record.customer_name
        assert record.customer_account_id
        assert "AnyTel" in record.customer_account_id
        assert record.issue_description
        assert record.network_issue_category
        assert record.service_location_id
        assert record.status in _VALID_STATUSES
        assert record.created_at
        assert record.updated_at

        created_dt = _parse_iso8601(record.created_at)
        updated_dt = _parse_iso8601(record.updated_at)
        assert created_dt <= updated_dt

        assert record.case_id not in existing_case_ids
