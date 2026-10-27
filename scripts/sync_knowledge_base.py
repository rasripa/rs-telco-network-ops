"""scripts/sync_knowledge_base.py: Runbook_Corpus re-synchronization.

Implements the design's "3. Knowledge Base setup
(scripts/setup_knowledge_base.py, scripts/sync_knowledge_base.py)"
component (.kiro/specs/network-ops-agent/design.md), Requirement 3.5
(task 7.3).

Unlike `scripts/setup_knowledge_base.py` (task 7.1/7.2), which performs
the *one-time* provisioning of the S3 bucket, the managed Knowledge_Base,
and its data source, this module operates against resources that have
*already been created* by that script. Because a Bedrock Knowledge_Base
and data source are addressed by an opaque, service-generated id (there
is no "get by name" lookup analogous to `head_bucket`/`describe_table`;
see `setup_knowledge_base.py`'s own "Design note" on this), this script
requires the operator to supply the already-provisioned
`--bucket-name`/`--knowledge-base-id`/`--data-source-id` explicitly
rather than trying to discover or re-create them.

Re-synchronization flow (Requirement 3.5 -- "added/modified/removed
Runbook_Corpus files are reflected in queryable content"):

  1. Re-upload the current `runbooks/` directory contents to the
     existing bucket via `setup_knowledge_base.upload_runbook_corpus`
     (reused, not duplicated). Uploading with the same S3 key as the
     filename means a new local file becomes a new S3 object (an
     *addition*) and a changed local file overwrites its existing S3
     object with new content (a *modification*) -- both are reflected
     simply by re-running the same upload.
  2. Delete, from the bucket, any Runbook_Corpus S3 object whose key is
     no longer present among the just-uploaded keys (`_prune_removed_
     runbook_documents`, defined in this module). This step is the
     result of the following reasoning, spelled out because design.md
     does not make it explicit:

     `upload_runbook_corpus` only ever uploads/overwrites; it never
     deletes an S3 object for a file that was removed from the local
     `runbooks/` directory. Design.md's sentence "Bedrock's incremental
     sync detects additions, content changes, and removals
     automatically" describes Bedrock's *ingestion-side* change
     detection relative to what currently exists in the S3 data
     source -- it does not, and cannot, look at the local filesystem.
     AWS's own documentation for the S3 data source connector describes
     this precisely: the connector "crawls new, modified, and deleted
     content each time your data source syncs", and on sync, a
     "Document deleted" scenario means "The document is removed from
     the vector store" -- i.e. removal detection is keyed off the
     document disappearing from S3, not off it disappearing from a local
     directory that Bedrock never sees.

     Consequently, if a document is deleted from the local `runbooks/`
     directory but its S3 object is left in place, Bedrock's incremental
     sync has nothing to detect: the object is still present and
     unchanged in S3, so the corresponding Knowledge_Base content is
     never removed, and Requirement 3.5's "excludes each document
     removed from the Runbook_Corpus" clause would not be satisfied.
     Deleting the stale S3 object here -- *before* re-invoking
     ingestion -- is therefore necessary (not merely a nice-to-have) for
     this script to fully satisfy Requirement 3.5, and is in scope for
     this task even though design.md's prose does not spell out the S3
     deletion step explicitly.
  3. Re-invoke `setup_knowledge_base.start_and_poll_ingestion_job`
     against the existing `--data-source-id` (reused, not duplicated),
     so Bedrock re-scans the data source, ingests every addition/
     modification, and removes every deletion performed in step 2 from
     the Knowledge_Base's queryable content -- printing a summary
     including any failed document identifiers (Requirements 3.3, 3.4,
     3.5).

See:
https://docs.aws.amazon.com/bedrock/latest/userguide/kb-data-source-sync-ingest.html
https://docs.aws.amazon.com/bedrock/latest/userguide/s3-data-source-connector.html
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Iterable

if __package__ in (None, ""):
    # Allow `python scripts/sync_knowledge_base.py` (direct script
    # execution) in addition to `python -m scripts.sync_knowledge_base`
    # (module execution). See scripts/generate_case_data.py for the same
    # bootstrap and rationale.
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import boto3

from agent.config import require_valid_credentials
from scripts.setup_knowledge_base import (
    start_and_poll_ingestion_job,
    upload_runbook_corpus,
)

# Filename/key suffixes that make up the Runbook_Corpus in S3 -- mirrors
# scripts/setup_knowledge_base.py's `_RUNBOOK_MARKDOWN_SUFFIX`/
# `_RUNBOOK_METADATA_SUFFIX` (redeclared here, rather than importing that
# module's underscore-prefixed constants, so this module does not depend
# on setup_knowledge_base.py's private implementation details -- only on
# its public `upload_runbook_corpus`/`start_and_poll_ingestion_job`
# functions per the task's reuse-not-duplicate guidance).
_RUNBOOK_MARKDOWN_SUFFIX = ".md"
_RUNBOOK_METADATA_SUFFIX = ".md.metadata.json"


def _is_runbook_corpus_key(key: str) -> bool:
    """Return True if `key` is a Runbook_Corpus object key: a rendered
    `.md` document or its `.md.metadata.json` sidecar. Mirrors
    `setup_knowledge_base._is_runbook_corpus_file`, operating on an S3
    key string rather than a local `Path`.

    Scoping deletion to only these two suffixes ensures
    `_prune_removed_runbook_documents` never deletes an unrelated S3
    object that might happen to share the bucket for some other purpose.
    """
    return key.endswith(_RUNBOOK_METADATA_SUFFIX) or key.endswith(
        _RUNBOOK_MARKDOWN_SUFFIX
    )


def _prune_removed_runbook_documents(
    bucket_name: str,
    current_keys: Iterable[str],
    region: str | None = None,
    profile: str | None = None,
) -> list[str]:
    """Delete every Runbook_Corpus S3 object in `bucket_name` whose key is
    not present in `current_keys`, so that a locally-removed document is
    also removed from S3 before ingestion re-runs (see the module
    docstring's step 2 for why this is required for Requirement 3.5).

    Lists every object in `bucket_name` (paginating via
    `ListObjectsV2`'s `ContinuationToken`), filters to Runbook_Corpus
    object keys (`_is_runbook_corpus_key`) so unrelated objects are never
    touched, and deletes (via `DeleteObjects`, batched to at most 1000
    keys per call per the API's limit) every such key absent from
    `current_keys` -- i.e. every Runbook_Corpus file that existed in the
    bucket from a prior sync but is no longer part of the current local
    `runbooks/` directory.

    This function does not perform the fail-closed credential check
    itself: like `upload_runbook_corpus`, it is intended to be called
    after the caller (`main`, below) has already performed that check.

    Args:
        bucket_name: The existing Runbook_Corpus S3 bucket (as supplied
            by the operator via `--bucket-name`).
        current_keys: The S3 object keys that correspond to the
            Runbook_Corpus files currently present locally (as returned
            by `upload_runbook_corpus`). Any Runbook_Corpus key found in
            the bucket but absent from this collection is considered
            removed and is deleted.
        region: Optional AWS Region override for the S3 client. When
            omitted, boto3's default region resolution applies.
        profile: Optional AWS named profile override for the S3 client.
            When omitted, boto3's default credential chain applies.

    Returns:
        The sorted list of S3 object keys that were deleted (empty if no
        Runbook_Corpus document was removed locally since the last
        sync).

    Raises:
        botocore.exceptions.ClientError: propagated for any
            `ListObjectsV2`/`DeleteObjects` failure.
    """
    current_key_set = set(current_keys)

    session = boto3.Session(profile_name=profile, region_name=region)
    s3_client = session.client("s3", region_name=region)

    stale_keys: list[str] = []
    list_kwargs: dict[str, Any] = {"Bucket": bucket_name}
    while True:
        response = s3_client.list_objects_v2(**list_kwargs)
        for obj in response.get("Contents", []):
            key = obj["Key"]
            if _is_runbook_corpus_key(key) and key not in current_key_set:
                stale_keys.append(key)

        if not response.get("IsTruncated"):
            break
        list_kwargs["ContinuationToken"] = response["NextContinuationToken"]

    stale_keys.sort()

    # DeleteObjects accepts at most 1000 keys per call; batch defensively
    # even though the Runbook_Corpus is expected to be far smaller.
    for batch_start in range(0, len(stale_keys), 1000):
        batch = stale_keys[batch_start : batch_start + 1000]
        s3_client.delete_objects(
            Bucket=bucket_name,
            Delete={"Objects": [{"Key": key} for key in batch]},
        )

    return stale_keys


# --- CLI entrypoint -----------------------------------------------------
#
# Requirements covered: 3.5 (re-synchronization), plus 11.3/11.4 (the
# --profile/--region defaults resolve to rs-gen-ai/us-west-2 when
# omitted) and 11.5/11.6 (the fail-closed credential check is performed
# up front, before the S3 upload/delete or ingestion calls that follow).


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments for `scripts/sync_knowledge_base.py`."""
    parser = argparse.ArgumentParser(
        description=(
            "Re-synchronize an already-provisioned Knowledge_Base's data "
            "source with the current contents of the local Runbook_Corpus "
            "directory: re-upload additions/modifications, prune S3 "
            "objects for locally-removed documents, and re-invoke "
            "ingestion."
        )
    )
    parser.add_argument(
        "--bucket-name",
        required=True,
        help=(
            "Name of the already-provisioned S3 bucket backing the "
            "Knowledge_Base's data source (as created by "
            "scripts/setup_knowledge_base.py). Required: this script has "
            "no 'get by name' lookup for it."
        ),
    )
    parser.add_argument(
        "--knowledge-base-id",
        required=True,
        help=(
            "The already-provisioned Knowledge_Base's id (as returned by "
            "scripts/setup_knowledge_base.py's create_knowledge_base). "
            "Required: a Knowledge_Base is addressed by an opaque, "
            "service-generated id with no 'get by name' lookup."
        ),
    )
    parser.add_argument(
        "--data-source-id",
        required=True,
        help=(
            "The already-provisioned data source's id (as returned by "
            "scripts/setup_knowledge_base.py's create_data_source). "
            "Required for the same reason as --knowledge-base-id."
        ),
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
        "--runbooks-dir",
        default="runbooks",
        help="Local directory containing the Runbook_Corpus. Defaults to 'runbooks'.",
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
    """Run the Runbook_Corpus re-synchronization flow end-to-end against
    real AWS (Requirement 3.5).

    Resolves `--profile`/`--region` (falling through to the `rs-gen-ai`/
    `us-west-2` defaults when omitted) and performs the fail-closed
    credential check (`agent.config.require_valid_credentials`) before
    any AWS call, then in order:

      1. `upload_runbook_corpus` -- re-upload every current
         Runbook_Corpus file to `--bucket-name` (reflects additions and
         modifications).
      2. `_prune_removed_runbook_documents` -- delete any Runbook_Corpus
         S3 object no longer present locally (reflects removals; see the
         module docstring for why this step is necessary).
      3. `start_and_poll_ingestion_job` -- re-invoke ingestion against
         `--data-source-id` so the Knowledge_Base's queryable content is
         brought back in sync with the bucket, reporting any failed
         document identifiers.

    Prints a summary of what was re-uploaded, what was pruned, and the
    ingestion outcome (including any failed document identifiers).

    Args:
        argv: Optional argument list (defaults to `sys.argv[1:]` via
            `argparse` when omitted). Primarily useful for testing.

    Raises:
        agent.config.CredentialResolutionError: propagated if the
            resolved AWS profile does not resolve to valid credentials;
            no S3 upload/delete or ingestion call is made in that case
            (Requirements 11.5, 11.6).
        FileNotFoundError: propagated from `upload_runbook_corpus` if
            `--runbooks-dir` does not exist.
        scripts.setup_knowledge_base.IngestionJobFailedError: propagated
            if the re-synchronization ingestion job fails/stops entirely
            (as opposed to a partial per-document failure, which is
            reported but does not raise).
        scripts.setup_knowledge_base.IngestionJobPollingTimeoutError:
            propagated if the re-synchronization ingestion job does not
            reach a terminal status within the configured polling
            budget.
    """
    args = _parse_args(argv)

    # Requirement 11.5/11.6: fail closed before any mutating AWS call
    # (the S3 upload/delete below, or ingestion) is made.
    resolved_profile, resolved_region = require_valid_credentials(
        args.profile, args.region
    )

    uploaded_keys = upload_runbook_corpus(
        args.bucket_name,
        runbooks_dir=args.runbooks_dir,
        region=resolved_region,
        profile=resolved_profile,
    )
    print(
        f"Re-uploaded {len(uploaded_keys)} Runbook_Corpus file(s) to "
        f"bucket '{args.bucket_name}' (reflects additions/modifications)."
    )

    pruned_keys = _prune_removed_runbook_documents(
        args.bucket_name,
        uploaded_keys,
        region=resolved_region,
        profile=resolved_profile,
    )
    if pruned_keys:
        print(
            f"Removed {len(pruned_keys)} Runbook_Corpus object(s) from "
            f"bucket '{args.bucket_name}' no longer present in "
            f"'{args.runbooks_dir}' (reflects removals):"
        )
        for key in pruned_keys:
            print(f"  - {key}")
    else:
        print("No locally-removed Runbook_Corpus files detected; nothing pruned.")

    ingestion_result = start_and_poll_ingestion_job(
        args.knowledge_base_id,
        args.data_source_id,
        profile=resolved_profile,
        region=resolved_region,
        poll_interval_seconds=args.poll_interval_seconds,
        max_poll_attempts=args.max_poll_attempts,
    )

    failed_count = ingestion_result["failed_document_count"]
    print(
        "Knowledge Base re-synchronization complete: "
        f"knowledge_base_id='{args.knowledge_base_id}', "
        f"data_source_id='{args.data_source_id}', "
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
