"""scripts/generate_case_data.py: Case_Data_Generator.

Contains both:
  - The pure, side-effect-free generation logic for synthetic AnyTel
    support Case_Records (Requirement 1): `build_case_record` and
    `generate_case_batch`. These perform no network or DynamoDB I/O.
  - A thin DynamoDB I/O layer built on top of those pure functions
    (task 3.4, Requirements 1.1, 1.3, 1.8, 11.1, 11.2): `ensure_case_table`
    (create-or-reuse the `Case_Table`), `scan_existing_case_ids` (read back
    real table contents to guarantee `case_id` uniqueness against more than
    just the current batch), and `write_case_records` (a batched write
    wrapper).
  - A runnable CLI entrypoint (task 3.6, Requirements 1.1, 1.2, 11.3,
    11.4, 11.5, 11.6): `main()`, which accepts optional `--profile`/
    `--region` arguments and ties `ensure_case_table`,
    `scan_existing_case_ids`, `generate_case_batch`, and
    `write_case_records` together end-to-end against the real Case_Table.

`build_case_record` and `generate_case_batch` both take a caller-supplied
`random.Random` instance (`rng`) so that, given the same seed and the same
other arguments, generation is reproducible and independently testable
(e.g. with Hypothesis). The only exception is the wall-clock anchor used to
derive `created_at`/`updated_at` (see `_build_timestamps`), which keeps
generated timestamps realistic (recent) without affecting the properties
this module is required to satisfy: `created_at` is always <= `updated_at`,
and both are emitted in a consistent, machine-parseable ISO-8601 format.
"""

from __future__ import annotations

import argparse
import random
import string
from datetime import datetime, timedelta, timezone
from typing import Any

if __package__ in (None, ""):
    # Allow `python scripts/generate_case_data.py` (direct script
    # execution) in addition to `python -m scripts.generate_case_data`
    # (module execution). When run directly, Python puts this file's own
    # directory (scripts/) on sys.path[0] rather than the project root,
    # so the absolute `agent.*`/`scripts.*` imports below would otherwise
    # fail with ModuleNotFoundError. This bootstrap inserts the project
    # root (this file's parent's parent) onto sys.path before those
    # imports are attempted; it is a no-op when this module is imported
    # normally as part of the `scripts` package.
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import boto3
from botocore.exceptions import ClientError

from agent.config import require_valid_credentials
from agent.models import CaseRecord
from scripts.generate_runbooks import RUNBOOK_CATEGORIES

# --- case_id generation -----------------------------------------------------

_CASE_ID_ALPHABET = string.ascii_uppercase + string.digits
_CASE_ID_RANDOM_SUFFIX_LENGTH = 8
_MAX_CASE_ID_ATTEMPTS = 10_000

# --- case status values (Requirement 1.4) -----------------------------------

_STATUSES: tuple[str, ...] = ("Open", "In Progress", "Resolved", "Closed")

# --- synthetic customer identity pools ---------------------------------------

_FIRST_NAMES: tuple[str, ...] = (
    "Ava",
    "Liam",
    "Sofia",
    "Noah",
    "Maya",
    "Ethan",
    "Zoe",
    "Lucas",
    "Chloe",
    "Mason",
    "Ella",
    "Jacob",
    "Grace",
    "Oliver",
    "Nora",
    "Elijah",
)

_LAST_NAMES: tuple[str, ...] = (
    "Nguyen",
    "Patel",
    "Garcia",
    "Smith",
    "Johnson",
    "Kim",
    "Rossi",
    "Müller",
    "Andersson",
    "Okafor",
    "Silva",
    "Dubois",
    "Kowalski",
    "Tanaka",
    "Hassan",
    "Ivanov",
)

# --- valid (runbook-addressable) issue descriptions (Requirement 1.6) -------

_VALID_ISSUE_DETAILS: tuple[str, ...] = (
    "intermittent connectivity and degraded service quality reported over the past few days",
    "complete loss of service since this morning",
    "slow performance during peak evening hours",
    "repeated disconnects several times per hour",
    "no connection at all after a recent technician site visit",
    "signal drops that started right after last night's outage window",
)


def _build_valid_issue_description(rng: random.Random, category: str, location: str) -> str:
    """Build an issue description consistent with a runbook issue category."""
    detail = rng.choice(_VALID_ISSUE_DETAILS)
    return (
        f"Customer at service location {location} is reporting a {category} "
        f"issue with their AnyTel service: {detail}."
    )


# --- invalid (non-runbook-addressable) issue profiles (Requirement 1.7) ----

_INVALID_ISSUE_PROFILES: tuple[tuple[str, str], ...] = (
    (
        "Billing Inquiry",
        "Customer is disputing a duplicate charge on their most recent AnyTel invoice.",
    ),
    (
        "Account Management",
        "Customer wants to add an authorized user to their AnyTel account.",
    ),
    (
        "Loyalty Program",
        "Customer is asking how to redeem AnyTel loyalty reward points.",
    ),
    (
        "General Inquiry",
        "Customer is asking about AnyTel's current promotional plan offers.",
    ),
    (
        "Device Upgrade",
        "Customer wants to know their eligibility date for an AnyTel device upgrade.",
    ),
)


def _build_invalid_profile(
    rng: random.Random, runbook_categories: list[str]
) -> tuple[str, str]:
    """Pick a (category, issue_description) pair that does not correspond to
    any Runbook_Corpus issue category, so it cannot produce a Knowledge_Base
    match (Requirement 1.7).
    """
    runbook_category_set = set(runbook_categories)
    candidates = [
        profile for profile in _INVALID_ISSUE_PROFILES if profile[0] not in runbook_category_set
    ]
    if not candidates:
        # Defensive fallback: every predefined invalid category happened to
        # collide with a supplied runbook category. Disambiguate below.
        candidates = list(_INVALID_ISSUE_PROFILES)

    category, description = rng.choice(candidates)
    while category in runbook_category_set:
        category = f"{category} (Unclassified)"
    return category, description


# --- case_id / timestamp helpers --------------------------------------------


def _generate_unique_case_id(rng: random.Random, existing_case_ids: set[str]) -> str:
    """Generate a `case_id` that does not collide with `existing_case_ids`."""
    for _ in range(_MAX_CASE_ID_ATTEMPTS):
        candidate = "CASE-" + "".join(
            rng.choices(_CASE_ID_ALPHABET, k=_CASE_ID_RANDOM_SUFFIX_LENGTH)
        )
        if candidate not in existing_case_ids:
            return candidate
    raise RuntimeError(
        "failed to generate a unique case_id after "
        f"{_MAX_CASE_ID_ATTEMPTS} attempts; existing_case_ids may be too large"
    )


def _isoformat(value: datetime) -> str:
    """Render a UTC datetime as a consistent, machine-parseable ISO-8601
    string (Requirement 1.4).
    """
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _build_timestamps(rng: random.Random) -> tuple[str, str]:
    """Build a (created_at, updated_at) pair where created_at <= updated_at
    (Requirement 1.4), anchored to the current wall-clock time so generated
    cases look recent.
    """
    now = datetime.now(timezone.utc)
    created_offset_minutes = rng.randint(30, 30 * 24 * 60)  # 30 min .. 30 days ago
    created_at = now - timedelta(minutes=created_offset_minutes)
    update_delay_minutes = rng.randint(0, created_offset_minutes)
    updated_at = created_at + timedelta(minutes=update_delay_minutes)
    return _isoformat(created_at), _isoformat(updated_at)


# --- public pure functions ---------------------------------------------------


def build_case_record(
    rng: random.Random,
    valid: bool,
    runbook_categories: list[str],
    existing_case_ids: set[str],
) -> CaseRecord:
    """Build a single synthetic Case_Record.

    Pure and side-effect free: performs no DynamoDB or other network I/O.

    Args:
        rng: A `random.Random` instance supplied by the caller so that
            generation is deterministic/reproducible given the same seed
            and inputs.
        valid: When True, the record's `network_issue_category` is drawn
            from `runbook_categories` (and the description is consistent
            with it), so a downstream Knowledge_Base match is plausible.
            When False, the category and description are drawn from a
            pool guaranteed not to correspond to any `runbook_categories`
            entry, so no Knowledge_Base match should occur.
        runbook_categories: The current set of Runbook_Corpus issue
            categories. Must be non-empty.
        existing_case_ids: `case_id` values already present (in the
            Case_Table and/or already generated earlier in this batch).
            The returned record's `case_id` is guaranteed not to be a
            member of this set.

    Returns:
        A fully populated `CaseRecord`.

    Raises:
        ValueError: if `runbook_categories` is empty.
    """
    if not runbook_categories:
        raise ValueError("runbook_categories must not be empty")

    case_id = _generate_unique_case_id(rng, existing_case_ids)
    customer_name = f"{rng.choice(_FIRST_NAMES)} {rng.choice(_LAST_NAMES)}"
    customer_account_id = f"AnyTel-{rng.randint(1, 999_999):06d}"
    service_location_id = f"LOC-{rng.randint(1000, 9999)}"
    status = rng.choice(_STATUSES)
    created_at, updated_at = _build_timestamps(rng)

    if valid:
        category = rng.choice(list(runbook_categories))
        issue_description = _build_valid_issue_description(rng, category, service_location_id)
    else:
        category, issue_description = _build_invalid_profile(rng, runbook_categories)

    return CaseRecord(
        case_id=case_id,
        customer_name=customer_name,
        customer_account_id=customer_account_id,
        issue_description=issue_description,
        network_issue_category=category,
        service_location_id=service_location_id,
        status=status,
        created_at=created_at,
        updated_at=updated_at,
    )


def generate_case_batch(
    rng: random.Random,
    runbook_categories: list[str],
    existing_case_ids: set[str],
    count: int,
    min_invalid: int = 2,
) -> list[CaseRecord]:
    """Generate a batch of `count` synthetic Case_Records.

    Pure and side-effect free: performs no DynamoDB or other network I/O.
    The DynamoDB table creation/write wrapper is a separate, thin I/O layer
    built on top of this function (task 3.4).

    Args:
        rng: A `random.Random` instance supplied by the caller.
        runbook_categories: The current set of Runbook_Corpus issue
            categories. Must be non-empty.
        existing_case_ids: `case_id` values already present in the
            Case_Table (e.g. from prior generation runs). Not mutated.
        count: Number of records to generate (Requirement 1.2 expects a
            caller-enforced range of 15-20 inclusive; this function itself
            accepts any non-negative count so it stays independently
            testable across the full input space).
        min_invalid: Minimum number of generated records whose
            `network_issue_category` falls outside `runbook_categories`
            (Requirement 1.7). Defaults to 2.

    Returns:
        A list of `count` `CaseRecord`s whose `case_id` values are pairwise
        distinct and disjoint from `existing_case_ids`, including at least
        `min_invalid` records that are intentionally invalid.

    Raises:
        ValueError: if `count` or `min_invalid` is negative, if
            `min_invalid` exceeds `count`, or if `runbook_categories` is
            empty.
    """
    if not runbook_categories:
        raise ValueError("runbook_categories must not be empty")
    if count < 0:
        raise ValueError("count must be non-negative")
    if min_invalid < 0:
        raise ValueError("min_invalid must be non-negative")
    if min_invalid > count:
        raise ValueError("min_invalid cannot exceed count")

    validity_flags: list[bool] = [False] * min_invalid + [True] * (count - min_invalid)
    rng.shuffle(validity_flags)

    used_case_ids: set[str] = set(existing_case_ids)
    records: list[CaseRecord] = []
    for valid in validity_flags:
        record = build_case_record(rng, valid, runbook_categories, used_case_ids)
        used_case_ids.add(record.case_id)
        records.append(record)
    return records


# --- DynamoDB I/O layer (task 3.4) ------------------------------------------
#
# The functions below are a thin I/O layer built on top of the pure
# `build_case_record`/`generate_case_batch` functions above. They are
# intentionally kept separate from that pure logic: everything above this
# point performs no network I/O and is exercised directly by property
# tests (Properties 1 and 2); everything below performs real AWS calls and
# is exercised by mocked unit/integration tests instead (task 3.5).
#
# Requirements covered:
#   1.1 - Case_Table created with case_id as partition key.
#   1.3 - case_id uniqueness against real table contents (not just the
#         current batch) via `scan_existing_case_ids`.
#   1.8 - Reuse an existing Case_Table without deleting/modifying any
#         existing item.
#   11.1/11.2 - Provisioned in us-west-2 using the rs-gen-ai profile (via
#         `agent.config.resolve_profile`/`resolve_region`), with the
#         fail-closed credential check performed before table creation.

DEFAULT_CASE_TABLE_NAME = "Case_Table"
CASE_TABLE_PARTITION_KEY = "case_id"

# How long `ensure_case_table` will wait for a newly-created table to
# reach ACTIVE before giving up.
_TABLE_ACTIVE_WAITER_DELAY_SECONDS = 2
_TABLE_ACTIVE_WAITER_MAX_ATTEMPTS = 25


def _dynamodb_resource(profile: str | None, region: str | None) -> Any:
    """Build a `dynamodb` boto3 resource for the given profile/region.

    A `None` profile falls back to boto3's default credential chain (e.g.
    the AgentCore Runtime execution role when deployed); a `None` region
    falls back to boto3's default region resolution.
    """
    session = boto3.Session(profile_name=profile, region_name=region)
    return session.resource("dynamodb", region_name=region)


def ensure_case_table(
    profile: str | None = None,
    region: str | None = None,
    table_name: str = DEFAULT_CASE_TABLE_NAME,
) -> Any:
    """Create the `Case_Table` if it does not exist; reuse it otherwise.

    This is a resource-creating operation (Requirement 11.5), so the
    fail-closed credential check (`agent.config.require_valid_credentials`)
    is performed first: if the resolved profile does not resolve to valid
    AWS credentials, this function raises
    `agent.config.CredentialResolutionError` and no table is created.

    Idempotent reuse (Requirement 1.8): if a table named `table_name`
    already exists, it is returned as-is. No existing item is read,
    deleted, or modified by this function -- it only checks the table's
    existence via `describe_table`.

    When the table does not exist, it is created with `case_id` (String)
    as the sole partition key, using on-demand (`PAY_PER_REQUEST`) billing
    so no throughput needs to be provisioned up front. This function then
    waits for the table to reach `ACTIVE` before returning, using boto3's
    built-in `table_exists` waiter.

    Args:
        profile: Explicit AWS named profile. Resolved against the
            `rs-gen-ai` default via `agent.config.resolve_profile` when
            omitted.
        region: Explicit AWS Region. Resolved against the `us-west-2`
            default via `agent.config.resolve_region` when omitted.
        table_name: The DynamoDB table name. Defaults to `Case_Table`.

    Returns:
        The `boto3` DynamoDB `Table` resource for `table_name`, ready for
        use (guaranteed to be in the `ACTIVE` state).

    Raises:
        agent.config.CredentialResolutionError: if the resolved profile
            does not resolve to valid AWS credentials. Raised before any
            table-creating call is made.
        botocore.exceptions.ClientError: propagated for any DynamoDB
            failure other than "table not found" while checking for the
            table's existence, or for any failure while creating it.
    """
    resolved_profile, resolved_region = require_valid_credentials(profile, region)

    resource = _dynamodb_resource(resolved_profile, resolved_region)
    client = resource.meta.client

    try:
        client.describe_table(TableName=table_name)
        # Table already exists: reuse it as-is. No item is read, deleted,
        # or modified here (Requirement 1.8).
        return resource.Table(table_name)
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "")
        if error_code != "ResourceNotFoundException":
            raise

    # Table does not exist: create it with case_id as the partition key.
    client.create_table(
        TableName=table_name,
        KeySchema=[{"AttributeName": CASE_TABLE_PARTITION_KEY, "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": CASE_TABLE_PARTITION_KEY, "AttributeType": "S"}
        ],
        BillingMode="PAY_PER_REQUEST",
    )

    waiter = client.get_waiter("table_exists")
    waiter.wait(
        TableName=table_name,
        WaiterConfig={
            "Delay": _TABLE_ACTIVE_WAITER_DELAY_SECONDS,
            "MaxAttempts": _TABLE_ACTIVE_WAITER_MAX_ATTEMPTS,
        },
    )

    return resource.Table(table_name)


def scan_existing_case_ids(table: Any) -> set[str]:
    """Read back the `case_id` of every item currently in `table`.

    Used to pass real table contents as `existing_case_ids` into
    `generate_case_batch`, guaranteeing that newly generated `case_id`
    values cannot collide with any record already present in the
    Case_Table -- not just with other records generated in the same batch
    (Requirement 1.3). This function only reads; it never modifies or
    deletes any item (Requirement 1.8).

    Args:
        table: A `boto3` DynamoDB `Table` resource for the `Case_Table`
            (as returned by `ensure_case_table`).

    Returns:
        The set of all `case_id` values currently stored in `table`.
        Empty when the table has no items.
    """
    existing_case_ids: set[str] = set()

    scan_kwargs: dict[str, Any] = {"ProjectionExpression": CASE_TABLE_PARTITION_KEY}
    while True:
        response = table.scan(**scan_kwargs)
        for item in response.get("Items", []):
            existing_case_ids.add(item[CASE_TABLE_PARTITION_KEY])

        last_evaluated_key = response.get("LastEvaluatedKey")
        if not last_evaluated_key:
            break
        scan_kwargs["ExclusiveStartKey"] = last_evaluated_key

    return existing_case_ids


def write_case_records(table: Any, records: list[CaseRecord]) -> int:
    """Write `records` to `table` via a batched, retrying write wrapper.

    Uses `Table.batch_writer()`, which automatically groups puts into
    `BatchWriteItem` calls (respecting the 25-item-per-request limit) and
    retries any unprocessed items -- functionally equivalent to a manual
    `BatchWriteItem`/`PutItem` loop, without needing to hand-implement
    batching/backoff.

    This function performs pure additions: every generated `CaseRecord`
    is expected to have a `case_id` already checked (via
    `scan_existing_case_ids` + `generate_case_batch`) to be absent from
    the table, so no existing item is overwritten in practice. This
    function does not delete or otherwise modify any pre-existing item.

    Args:
        table: A `boto3` DynamoDB `Table` resource for the `Case_Table`
            (as returned by `ensure_case_table`).
        records: The `CaseRecord`s to write.

    Returns:
        The number of records written (`len(records)`).
    """
    with table.batch_writer() as batch:
        for record in records:
            batch.put_item(
                Item={
                    "case_id": record.case_id,
                    "customer_name": record.customer_name,
                    "customer_account_id": record.customer_account_id,
                    "issue_description": record.issue_description,
                    "network_issue_category": record.network_issue_category,
                    "service_location_id": record.service_location_id,
                    "status": record.status,
                    "created_at": record.created_at,
                    "updated_at": record.updated_at,
                }
            )
    return len(records)


# --- CLI entrypoint (task 3.6) ----------------------------------------------
#
# Ties `ensure_case_table`, `scan_existing_case_ids`, `generate_case_batch`,
# and `write_case_records` together into a single, runnable end-to-end
# script invocation.
#
# Requirements covered:
#   1.1 - `ensure_case_table` creates the Case_Table (case_id partition
#         key) if it does not already exist, or reuses it otherwise.
#   1.2 - Each run adds a number of new Case_Records between 15 and 20
#         inclusive. The batch size for a given run is chosen once, up
#         front, as a uniformly random integer in that inclusive range
#         (`rng.randint(15, 20)`) rather than a single fixed constant, so
#         that repeated runs (and the demo data they produce) are not all
#         identically sized. Any fixed value in [15, 20] would also
#         satisfy this requirement; a random choice was preferred so the
#         generator's own boundary-count testing (task 3.5) has a
#         realistic analog to the value actually used at runtime.
#   11.3/11.4 - `--profile`/`--region` are optional CLI arguments; when
#         omitted, `agent.config.resolve_profile`/`resolve_region` (via
#         `ensure_case_table`'s internal `require_valid_credentials` call)
#         apply the `rs-gen-ai`/`us-west-2` defaults.
#   11.5/11.6 - The fail-closed credential check happens inside
#         `ensure_case_table` (via `require_valid_credentials`) before any
#         table-creating call; `agent.config.CredentialResolutionError` is
#         allowed to propagate out of `main()` uncaught here so its
#         "credential profile could not be resolved" message reaches the
#         operator on stderr with a non-zero exit status, rather than
#         being masked or swallowed.

# Requirement 1.2: inclusive range of new Case_Records added per run.
_MIN_BATCH_COUNT = 15
_MAX_BATCH_COUNT = 20


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments for `scripts/generate_case_data.py`.

    Both `--profile` and `--region` are optional: when omitted, the
    downstream `ensure_case_table` call resolves them to the
    `rs-gen-ai`/`us-west-2` defaults via `agent.config`
    (Requirements 11.3, 11.4).
    """
    parser = argparse.ArgumentParser(
        description=(
            "Case_Data_Generator: create/reuse the Case_Table in DynamoDB "
            "and add 15-20 new synthetic AnyTel support Case_Records to it."
        )
    )
    parser.add_argument(
        "--profile",
        default=None,
        help=(
            "AWS named profile to use. Defaults to the 'rs-gen-ai' profile "
            "(via agent.config.resolve_profile) when omitted."
        ),
    )
    parser.add_argument(
        "--region",
        default=None,
        help=(
            "AWS Region to use. Defaults to 'us-west-2' (via "
            "agent.config.resolve_region) when omitted."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    """Run the Case_Data_Generator end-to-end against real AWS.

    Resolves `--profile`/`--region` (falling through to the `rs-gen-ai`/
    `us-west-2` defaults when omitted), ensures the `Case_Table` exists
    (creating it if necessary), reads back the `case_id`s already present
    in the table, generates a new batch of 15-20 Case_Records disjoint
    from those existing ids, writes the new batch to the table, and
    prints a summary of what was written.

    Args:
        argv: Optional argument list (defaults to `sys.argv[1:]` via
            `argparse` when omitted). Primarily useful for testing.

    Raises:
        agent.config.CredentialResolutionError: propagated from
            `ensure_case_table` if the resolved AWS profile does not
            resolve to valid credentials; no table is created and no
            records are written in that case (Requirements 11.5, 11.6).
    """
    args = _parse_args(argv)

    # Requirement 11.5/11.6: the fail-closed credential check happens
    # inside ensure_case_table (via require_valid_credentials) before any
    # table-creating call is made.
    table = ensure_case_table(profile=args.profile, region=args.region)

    existing_case_ids = scan_existing_case_ids(table)

    # Requirement 1.2: choose this run's batch size uniformly at random
    # from the 15-20 inclusive range (see module-level comment above for
    # why a random choice was preferred over a single fixed constant).
    rng = random.Random()
    count = rng.randint(_MIN_BATCH_COUNT, _MAX_BATCH_COUNT)

    new_records = generate_case_batch(
        rng, list(RUNBOOK_CATEGORIES), existing_case_ids, count
    )

    written_count = write_case_records(table, new_records)

    resolved_region = args.region or "us-west-2"
    print(
        f"Wrote {written_count} new Case_Record(s) to table "
        f"'{table.name}' in region '{resolved_region}'. "
        f"Table now has at least {len(existing_case_ids) + written_count} "
        f"Case_Record(s) total."
    )


if __name__ == "__main__":
    main()
