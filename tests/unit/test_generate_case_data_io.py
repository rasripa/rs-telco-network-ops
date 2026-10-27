"""Unit tests for scripts/generate_case_data.py's DynamoDB I/O layer and
generation boundary counts.

Validates Requirements 1.2 and 1.8:
  - 1.2: a successful generation run adds between 15 and 20 (inclusive)
    new Case_Records to the Case_Table.
  - 1.8: if the Case_Table already exists, the generator reuses it and
    does not delete or modify any existing item.

This file is distinct from tests/property/test_case_data_generation.py
(Property 1) and tests/property/test_case_batch_*.py (Property 2), which
cover `build_case_record`/`generate_case_batch` from a property-based
angle. This file instead:
  - Mocks `boto3.Session`/`boto3.resource` and
    `agent.config.require_valid_credentials` to exercise
    `ensure_case_table`'s two paths (table-exists vs. table-not-exists)
    without making any real AWS call.
  - Exercises `generate_case_batch` directly (it is pure, so no mocking
    is needed) at the count boundaries of interest: 14, 15, 20, and 21.

Constraint surface note: re-reading `scripts/generate_case_data.py`
confirms that `generate_case_batch` itself does NOT enforce the 15-20
inclusive range from Requirement 1.2 -- its docstring explicitly says
"this function itself accepts any non-negative count so it stays
independently testable across the full input space". The 15-20 range is
a caller-enforced constraint that belongs to the CLI wiring (task 3.6),
which does not exist yet in this file. So the boundary tests below verify
that `generate_case_batch` succeeds (does not raise) and returns exactly
`count` records at each of 14, 15, 20, and 21 -- rather than asserting
that 14/21 are rejected, which is not a constraint this function makes.
"""

from __future__ import annotations

import random
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from scripts.generate_case_data import (
    CASE_TABLE_PARTITION_KEY,
    DEFAULT_CASE_TABLE_NAME,
    ensure_case_table,
    generate_case_batch,
)

_RUNBOOK_CATEGORIES = ["Broadband Outage", "Mobile Signal", "Router Configuration"]


def _resource_not_found_error(operation_name: str = "DescribeTable") -> ClientError:
    """Build the ClientError boto3 raises for `describe_table` when the
    named table does not exist."""
    return ClientError(
        error_response={
            "Error": {
                "Code": "ResourceNotFoundException",
                "Message": "Requested resource not found",
            }
        },
        operation_name=operation_name,
    )


class TestEnsureCaseTableExistingTable:
    """Requirement 1.8: if the Case_Table already exists, reuse it without
    ever calling `create_table` or waiting for table creation."""

    @patch("scripts.generate_case_data.require_valid_credentials")
    @patch("scripts.generate_case_data.boto3.Session")
    def test_reuses_existing_table_without_create_or_wait(
        self, mock_session_cls: MagicMock, mock_require_valid_credentials: MagicMock
    ) -> None:
        mock_require_valid_credentials.return_value = ("rs-gen-ai", "us-west-2")

        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session
        mock_resource = MagicMock()
        mock_session.resource.return_value = mock_resource
        mock_client = MagicMock()
        mock_resource.meta.client = mock_client

        # describe_table succeeds: the table already exists.
        mock_client.describe_table.return_value = {
            "Table": {"TableName": DEFAULT_CASE_TABLE_NAME, "TableStatus": "ACTIVE"}
        }
        mock_table = MagicMock()
        mock_resource.Table.return_value = mock_table

        result = ensure_case_table(profile="rs-gen-ai", region="us-west-2")

        # The existing table is returned as-is.
        assert result is mock_table
        mock_resource.Table.assert_called_once_with(DEFAULT_CASE_TABLE_NAME)

        # No modification of an existing table: create_table and the
        # table_exists waiter must never be invoked.
        mock_client.create_table.assert_not_called()
        mock_client.get_waiter.assert_not_called()

    @patch("scripts.generate_case_data.require_valid_credentials")
    @patch("scripts.generate_case_data.boto3.Session")
    def test_credential_check_runs_before_describe_table(
        self, mock_session_cls: MagicMock, mock_require_valid_credentials: MagicMock
    ) -> None:
        """The fail-closed credential check must be performed before any
        DynamoDB call is made."""
        mock_require_valid_credentials.return_value = ("rs-gen-ai", "us-west-2")

        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session
        mock_resource = MagicMock()
        mock_session.resource.return_value = mock_resource
        mock_client = MagicMock()
        mock_resource.meta.client = mock_client
        mock_client.describe_table.return_value = {"Table": {}}

        ensure_case_table(profile="rs-gen-ai", region="us-west-2")

        mock_require_valid_credentials.assert_called_once_with("rs-gen-ai", "us-west-2")


class TestEnsureCaseTableNewTable:
    """Requirement 1.1/1.8: if the Case_Table does not exist, create it
    with `case_id` as the partition key and wait for it to become
    ACTIVE."""

    @patch("scripts.generate_case_data.require_valid_credentials")
    @patch("scripts.generate_case_data.boto3.Session")
    def test_creates_table_with_correct_schema_when_missing(
        self, mock_session_cls: MagicMock, mock_require_valid_credentials: MagicMock
    ) -> None:
        mock_require_valid_credentials.return_value = ("rs-gen-ai", "us-west-2")

        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session
        mock_resource = MagicMock()
        mock_session.resource.return_value = mock_resource
        mock_client = MagicMock()
        mock_resource.meta.client = mock_client

        # describe_table raises ResourceNotFoundException: the table does
        # not exist yet.
        mock_client.describe_table.side_effect = _resource_not_found_error()

        mock_waiter = MagicMock()
        mock_client.get_waiter.return_value = mock_waiter

        mock_table = MagicMock()
        mock_resource.Table.return_value = mock_table

        result = ensure_case_table(profile="rs-gen-ai", region="us-west-2")

        assert result is mock_table

        # create_table IS called with the correct KeySchema,
        # AttributeDefinitions, and BillingMode.
        mock_client.create_table.assert_called_once()
        _, create_kwargs = mock_client.create_table.call_args
        assert create_kwargs["TableName"] == DEFAULT_CASE_TABLE_NAME
        assert create_kwargs["KeySchema"] == [
            {"AttributeName": CASE_TABLE_PARTITION_KEY, "KeyType": "HASH"}
        ]
        assert create_kwargs["AttributeDefinitions"] == [
            {"AttributeName": CASE_TABLE_PARTITION_KEY, "AttributeType": "S"}
        ]
        assert create_kwargs["BillingMode"] == "PAY_PER_REQUEST"

        # The table_exists waiter is invoked to wait for ACTIVE status.
        mock_client.get_waiter.assert_called_once_with("table_exists")
        mock_waiter.wait.assert_called_once()
        _, wait_kwargs = mock_waiter.wait.call_args
        assert wait_kwargs["TableName"] == DEFAULT_CASE_TABLE_NAME

    @patch("scripts.generate_case_data.require_valid_credentials")
    @patch("scripts.generate_case_data.boto3.Session")
    def test_custom_table_name_is_used_for_create_and_waiter(
        self, mock_session_cls: MagicMock, mock_require_valid_credentials: MagicMock
    ) -> None:
        mock_require_valid_credentials.return_value = ("rs-gen-ai", "us-west-2")

        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session
        mock_resource = MagicMock()
        mock_session.resource.return_value = mock_resource
        mock_client = MagicMock()
        mock_resource.meta.client = mock_client
        mock_client.describe_table.side_effect = _resource_not_found_error()
        mock_waiter = MagicMock()
        mock_client.get_waiter.return_value = mock_waiter
        mock_resource.Table.return_value = MagicMock()

        ensure_case_table(
            profile="rs-gen-ai", region="us-west-2", table_name="Custom_Case_Table"
        )

        _, create_kwargs = mock_client.create_table.call_args
        assert create_kwargs["TableName"] == "Custom_Case_Table"
        _, wait_kwargs = mock_waiter.wait.call_args
        assert wait_kwargs["TableName"] == "Custom_Case_Table"

    @patch("scripts.generate_case_data.require_valid_credentials")
    @patch("scripts.generate_case_data.boto3.Session")
    def test_non_resource_not_found_client_error_is_propagated(
        self, mock_session_cls: MagicMock, mock_require_valid_credentials: MagicMock
    ) -> None:
        """A DescribeTable failure for a reason other than "table not
        found" must not be swallowed or misinterpreted as "create the
        table"."""
        mock_require_valid_credentials.return_value = ("rs-gen-ai", "us-west-2")

        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session
        mock_resource = MagicMock()
        mock_session.resource.return_value = mock_resource
        mock_client = MagicMock()
        mock_resource.meta.client = mock_client
        mock_client.describe_table.side_effect = ClientError(
            error_response={
                "Error": {
                    "Code": "AccessDeniedException",
                    "Message": "not authorized",
                }
            },
            operation_name="DescribeTable",
        )

        with pytest.raises(ClientError) as exc_info:
            ensure_case_table(profile="rs-gen-ai", region="us-west-2")

        assert exc_info.value.response["Error"]["Code"] == "AccessDeniedException"
        mock_client.create_table.assert_not_called()


class TestGenerateCaseBatchBoundaryCounts:
    """Boundary counts for the 15-20 inclusive range from Requirement 1.2.

    `generate_case_batch` is a pure function (no I/O), so no mocking is
    required here. As documented in its docstring, the function itself
    accepts any non-negative `count` -- the 15-20 inclusive range is a
    caller-enforced constraint applied by the (not-yet-implemented) CLI
    wiring in task 3.6, not by this function. Accordingly, these tests
    confirm that counts just outside the range (14, 21) behave
    identically, at the pure-function level, to counts at the range's
    edges (15, 20): all four succeed and return exactly `count` records.
    """

    @pytest.mark.parametrize("count", [14, 15, 20, 21])
    def test_boundary_counts_succeed_with_exact_length(self, count: int) -> None:
        rng = random.Random(42)

        records = generate_case_batch(
            rng, _RUNBOOK_CATEGORIES, set(), count, min_invalid=2
        )

        assert len(records) == count
        # case_id values are pairwise distinct at every boundary count.
        assert len({record.case_id for record in records}) == count

    @pytest.mark.parametrize("count", [14, 15, 20, 21])
    def test_boundary_counts_respect_min_invalid(self, count: int) -> None:
        """At every boundary count, at least `min_invalid` records must
        have a category outside the supplied runbook categories
        (Requirement 1.7)."""
        rng = random.Random(7)
        runbook_category_set = set(_RUNBOOK_CATEGORIES)

        records = generate_case_batch(
            rng, _RUNBOOK_CATEGORIES, set(), count, min_invalid=2
        )

        invalid_count = sum(
            1
            for record in records
            if record.network_issue_category not in runbook_category_set
        )
        assert invalid_count >= 2

    def test_count_just_below_minimum_still_succeeds(self) -> None:
        """count=14 (one below the Requirement 1.2 minimum of 15) is not
        rejected by generate_case_batch itself -- confirming the range
        is enforced by the caller, not this function."""
        rng = random.Random(1)

        records = generate_case_batch(
            rng, _RUNBOOK_CATEGORIES, set(), 14, min_invalid=2
        )

        assert len(records) == 14

    def test_count_just_above_maximum_still_succeeds(self) -> None:
        """count=21 (one above the Requirement 1.2 maximum of 20) is not
        rejected by generate_case_batch itself -- confirming the range
        is enforced by the caller, not this function."""
        rng = random.Random(2)

        records = generate_case_batch(
            rng, _RUNBOOK_CATEGORIES, set(), 21, min_invalid=2
        )

        assert len(records) == 21
