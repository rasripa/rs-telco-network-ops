"""tests/integration/test_knowledge_base_sync.py

Mocked integration test for `scripts/sync_knowledge_base.py`'s
re-synchronization flow (task 7.3, `main`), covering task 7.5.

Validates Requirement 3.5: while the Runbook_Corpus has been updated
since the most recent ingestion, running the re-synchronization
procedure re-synchronizes the Knowledge_Base data source such that the
Knowledge_Base's queryable content includes each newly added document,
reflects the current content of each modified document, and excludes
each removed document -- by re-invoking ingestion against the *existing*
data source (`--data-source-id`) rather than creating a new one.

`sync_knowledge_base.main()` reuses two functions from
`scripts/setup_knowledge_base.py` (`upload_runbook_corpus` and
`start_and_poll_ingestion_job`), both of which build their own
`boto3.Session`/clients internally, and calls its own module-local
`_prune_removed_runbook_documents`, which likewise builds its own
`boto3.Session`/S3 client. Consistent with
`tests/integration/test_knowledge_base_ingestion.py`'s approach for
`setup_knowledge_base.py`, this test mocks at the `boto3.Session` level
rather than injecting fake clients.

Both `scripts.setup_knowledge_base` and `scripts.sync_knowledge_base` do
`import boto3` at module scope and reference `boto3.Session` at call
time, so `scripts.setup_knowledge_base.boto3` and
`scripts.sync_knowledge_base.boto3` are the *same* module object --
patching `Session` on one dotted path patches the identical attribute
reached via the other path. Patching both paths independently would
therefore silently overwrite one mock with the other (whichever `patch`
decorator is entered last "wins"), rather than producing two distinct
session mocks. To avoid that footgun while still patching at the
`boto3.Session` level for both call sites as intended:

  - A single `scripts.setup_knowledge_base.boto3.Session` patch stands
    in for the shared `boto3.Session` constructor used by *all three*
    call sites (`upload_runbook_corpus`'s S3 client,
    `start_and_poll_ingestion_job`'s `bedrock-agent` client, and
    `_prune_removed_runbook_documents`'s own S3 client in
    `scripts.sync_knowledge_base`) -- reflecting that they resolve to
    the exact same underlying attribute at runtime regardless of which
    module's dotted path is used to reach it.
  - The mocked session's `.client(service_name, ...)` is a dispatcher
    that returns a dedicated mock S3 client for `"s3"` (shared by both
    `upload_runbook_corpus` and `_prune_removed_runbook_documents`,
    matching their real-world identical `session.client("s3", ...)`
    call shape) and a dedicated mock `bedrock-agent` client for
    `"bedrock-agent"` (used by `start_and_poll_ingestion_job`).
  - `scripts.sync_knowledge_base.require_valid_credentials` is patched
    so the fail-closed credential check (Requirements 11.5/11.6) never
    attempts a real `sts:GetCallerIdentity` call.

No real AWS call is made.

Scenario -- added, modified, and removed documents:

  - `mobile_signal_coverage.md` (+ its `.metadata.json` sidecar) is
    present in the local `runbooks/` directory used by this test AND
    already exists in the mocked S3 bucket listing: it represents a
    *modified* document (re-uploading it, keyed by the same filename,
    is what causes Bedrock's next ingestion to pick up its new
    content).
  - `new_fiber_rollout_issue.md` (+ sidecar) is present locally but does
    NOT appear in the mocked S3 bucket listing: it represents an
    *added* document.
  - `legacy_isdn_outage.md` appears in the mocked S3 bucket listing but
    has no corresponding file in the local `runbooks/` directory used by
    this test: it represents a document *removed* from the
    Runbook_Corpus since the prior sync, and must be pruned from S3
    (`delete_objects`) before ingestion re-runs so Bedrock's own
    change detection (which only observes S3, not the local
    filesystem) has something to remove.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from scripts.sync_knowledge_base import main

BUCKET_NAME = "test-bucket"
KNOWLEDGE_BASE_ID = "kb-123"
DATA_SOURCE_ID = "ds-456"
INGESTION_JOB_ID = "ingestion-job-resync-001"

# The S3 key of a Runbook_Corpus document left over from a prior sync
# that is no longer present in the local runbooks/ directory used by
# this test -- simulates a "removed" document (Requirement 3.5).
REMOVED_DOCUMENT_KEY = "legacy_isdn_outage.md"


def _write_runbook_pair(directory: Path, slug: str, body: str, category: str) -> None:
    """Write a `<slug>.md` Runbook_Corpus document and its
    `<slug>.md.metadata.json` sidecar into `directory`, mirroring
    `scripts/generate_runbooks.py`'s writer output shape closely enough
    for this test's purposes."""
    (directory / f"{slug}.md").write_text(body, encoding="utf-8")
    (directory / f"{slug}.md.metadata.json").write_text(
        json.dumps({"metadataAttributes": {"issue_category": category}}),
        encoding="utf-8",
    )


class TestKnowledgeBaseResynchronization:
    """Requirement 3.5: added, modified, and removed Runbook_Corpus files
    are all reflected after re-sync, via re-invocation of ingestion
    against the existing data source."""

    @patch("scripts.sync_knowledge_base.require_valid_credentials")
    @patch("scripts.setup_knowledge_base.time.sleep")
    @patch("scripts.setup_knowledge_base.boto3.Session")
    def test_resync_reflects_added_modified_and_removed_documents(
        self,
        mock_session_cls: MagicMock,
        mock_setup_sleep: MagicMock,
        mock_require_valid_credentials: MagicMock,
    ) -> None:
        mock_require_valid_credentials.return_value = ("rs-gen-ai", "us-west-2")

        # A single boto3.Session mock backs every call site: both
        # scripts.setup_knowledge_base.boto3.Session and
        # scripts.sync_knowledge_base.boto3.Session resolve to this same
        # patched constructor (see module docstring). The mocked
        # session's .client(...) dispatches by service_name so the "s3"
        # client (shared by upload_runbook_corpus and
        # _prune_removed_runbook_documents) and the "bedrock-agent"
        # client (used by start_and_poll_ingestion_job) are distinct
        # mocks, matching their real, independently-callable API surfaces.
        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session
        mock_s3_client = MagicMock()
        mock_bedrock_agent_client = MagicMock()

        def _client_side_effect(service_name: str, region_name=None):
            if service_name == "s3":
                return mock_s3_client
            if service_name == "bedrock-agent":
                return mock_bedrock_agent_client
            raise AssertionError(f"unexpected client service_name: {service_name!r}")

        mock_session.client.side_effect = _client_side_effect

        # Immediate COMPLETE status with zero failed documents: the
        # while-loop's terminal-status check is satisfied by
        # start_ingestion_job's own response, so get_ingestion_job is
        # never polled.
        mock_bedrock_agent_client.start_ingestion_job.return_value = {
            "ingestionJob": {
                "ingestionJobId": INGESTION_JOB_ID,
                "status": "COMPLETE",
                "statistics": {
                    "numberOfDocumentsScanned": 4,
                    "numberOfDocumentsIndexed": 4,
                    "numberOfDocumentsFailed": 0,
                },
            }
        }

        # Bucket listing from a prior sync: mobile_signal_coverage.* is
        # still there (will be re-uploaded as a "modification"), and
        # legacy_isdn_outage.md is there but will NOT be part of the
        # current local runbooks/ directory below -- it is stale and
        # must be pruned.
        mock_s3_client.list_objects_v2.return_value = {
            "Contents": [
                {"Key": "mobile_signal_coverage.md"},
                {"Key": "mobile_signal_coverage.md.metadata.json"},
                {"Key": REMOVED_DOCUMENT_KEY},
            ],
            "IsTruncated": False,
        }

        with tempfile.TemporaryDirectory() as tmp_dir:
            runbooks_dir = Path(tmp_dir) / "runbooks"
            runbooks_dir.mkdir()

            # Modified document: same filename as an S3 object that
            # already exists (per the mocked listing above), but with
            # locally-updated content.
            _write_runbook_pair(
                runbooks_dir,
                "mobile_signal_coverage",
                (
                    "# Mobile Signal / Coverage Issues (AnyTel)\n\n"
                    "Updated diagnostic guidance reflecting the latest "
                    "AnyTel radio access network rollout.\n"
                ),
                category="mobile_signal_coverage",
            )

            # Added document: not present at all in the mocked bucket
            # listing above.
            _write_runbook_pair(
                runbooks_dir,
                "new_fiber_rollout_issue",
                (
                    "# Fiber Rollout Connectivity Issues (AnyTel)\n\n"
                    "Newly authored runbook covering AnyTel's fiber "
                    "rollout connectivity issues.\n"
                ),
                category="fiber_rollout_issue",
            )

            main(
                [
                    "--bucket-name",
                    BUCKET_NAME,
                    "--knowledge-base-id",
                    KNOWLEDGE_BASE_ID,
                    "--data-source-id",
                    DATA_SOURCE_ID,
                    "--runbooks-dir",
                    str(runbooks_dir),
                ]
            )

        # --- Added/modified documents are re-uploaded --------------------
        # upload_runbook_corpus uploads every current local Runbook_Corpus
        # file (both the modified and the added document, plus their
        # sidecars): 4 files total.
        assert mock_s3_client.upload_file.call_count == 4
        uploaded_keys = {
            call_args.args[2] for call_args in mock_s3_client.upload_file.call_args_list
        }
        assert uploaded_keys == {
            "mobile_signal_coverage.md",
            "mobile_signal_coverage.md.metadata.json",
            "new_fiber_rollout_issue.md",
            "new_fiber_rollout_issue.md.metadata.json",
        }
        for call_args in mock_s3_client.upload_file.call_args_list:
            # Each upload_file(local_path, bucket_name, key) call targets
            # the bucket supplied via --bucket-name.
            assert call_args.args[1] == BUCKET_NAME

        # --- Removed document is pruned from S3 --------------------------
        mock_s3_client.list_objects_v2.assert_called_once_with(Bucket=BUCKET_NAME)
        mock_s3_client.delete_objects.assert_called_once_with(
            Bucket=BUCKET_NAME,
            Delete={"Objects": [{"Key": REMOVED_DOCUMENT_KEY}]},
        )

        # --- Ingestion is re-invoked against the EXISTING data source ----
        # (confirms re-synchronization, not creation of a new data source).
        mock_bedrock_agent_client.start_ingestion_job.assert_called_once_with(
            knowledgeBaseId=KNOWLEDGE_BASE_ID,
            dataSourceId=DATA_SOURCE_ID,
        )
        # COMPLETE with zero failures resolved immediately: no polling
        # via get_ingestion_job was needed.
        mock_bedrock_agent_client.get_ingestion_job.assert_not_called()
