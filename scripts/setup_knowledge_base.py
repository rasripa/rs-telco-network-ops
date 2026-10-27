"""scripts/setup_knowledge_base.py: Knowledge Base provisioning script.

Implements the design's "3. Knowledge Base setup
(scripts/setup_knowledge_base.py, scripts/sync_knowledge_base.py)"
component (.kiro/specs/network-ops-agent/design.md), Requirement 3.

This module is being built up incrementally across staggered tasks in the
same file (see tasks.md's task dependency graph notes):

  - Task 7.1: S3 bucket creation/reuse and Runbook_Corpus upload --
    `ensure_runbook_bucket` and `upload_runbook_corpus` above.
  - Task 7.2 (this task): `create_knowledge_base`, `create_data_source`,
    `start_and_poll_ingestion_job`, and ingestion-failure reporting, wired
    together with the task 7.1 functions into a runnable CLI entrypoint
    (`main`) below.

This module now performs the *complete* one-time Knowledge Base setup
flow end-to-end: create/reuse the S3 bucket, upload the Runbook_Corpus,
create the managed Knowledge_Base, create its S3-backed data source, and
run+poll the initial ingestion job (Requirements 3.1, 3.2, 3.3, 3.4).
Re-synchronizing after the Runbook_Corpus changes (Requirement 3.5) is a
separate, later script (`scripts/sync_knowledge_base.py`, task 7.3) that
re-invokes `StartIngestionJob` against the *existing* data source created
here -- this module's `main()` is a one-time setup entrypoint, not meant
to be re-run repeatedly (doing so would create additional Knowledge_Base/
data source resources rather than reusing the ones already provisioned;
see the `create_knowledge_base`/`create_data_source` docstrings below for
why no reuse-by-name check is implemented here).

Design note -- IAM service role ARN: a managed Knowledge_Base requires a
pre-existing IAM role ARN (`roleArn`) that Bedrock assumes to read the S3
data source and invoke the managed embedding model. Provisioning that
IAM role (and its trust/permissions policies) is infrastructure setup
that is out of scope for this *data*-provisioning script -- consistent
with this project's other setup scripts, which never create IAM roles
themselves. `create_knowledge_base` therefore accepts `role_arn` as an
explicit parameter and, when omitted, falls back to the
`NETWORK_OPS_KB_ROLE_ARN` environment variable (mirroring the explicit-
value-over-environment-variable pattern already used by
`agent/knowledge_base_retriever.py`'s `NETWORK_OPS_KNOWLEDGE_BASE_ID`);
if neither is available, `create_knowledge_base` raises `ValueError`
before making any AWS call, since a managed Knowledge_Base cannot be
created without one.
"""

from __future__ import annotations

import argparse
import os
import time
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    # Allow `python scripts/setup_knowledge_base.py` (direct script
    # execution) in addition to `python -m scripts.setup_knowledge_base`
    # (module execution). See scripts/generate_case_data.py for the same
    # bootstrap and rationale.
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import boto3
from botocore.exceptions import ClientError

from agent.config import require_valid_credentials

# --- S3 bucket creation/reuse and Runbook_Corpus upload (task 7.1) ---------
#
# Requirements covered:
#   3.1 - Provision (or reuse) the S3 bucket used as the Knowledge_Base's
#         data source within the Deployment_Account.
#   3.2 - Upload all Runbook_Corpus documents (the .md files and their
#         .md.metadata.json sidecars) to that data source so a later
#         ingestion job (task 7.2) can pick them up.
#   11.1/11.2 - Provisioned in us-west-2 using the rs-gen-ai profile.
#   11.5/11.6 - The fail-closed credential check
#         (`agent.config.require_valid_credentials`) is performed before
#         any resource-creating call (bucket creation).
#
# Bucket naming: S3 bucket names are globally unique across all AWS
# accounts, so a fixed literal name (e.g. "anytel-runbook-corpus") risks
# colliding with a bucket already owned by an unrelated AWS account and
# would make `create_bucket` fail with `BucketAlreadyExists` for reasons
# outside this project's control. To avoid that, the default bucket name
# incorporates both the resolved AWS account id (via `sts:GetCallerIdentity`,
# a read-only call) and the resolved region, which together make the
# default name unique to this Deployment_Account/region pair while still
# staying deterministic across repeated runs (so re-running this script
# reuses the same bucket rather than creating a new one each time).

DEFAULT_BUCKET_NAME_PREFIX = "anytel-runbook-corpus"

# Filename suffixes that make up the Runbook_Corpus: the rendered
# markdown documents themselves, and their metadata sidecars (see
# scripts/generate_runbooks.py's `write_runbook_document`).
_RUNBOOK_MARKDOWN_SUFFIX = ".md"
_RUNBOOK_METADATA_SUFFIX = ".md.metadata.json"


def _default_bucket_name(session: boto3.Session, region: str) -> str:
    """Build a deterministic, globally-unique-enough default bucket name
    for the given session's AWS account and region.

    Calls `sts:GetCallerIdentity` (read-only) to resolve the account id.
    """
    sts_client = session.client("sts")
    account_id = sts_client.get_caller_identity()["Account"]
    return f"{DEFAULT_BUCKET_NAME_PREFIX}-{account_id}-{region}".lower()


def _is_bucket_not_found_error(exc: ClientError) -> bool:
    """Return True if `exc` is the "bucket does not exist" ClientError
    raised by `head_bucket`, as opposed to some other failure (e.g.
    access denied) that must not be misinterpreted as "create it"."""
    error = exc.response.get("Error", {})
    error_code = str(error.get("Code", ""))
    if error_code in ("404", "NoSuchBucket", "NotFound"):
        return True
    status_code = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return status_code == 404


def ensure_runbook_bucket(
    profile: str | None = None,
    region: str | None = None,
    bucket_name: str | None = None,
) -> str:
    """Create the Runbook_Corpus data source S3 bucket if it does not
    exist; reuse it as-is otherwise.

    This is a resource-creating operation (Requirement 11.5), so the
    fail-closed credential check (`agent.config.require_valid_credentials`)
    is performed first: if the resolved profile does not resolve to valid
    AWS credentials, this function raises
    `agent.config.CredentialResolutionError` and no bucket is created.

    Idempotent reuse: if a bucket named `bucket_name` (or the resolved
    default name) already exists and is reachable, it is returned as-is.
    No existing object in the bucket is read, deleted, or modified by
    this function -- it only checks the bucket's existence via
    `head_bucket`.

    When the bucket does not exist, it is created via `create_bucket`.
    Regions other than `us-east-1` require an explicit
    `CreateBucketConfiguration.LocationConstraint`; `us-east-1` must NOT
    be passed a `LocationConstraint` (the API rejects it), so that is
    handled conditionally below.

    Args:
        profile: Explicit AWS named profile. Resolved against the
            `rs-gen-ai` default via `agent.config.resolve_profile` when
            omitted.
        region: Explicit AWS Region. Resolved against the `us-west-2`
            default via `agent.config.resolve_region` when omitted.
        bucket_name: Explicit S3 bucket name. When omitted, a
            deterministic default name incorporating the resolved AWS
            account id and region is used (see module-level comment
            above for why a fixed literal name is not used).

    Returns:
        The bucket name that was created or reused.

    Raises:
        agent.config.CredentialResolutionError: if the resolved profile
            does not resolve to valid AWS credentials. Raised before any
            bucket-creating call is made.
        botocore.exceptions.ClientError: propagated for any S3 failure
            other than "bucket not found" while checking for the
            bucket's existence, or for any failure while creating it.
    """
    resolved_profile, resolved_region = require_valid_credentials(profile, region)

    session = boto3.Session(profile_name=resolved_profile, region_name=resolved_region)
    resolved_bucket_name = bucket_name or _default_bucket_name(session, resolved_region)

    s3_client = session.client("s3", region_name=resolved_region)

    try:
        s3_client.head_bucket(Bucket=resolved_bucket_name)
        # Bucket already exists: reuse it as-is. No object is read,
        # deleted, or modified here.
        return resolved_bucket_name
    except ClientError as exc:
        if not _is_bucket_not_found_error(exc):
            raise

    # Bucket does not exist: create it. us-east-1 is the one region that
    # must NOT be given a CreateBucketConfiguration/LocationConstraint;
    # every other region requires one matching the target region.
    create_kwargs: dict[str, Any] = {"Bucket": resolved_bucket_name}
    if resolved_region != "us-east-1":
        create_kwargs["CreateBucketConfiguration"] = {
            "LocationConstraint": resolved_region
        }
    s3_client.create_bucket(**create_kwargs)

    return resolved_bucket_name


def _is_runbook_corpus_file(path: Path) -> bool:
    """Return True if `path` is a Runbook_Corpus file that should be
    uploaded: a rendered `.md` document or its `.md.metadata.json`
    sidecar (see scripts/generate_runbooks.py's `write_runbook_document`).
    """
    if not path.is_file():
        return False
    name = path.name
    return name.endswith(_RUNBOOK_METADATA_SUFFIX) or name.endswith(
        _RUNBOOK_MARKDOWN_SUFFIX
    )


def upload_runbook_corpus(
    bucket_name: str,
    runbooks_dir: str | Path = "runbooks",
    region: str | None = None,
    profile: str | None = None,
) -> list[str]:
    """Upload every Runbook_Corpus file in `runbooks_dir` to `bucket_name`.

    Iterates all `.md` documents and their `.md.metadata.json` sidecars in
    `runbooks_dir` (non-recursively; the design stores the complete
    Runbook_Corpus together in a single flat directory) and uploads each
    one to `bucket_name`, keyed by its filename alone. Keeping the key
    equal to the filename (rather than nesting it under a sub-prefix)
    guarantees a `.md` document and its `.md.metadata.json` sidecar sit
    at the same S3 key stem, which Amazon Bedrock knowledge bases require
    to associate a sidecar's `metadataAttributes` with its companion
    document (design.md, "2. Runbook_Generator").

    This function does not perform the fail-closed credential check
    itself: it is intended to be called after `ensure_runbook_bucket`
    (which already performed that check) with the bucket name it
    returned, mirroring `scripts/generate_case_data.py`'s
    `write_case_records`, which likewise assumes `ensure_case_table` has
    already run.

    Args:
        bucket_name: The S3 bucket to upload into (as returned by
            `ensure_runbook_bucket`).
        runbooks_dir: Local directory containing the Runbook_Corpus.
            Defaults to `"runbooks"`.
        region: Optional AWS Region override for the S3 client. When
            omitted, boto3's default region resolution applies.
        profile: Optional AWS named profile override for the S3 client.
            When omitted, boto3's default credential chain applies.

    Returns:
        The list of S3 object keys that were uploaded, in the order they
        were uploaded (sorted by filename for deterministic output).

    Raises:
        FileNotFoundError: if `runbooks_dir` does not exist.
        botocore.exceptions.ClientError: propagated for any S3 upload
            failure.
    """
    directory = Path(runbooks_dir)
    if not directory.is_dir():
        raise FileNotFoundError(f"runbooks directory not found: {directory}")

    session = boto3.Session(profile_name=profile, region_name=region)
    s3_client = session.client("s3", region_name=region)

    corpus_files = sorted(
        (path for path in directory.iterdir() if _is_runbook_corpus_file(path)),
        key=lambda path: path.name,
    )

    uploaded_keys: list[str] = []
    for file_path in corpus_files:
        key = file_path.name
        s3_client.upload_file(str(file_path), bucket_name, key)
        uploaded_keys.append(key)

    return uploaded_keys


# --- Knowledge Base + data source creation and ingestion (task 7.2) -------
#
# Requirements covered:
#   3.1 - Provision the managed Knowledge_Base (`create_knowledge_base`)
#         and its S3-backed data source (`create_data_source`) within the
#         Deployment_Account.
#   3.2 - `start_and_poll_ingestion_job` ingests every uploaded
#         Runbook_Corpus document (via task 7.1's `upload_runbook_corpus`)
#         into the Knowledge_Base's data source.
#   3.3 - Successfully ingested documents remain queryable regardless of
#         whether other documents failed: a partial failure (some
#         documents failed, the job otherwise completed) is treated as a
#         non-fatal outcome by `start_and_poll_ingestion_job` -- it does
#         not raise for that case, and ingestion of the remaining
#         documents is not something this script needs to separately
#         "continue", since a single `StartIngestionJob` call already
#         processes every document in the data source in one job; a
#         per-document failure does not stop the job from processing the
#         other documents.
#   3.4 - Failed document identifiers are read from
#         `ListKnowledgeBaseDocuments` (filtered to `status == "FAILED"`)
#         and printed to the operator, together with the job's
#         `failureReasons`/`statistics.numberOfDocumentsFailed`.
#
# Design note -- why `create_knowledge_base`/`create_data_source` do not
# check for an existing same-named resource before creating one: unlike
# `ensure_runbook_bucket`/`ensure_case_table` (which reuse an existing S3
# bucket/DynamoDB table by a caller-supplied *name*, since those
# resources are addressed by name), a Bedrock Knowledge_Base/data source
# is addressed by an opaque, service-generated id returned from
# `CreateKnowledgeBase`/`CreateDataSource` -- there is no "get by name"
# lookup analogous to `head_bucket`/`describe_table` to check for reuse
# before creating. `scripts/sync_knowledge_base.py` (task 7.3) is the
# script responsible for operating against an *already-created*
# Knowledge_Base/data source id (re-invoking `StartIngestionJob`) once
# this one-time setup script has produced them.

# The Knowledge_Base's managed-connector data source has no separately
# provisioned vector store to configure -- see the module docstring and
# design.md's "Knowledge Base setup" component section.
DEFAULT_KNOWLEDGE_BASE_NAME = "AnyTel-Network-Ops-Knowledge-Base"
DEFAULT_DATA_SOURCE_NAME = "anytel-runbook-corpus-s3"

# Environment variable providing the pre-provisioned IAM service role ARN
# a managed Knowledge_Base assumes to read its S3 data source and invoke
# the managed embedding model, when `role_arn` is not passed explicitly
# to `create_knowledge_base` (see the module docstring's "Design note --
# IAM service role ARN" for why this script does not create that role
# itself).
KNOWLEDGE_BASE_ROLE_ARN_ENV_VAR = "NETWORK_OPS_KB_ROLE_ARN"

# `ManagedKnowledgeBaseConnectorConfiguration.connectorParameters` for the
# S3 connector type is versioned by AWS; "1" is the only version
# documented at the time of writing (see
# https://docs.aws.amazon.com/kendra/latest/dg/kendra-availability-change.html's
# "Step 3: Configure Data Sources" managed-connector example).
_S3_CONNECTOR_PARAMETERS_VERSION = "1"

# Ingestion job statuses that mean "no further polling is needed" (the
# job has reached a terminal state). COMPLETE is the only status that is
# NOT treated as a failure by `start_and_poll_ingestion_job` -- FAILED and
# STOPPED both indicate the *entire* job did not finish normally, as
# distinct from a partial per-document failure within an otherwise
# COMPLETE job (Requirement 3.3).
_INGESTION_JOB_TERMINAL_STATUSES = frozenset({"COMPLETE", "FAILED", "STOPPED"})
_INGESTION_JOB_FAILED_STATUSES = frozenset({"FAILED", "STOPPED"})


class IngestionJobFailedError(RuntimeError):
    """Raised when an ingestion job's terminal status is `FAILED` or
    `STOPPED` -- i.e. the *entire* job did not complete, as opposed to a
    partial failure where some individual documents failed but the job
    otherwise reached `COMPLETE`.

    Per Requirement 3.3/3.4, a partial per-document failure is NOT treated
    as fatal (successfully ingested documents remain queryable regardless
    of other documents' failures), so `start_and_poll_ingestion_job` does
    NOT raise this for that case -- only for a fully failed/stopped job.
    """


class IngestionJobPollingTimeoutError(RuntimeError):
    """Raised when an ingestion job does not reach a terminal status
    (`COMPLETE`, `FAILED`, or `STOPPED`) within `max_poll_attempts` polls
    of `GetIngestionJob`.

    This is distinct from `IngestionJobFailedError`: the job may still be
    legitimately in progress server-side when this is raised; it only
    means this function gave up waiting for it to finish.
    """


def create_knowledge_base(
    name: str = DEFAULT_KNOWLEDGE_BASE_NAME,
    role_arn: str | None = None,
    description: str | None = None,
    profile: str | None = None,
    region: str | None = None,
) -> str:
    """Create a managed Knowledge_Base (Requirement 3.1).

    Calls `bedrock-agent:CreateKnowledgeBase` with
    `knowledgeBaseConfiguration.type = "MANAGED"` and
    `managedKnowledgeBaseConfiguration.embeddingModelType = "MANAGED"`, so
    Bedrock uses its own fully managed embedding model and vector store --
    no `storageConfiguration`/vector store of any kind is provisioned or
    referenced by this call (design.md, "3. Knowledge Base setup").

    This is a resource-creating operation (Requirement 11.5), so the
    fail-closed credential check (`agent.config.require_valid_credentials`)
    is performed first: if the resolved profile does not resolve to valid
    AWS credentials, this function raises
    `agent.config.CredentialResolutionError` and no Knowledge_Base is
    created.

    Args:
        name: The Knowledge_Base's name. Defaults to
            `DEFAULT_KNOWLEDGE_BASE_NAME`.
        role_arn: The ARN of the pre-provisioned IAM service role Bedrock
            assumes to read the data source and invoke the managed
            embedding model. When omitted, falls back to the
            `NETWORK_OPS_KB_ROLE_ARN` environment variable (see the
            module docstring's "Design note -- IAM service role ARN").
        description: Optional human-readable description.
        profile: Explicit AWS named profile. Resolved against the
            `rs-gen-ai` default via `agent.config.resolve_profile` when
            omitted.
        region: Explicit AWS Region. Resolved against the `us-west-2`
            default via `agent.config.resolve_region` when omitted.

    Returns:
        The created Knowledge_Base's `knowledgeBaseId`.

    Raises:
        ValueError: if `role_arn` is omitted and the
            `NETWORK_OPS_KB_ROLE_ARN` environment variable is also unset,
            since a managed Knowledge_Base cannot be created without a
            service role. Raised before any AWS call is made.
        agent.config.CredentialResolutionError: if the resolved profile
            does not resolve to valid AWS credentials. Raised before any
            resource-creating call is made.
        botocore.exceptions.ClientError: propagated for any
            `CreateKnowledgeBase` failure.
    """
    resolved_profile, resolved_region = require_valid_credentials(profile, region)

    resolved_role_arn = role_arn or os.environ.get(KNOWLEDGE_BASE_ROLE_ARN_ENV_VAR)
    if not resolved_role_arn:
        raise ValueError(
            "no IAM service role ARN provided: pass role_arn explicitly or "
            f"set the {KNOWLEDGE_BASE_ROLE_ARN_ENV_VAR} environment "
            "variable to the ARN of a pre-provisioned IAM role Bedrock "
            "can assume to create the managed Knowledge_Base"
        )

    session = boto3.Session(profile_name=resolved_profile, region_name=resolved_region)
    bedrock_agent_client = session.client("bedrock-agent", region_name=resolved_region)

    create_kwargs: dict[str, Any] = {
        "name": name,
        "roleArn": resolved_role_arn,
        "knowledgeBaseConfiguration": {
            "type": "MANAGED",
            "managedKnowledgeBaseConfiguration": {
                "embeddingModelType": "MANAGED",
            },
        },
    }
    if description:
        create_kwargs["description"] = description

    response = bedrock_agent_client.create_knowledge_base(**create_kwargs)
    return response["knowledgeBase"]["knowledgeBaseId"]


def create_data_source(
    knowledge_base_id: str,
    bucket_name: str,
    name: str = DEFAULT_DATA_SOURCE_NAME,
    bucket_owner_account_id: str | None = None,
    profile: str | None = None,
    region: str | None = None,
) -> str:
    """Create an S3-backed managed-connector data source for a
    Knowledge_Base (Requirement 3.1, 3.2).

    Calls `bedrock-agent:CreateDataSource` with
    `dataSourceConfiguration.type = "MANAGED_KNOWLEDGE_BASE_CONNECTOR"`
    and a `managedKnowledgeBaseConnectorConfiguration.connectorParameters`
    of `type = "S3"` pointing at `bucket_name` (design.md, "3. Knowledge
    Base setup"), so the Knowledge_Base's later ingestion job
    (`start_and_poll_ingestion_job`) picks up every Runbook_Corpus file
    uploaded there by task 7.1's `upload_runbook_corpus`.

    This is a resource-creating operation (Requirement 11.5), so the
    fail-closed credential check (`agent.config.require_valid_credentials`)
    is performed first: if the resolved profile does not resolve to valid
    AWS credentials, this function raises
    `agent.config.CredentialResolutionError` and no data source is
    created.

    Note: `CreateDataSource` is asynchronous for managed Knowledge_Bases;
    the returned data source transitions from `CREATING` to `AVAILABLE`
    (typically within a few minutes) before it can be used in an
    ingestion job. This function does not itself wait for that
    transition -- `start_and_poll_ingestion_job`'s own polling loop (and
    `StartIngestionJob`'s own validation) surfaces any resulting failure
    if ingestion is attempted before the data source is `AVAILABLE`.

    Args:
        knowledge_base_id: The `knowledgeBaseId` to attach this data
            source to (as returned by `create_knowledge_base`).
        bucket_name: The S3 bucket the data source reads from (as
            returned by `ensure_runbook_bucket`).
        name: The data source's name. Defaults to
            `DEFAULT_DATA_SOURCE_NAME`.
        bucket_owner_account_id: The AWS account id that owns
            `bucket_name`. When omitted, resolved via
            `sts:GetCallerIdentity` under the resolved profile (i.e. the
            same account the Knowledge_Base itself is being created in,
            which matches this project's own bucket -- see
            `ensure_runbook_bucket`).
        profile: Explicit AWS named profile. Resolved against the
            `rs-gen-ai` default via `agent.config.resolve_profile` when
            omitted.
        region: Explicit AWS Region. Resolved against the `us-west-2`
            default via `agent.config.resolve_region` when omitted.

    Returns:
        The created data source's `dataSourceId`.

    Raises:
        agent.config.CredentialResolutionError: if the resolved profile
            does not resolve to valid AWS credentials. Raised before any
            resource-creating call is made.
        botocore.exceptions.ClientError: propagated for any
            `CreateDataSource` (or, when resolving
            `bucket_owner_account_id`, `GetCallerIdentity`) failure.
    """
    resolved_profile, resolved_region = require_valid_credentials(profile, region)

    session = boto3.Session(profile_name=resolved_profile, region_name=resolved_region)
    bedrock_agent_client = session.client("bedrock-agent", region_name=resolved_region)

    resolved_owner_account_id = bucket_owner_account_id
    if not resolved_owner_account_id:
        sts_client = session.client("sts")
        resolved_owner_account_id = sts_client.get_caller_identity()["Account"]

    response = bedrock_agent_client.create_data_source(
        knowledgeBaseId=knowledge_base_id,
        name=name,
        dataSourceConfiguration={
            "type": "MANAGED_KNOWLEDGE_BASE_CONNECTOR",
            "managedKnowledgeBaseConnectorConfiguration": {
                "connectorParameters": {
                    "type": "S3",
                    "version": _S3_CONNECTOR_PARAMETERS_VERSION,
                    "connectionConfiguration": {
                        "bucketName": bucket_name,
                        "bucketOwnerAccountId": resolved_owner_account_id,
                    },
                }
            },
        },
    )
    return response["dataSource"]["dataSourceId"]


def _document_identifier_to_string(identifier: dict[str, Any]) -> str:
    """Render a `DocumentIdentifier` (from a `ListKnowledgeBaseDocuments`
    `documentDetails[]` entry) as a single human-readable string
    identifying the affected document, for the ingestion-failure report
    (Requirement 3.4).
    """
    data_source_type = identifier.get("dataSourceType")
    if data_source_type == "S3":
        return identifier.get("s3", {}).get("uri") or "<unknown S3 document>"
    if data_source_type == "CUSTOM":
        return identifier.get("custom", {}).get("id") or "<unknown custom document>"
    return "<unknown document>"


def _list_failed_document_identifiers(
    bedrock_agent_client: Any, knowledge_base_id: str, data_source_id: str
) -> list[str]:
    """Return the identifiers of every document in `data_source_id` whose
    current ingestion `status` is `FAILED`, via
    `bedrock-agent:ListKnowledgeBaseDocuments` (Requirement 3.4).

    Paginates through the full result set using `nextToken` so no failed
    document is missed when the data source has more documents than a
    single page returns.
    """
    failed_identifiers: list[str] = []
    list_kwargs: dict[str, Any] = {
        "knowledgeBaseId": knowledge_base_id,
        "dataSourceId": data_source_id,
    }
    while True:
        response = bedrock_agent_client.list_knowledge_base_documents(**list_kwargs)
        for detail in response.get("documentDetails", []):
            if detail.get("status") == "FAILED":
                failed_identifiers.append(
                    _document_identifier_to_string(detail.get("identifier", {}))
                )

        next_token = response.get("nextToken")
        if not next_token:
            break
        list_kwargs["nextToken"] = next_token

    return failed_identifiers


def start_and_poll_ingestion_job(
    knowledge_base_id: str,
    data_source_id: str,
    profile: str | None = None,
    region: str | None = None,
    poll_interval_seconds: float = 5,
    max_poll_attempts: int = 60,
) -> dict[str, Any]:
    """Start an ingestion job and poll it to completion, reporting any
    per-document ingestion failures (Requirements 3.2, 3.3, 3.4).

    Calls `bedrock-agent:StartIngestionJob` for `data_source_id`, then
    polls `bedrock-agent:GetIngestionJob` (sleeping `poll_interval_seconds`
    between polls, up to `max_poll_attempts` times) until the job reaches
    a terminal status: `COMPLETE`, `FAILED`, or `STOPPED`.

    Partial failure handling (Requirement 3.3, 3.4): if the job reaches
    `COMPLETE` but its `statistics.numberOfDocumentsFailed` is greater
    than zero, this function does NOT raise -- successfully ingested
    documents remain queryable regardless of other documents' failures.
    Instead, it looks up every document with `status == "FAILED"` via
    `ListKnowledgeBaseDocuments` (`_list_failed_document_identifiers`),
    prints each failed document's identifier together with the job's
    overall failure count, and returns them in the result dict so a
    caller (e.g. `main()`) can include them in its own summary.

    Fatal outcomes: this function DOES raise when the *entire* job fails
    to complete -- either because the terminal status is `FAILED`/
    `STOPPED` (`IngestionJobFailedError`, carrying the job's
    `failureReasons`), or because a `StartIngestionJob`/`GetIngestionJob`
    call itself raises a `botocore.exceptions.ClientError` (propagated
    as-is), or because polling exhausts `max_poll_attempts` without the
    job ever reaching a terminal status
    (`IngestionJobPollingTimeoutError`).

    Args:
        knowledge_base_id: The Knowledge_Base's id (as returned by
            `create_knowledge_base`).
        data_source_id: The data source's id (as returned by
            `create_data_source`).
        profile: Explicit AWS named profile. When omitted, boto3's
            default credential chain applies (this function does not
            perform the fail-closed credential check itself -- it
            assumes `create_knowledge_base`/`create_data_source` already
            did, mirroring `upload_runbook_corpus`'s relationship to
            `ensure_runbook_bucket`).
        region: Explicit AWS Region override. When omitted, boto3's
            default region resolution applies.
        poll_interval_seconds: Seconds to sleep between successive
            `GetIngestionJob` polls. Defaults to 5.
        max_poll_attempts: Maximum number of `GetIngestionJob` polls
            before giving up and raising
            `IngestionJobPollingTimeoutError`. Defaults to 60 (so, with
            the default 5-second interval, up to 5 minutes).

    Returns:
        A dict with:
          - `"ingestion_job_id"`: the started job's id.
          - `"status"`: the job's final observed status (always
            `"COMPLETE"` when this function returns normally, since
            `FAILED`/`STOPPED` raise instead).
          - `"statistics"`: the job's raw `statistics` dict from the
            final `GetIngestionJob` response (document/metadata scanned/
            indexed/failed/skipped/deleted counts).
          - `"failed_document_count"`: `statistics.numberOfDocumentsFailed`
            (0 when every document ingested successfully).
          - `"failed_document_identifiers"`: the list of identifiers (S3
            URIs or custom ids) of documents whose ingestion status is
            `FAILED`; empty when `failed_document_count` is 0.

    Raises:
        IngestionJobFailedError: if the job's terminal status is `FAILED`
            or `STOPPED` (the entire job did not complete).
        IngestionJobPollingTimeoutError: if the job never reaches a
            terminal status within `max_poll_attempts` polls.
        botocore.exceptions.ClientError: propagated for any
            `StartIngestionJob`/`GetIngestionJob`/
            `ListKnowledgeBaseDocuments` API-level failure.
    """
    session = boto3.Session(profile_name=profile, region_name=region)
    bedrock_agent_client = session.client("bedrock-agent", region_name=region)

    start_response = bedrock_agent_client.start_ingestion_job(
        knowledgeBaseId=knowledge_base_id,
        dataSourceId=data_source_id,
    )
    job = start_response["ingestionJob"]
    ingestion_job_id = job["ingestionJobId"]
    status = job.get("status")

    attempts = 0
    while status not in _INGESTION_JOB_TERMINAL_STATUSES:
        if attempts >= max_poll_attempts:
            raise IngestionJobPollingTimeoutError(
                f"ingestion job {ingestion_job_id} did not reach a "
                f"terminal status after {max_poll_attempts} polls "
                f"(last observed status: {status!r})"
            )
        time.sleep(poll_interval_seconds)
        attempts += 1

        job_response = bedrock_agent_client.get_ingestion_job(
            knowledgeBaseId=knowledge_base_id,
            dataSourceId=data_source_id,
            ingestionJobId=ingestion_job_id,
        )
        job = job_response["ingestionJob"]
        status = job.get("status")

    statistics = job.get("statistics") or {}
    failed_document_count = statistics.get("numberOfDocumentsFailed", 0) or 0

    if status in _INGESTION_JOB_FAILED_STATUSES:
        failure_reasons = job.get("failureReasons") or []
        raise IngestionJobFailedError(
            f"ingestion job {ingestion_job_id} ended with status "
            f"{status!r}; failureReasons={failure_reasons!r}"
        )

    # Requirement 3.3/3.4: status == "COMPLETE" here (the only status not
    # in _INGESTION_JOB_FAILED_STATUSES among the terminal ones). A
    # partial per-document failure does not raise -- report it instead.
    failed_document_identifiers: list[str] = []
    if failed_document_count:
        failed_document_identifiers = _list_failed_document_identifiers(
            bedrock_agent_client, knowledge_base_id, data_source_id
        )
        print(
            f"Ingestion job {ingestion_job_id} completed with "
            f"{failed_document_count} failed document(s); ingestion of "
            "the remaining documents continued and they remain "
            "queryable. Failed document identifier(s):"
        )
        for identifier in failed_document_identifiers:
            print(f"  - {identifier}")
    else:
        print(f"Ingestion job {ingestion_job_id} completed with no failed documents.")

    return {
        "ingestion_job_id": ingestion_job_id,
        "status": status,
        "statistics": statistics,
        "failed_document_count": failed_document_count,
        "failed_document_identifiers": failed_document_identifiers,
    }


# --- CLI entrypoint (task 7.2) -----------------------------------------------
#
# Ties `ensure_runbook_bucket`, `upload_runbook_corpus`,
# `create_knowledge_base`, `create_data_source`, and
# `start_and_poll_ingestion_job` together into a single, runnable
# end-to-end Knowledge Base setup script invocation. There is no
# dedicated later "CLI wiring" task for this script in tasks.md (unlike,
# e.g., generate_case_data.py's separate task 3.6) -- task 7.2 is the
# final task touching this file per the task dependency graph's staggered
# edit sequence (7.1 -> 7.2), so the CLI entrypoint is included here.
#
# Requirements covered: 3.1, 3.2, 3.3, 3.4 (end-to-end), plus 11.1-11.6
# via the fail-closed credential check performed inside
# `ensure_runbook_bucket`/`create_knowledge_base`/`create_data_source`.


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments for `scripts/setup_knowledge_base.py`."""
    parser = argparse.ArgumentParser(
        description=(
            "Knowledge Base setup: create/reuse the Runbook_Corpus S3 "
            "bucket, upload the corpus, create the managed Knowledge_Base "
            "and its S3 data source, and run the initial ingestion job."
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
    parser.add_argument(
        "--bucket-name",
        default=None,
        help=(
            "Explicit S3 bucket name for the Runbook_Corpus data source. "
            "Defaults to a deterministic name derived from the resolved "
            "AWS account id and region when omitted."
        ),
    )
    parser.add_argument(
        "--runbooks-dir",
        default="runbooks",
        help="Local directory containing the Runbook_Corpus. Defaults to 'runbooks'.",
    )
    parser.add_argument(
        "--knowledge-base-name",
        default=DEFAULT_KNOWLEDGE_BASE_NAME,
        help=f"Knowledge_Base name. Defaults to '{DEFAULT_KNOWLEDGE_BASE_NAME}'.",
    )
    parser.add_argument(
        "--role-arn",
        default=None,
        help=(
            "IAM service role ARN for the managed Knowledge_Base. Defaults "
            f"to the {KNOWLEDGE_BASE_ROLE_ARN_ENV_VAR} environment "
            "variable when omitted."
        ),
    )
    parser.add_argument(
        "--data-source-name",
        default=DEFAULT_DATA_SOURCE_NAME,
        help=f"Data source name. Defaults to '{DEFAULT_DATA_SOURCE_NAME}'.",
    )
    parser.add_argument(
        "--poll-interval-seconds",
        type=float,
        default=5,
        help="Seconds between ingestion job status polls. Defaults to 5.",
    )
    parser.add_argument(
        "--max-poll-attempts",
        type=int,
        default=60,
        help="Maximum number of ingestion job status polls. Defaults to 60.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    """Run the complete Knowledge Base setup flow end-to-end against real
    AWS.

    Resolves `--profile`/`--region` (falling through to the `rs-gen-ai`/
    `us-west-2` defaults when omitted), then in order:

      1. `ensure_runbook_bucket` -- create/reuse the S3 bucket.
      2. `upload_runbook_corpus` -- upload every Runbook_Corpus file.
      3. `create_knowledge_base` -- create the managed Knowledge_Base.
      4. `create_data_source` -- create its S3-backed data source.
      5. `start_and_poll_ingestion_job` -- run and poll the initial
         ingestion job, reporting any failed document identifiers.

    Prints a final summary including the created Knowledge_Base id, data
    source id, and any failed document identifiers.

    Args:
        argv: Optional argument list (defaults to `sys.argv[1:]` via
            `argparse` when omitted). Primarily useful for testing.

    Raises:
        agent.config.CredentialResolutionError: propagated if the
            resolved AWS profile does not resolve to valid credentials;
            no resource is created in that case (Requirements 11.5, 11.6).
        ValueError: propagated from `create_knowledge_base` if no IAM
            service role ARN is available.
        IngestionJobFailedError: propagated if the initial ingestion job
            fails/stops entirely (as opposed to a partial per-document
            failure, which is reported but does not raise).
        IngestionJobPollingTimeoutError: propagated if the initial
            ingestion job does not reach a terminal status within the
            configured polling budget.
    """
    args = _parse_args(argv)

    bucket_name = ensure_runbook_bucket(
        profile=args.profile, region=args.region, bucket_name=args.bucket_name
    )
    uploaded_keys = upload_runbook_corpus(
        bucket_name,
        runbooks_dir=args.runbooks_dir,
        region=args.region,
        profile=args.profile,
    )
    print(f"Uploaded {len(uploaded_keys)} Runbook_Corpus file(s) to bucket '{bucket_name}'.")

    knowledge_base_id = create_knowledge_base(
        name=args.knowledge_base_name,
        role_arn=args.role_arn,
        profile=args.profile,
        region=args.region,
    )
    print(f"Created Knowledge_Base '{knowledge_base_id}'.")

    data_source_id = create_data_source(
        knowledge_base_id,
        bucket_name,
        name=args.data_source_name,
        profile=args.profile,
        region=args.region,
    )
    print(f"Created data source '{data_source_id}' for bucket '{bucket_name}'.")

    ingestion_result = start_and_poll_ingestion_job(
        knowledge_base_id,
        data_source_id,
        profile=args.profile,
        region=args.region,
        poll_interval_seconds=args.poll_interval_seconds,
        max_poll_attempts=args.max_poll_attempts,
    )

    failed_count = ingestion_result["failed_document_count"]
    print(
        "Knowledge Base setup complete: "
        f"knowledge_base_id='{knowledge_base_id}', "
        f"data_source_id='{data_source_id}', "
        f"ingestion_job_id='{ingestion_result['ingestion_job_id']}', "
        f"failed_document_count={failed_count}."
    )
    if failed_count:
        print(
            "Failed document identifier(s) (successfully ingested "
            "documents remain queryable):"
        )
        for identifier in ingestion_result["failed_document_identifiers"]:
            print(f"  - {identifier}")


if __name__ == "__main__":
    main()
