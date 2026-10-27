"""tests/integration/test_knowledge_base_ingestion.py

Mocked integration test for `start_and_poll_ingestion_job`
(scripts/setup_knowledge_base.py, task 7.2), covering ingestion polling
with a partial per-document failure (task 7.4).

Validates Requirements 3.3 and 3.4:
  - 3.3: when ingestion of the Runbook_Corpus completes, the
    Knowledge_Base remains queryable for every document that ingested
    successfully, regardless of whether one or more other documents
    failed ingestion -- i.e. a partial per-document failure inside an
    otherwise `COMPLETE` ingestion job is NOT fatal and must not raise.
  - 3.4: if a document fails ingestion, the system reports the failure
    together with the affected document's identifier to the developer
    running the ingestion procedure.

`start_and_poll_ingestion_job` builds its own `boto3.Session` and
`bedrock-agent` client internally rather than accepting an injected
client (see scripts/setup_knowledge_base.py), so this test mocks at the
`boto3.Session` level -- `scripts.setup_knowledge_base.boto3.Session` --
to control what `start_ingestion_job`, `get_ingestion_job`, and
`list_knowledge_base_documents` return, and mocks
`scripts.setup_knowledge_base.time.sleep` so the polling loop's sleeps
between `GetIngestionJob` calls do not slow the test down. No real AWS
call is made.

The simulated polling sequence mirrors a realistic ingestion job
lifecycle:
  1. `StartIngestionJob` returns a freshly started job with status
     `STARTING`.
  2. `GetIngestionJob` is polled three times: `IN_PROGRESS`,
     `IN_PROGRESS`, then `COMPLETE` -- confirming the function actually
     polls repeatedly rather than trusting a single response.
  3. The terminal `COMPLETE` response's `statistics` reports a partial
     failure: 5 documents scanned, 4 indexed, 1 failed.
  4. `ListKnowledgeBaseDocuments` (called only because
     `numberOfDocumentsFailed > 0`) returns 4 `INDEXED` documents and 1
     `FAILED` document, the latter carrying an `identifier.s3.uri`.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from scripts.setup_knowledge_base import start_and_poll_ingestion_job

KNOWLEDGE_BASE_ID = "kb-abc123"
DATA_SOURCE_ID = "ds-xyz789"
INGESTION_JOB_ID = "ingestion-job-001"
FAILED_DOCUMENT_URI = "s3://anytel-runbook-corpus/router-cpe-configuration.md"


def _indexed_document(uri: str) -> dict:
    return {
        "status": "INDEXED",
        "identifier": {"dataSourceType": "S3", "s3": {"uri": uri}},
    }


def _failed_document(uri: str) -> dict:
    return {
        "status": "FAILED",
        "identifier": {"dataSourceType": "S3", "s3": {"uri": uri}},
    }


class TestIngestionPollingWithPartialFailure:
    """Requirement 3.3/3.4: a partial per-document failure within an
    otherwise COMPLETE ingestion job is reported, not raised."""

    @patch("scripts.setup_knowledge_base.time.sleep")
    @patch("scripts.setup_knowledge_base.boto3.Session")
    def test_partial_failure_is_reported_without_raising(
        self, mock_session_cls: MagicMock, mock_sleep: MagicMock
    ) -> None:
        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session
        mock_bedrock_agent_client = MagicMock()
        mock_session.client.return_value = mock_bedrock_agent_client

        # 1. StartIngestionJob: the job has just been started.
        mock_bedrock_agent_client.start_ingestion_job.return_value = {
            "ingestionJob": {
                "ingestionJobId": INGESTION_JOB_ID,
                "status": "STARTING",
            }
        }

        # 2. GetIngestionJob: IN_PROGRESS, IN_PROGRESS, then COMPLETE with
        #    a partial failure recorded in the final response's
        #    statistics (5 scanned, 4 indexed, 1 failed).
        mock_bedrock_agent_client.get_ingestion_job.side_effect = [
            {
                "ingestionJob": {
                    "ingestionJobId": INGESTION_JOB_ID,
                    "status": "IN_PROGRESS",
                }
            },
            {
                "ingestionJob": {
                    "ingestionJobId": INGESTION_JOB_ID,
                    "status": "IN_PROGRESS",
                }
            },
            {
                "ingestionJob": {
                    "ingestionJobId": INGESTION_JOB_ID,
                    "status": "COMPLETE",
                    "statistics": {
                        "numberOfDocumentsScanned": 5,
                        "numberOfDocumentsIndexed": 4,
                        "numberOfDocumentsFailed": 1,
                    },
                }
            },
        ]

        # 3. ListKnowledgeBaseDocuments: 4 successfully indexed documents
        #    plus the 1 failed document, with no further pagination.
        mock_bedrock_agent_client.list_knowledge_base_documents.return_value = {
            "documentDetails": [
                _indexed_document("s3://anytel-runbook-corpus/broadband-outage.md"),
                _indexed_document("s3://anytel-runbook-corpus/mobile-signal.md"),
                _indexed_document(
                    "s3://anytel-runbook-corpus/service-degradation.md"
                ),
                _indexed_document("s3://anytel-runbook-corpus/planned-maintenance.md"),
                _failed_document(FAILED_DOCUMENT_URI),
            ]
        }

        # Requirement 3.3: a partial failure must not be fatal --
        # successfully ingested documents remain queryable regardless of
        # the other document's failure, so this call must not raise.
        result = start_and_poll_ingestion_job(
            KNOWLEDGE_BASE_ID,
            DATA_SOURCE_ID,
            profile="rs-gen-ai",
            region="us-west-2",
            poll_interval_seconds=5,
            max_poll_attempts=60,
        )

        # Requirement 3.4: the failure is reported together with the
        # affected document's identifier.
        assert result["ingestion_job_id"] == INGESTION_JOB_ID
        assert result["status"] == "COMPLETE"
        assert result["failed_document_count"] == 1
        assert result["failed_document_identifiers"] == [FAILED_DOCUMENT_URI]

        # Confirm actual polling occurred (three GetIngestionJob calls),
        # rather than a single call being trusted as the final outcome.
        assert mock_bedrock_agent_client.get_ingestion_job.call_count == 3

        # StartIngestionJob was invoked exactly once, against the
        # expected knowledge base / data source ids.
        mock_bedrock_agent_client.start_ingestion_job.assert_called_once_with(
            knowledgeBaseId=KNOWLEDGE_BASE_ID,
            dataSourceId=DATA_SOURCE_ID,
        )

        # ListKnowledgeBaseDocuments was consulted to resolve the failed
        # document's identifier, scoped to the same knowledge base/data
        # source.
        mock_bedrock_agent_client.list_knowledge_base_documents.assert_called_once_with(
            knowledgeBaseId=KNOWLEDGE_BASE_ID,
            dataSourceId=DATA_SOURCE_ID,
        )

        # Real sleeps between polls were avoided.
        assert mock_sleep.call_count == 3

    @patch("scripts.setup_knowledge_base.time.sleep")
    @patch("scripts.setup_knowledge_base.boto3.Session")
    def test_full_success_reports_zero_failures_without_listing_documents(
        self, mock_session_cls: MagicMock, mock_sleep: MagicMock
    ) -> None:
        """Contrast case: when every document ingests successfully,
        `failed_document_count` is 0, `failed_document_identifiers` is
        empty, and `ListKnowledgeBaseDocuments` is never called (it is
        only needed to resolve failed document identifiers)."""
        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session
        mock_bedrock_agent_client = MagicMock()
        mock_session.client.return_value = mock_bedrock_agent_client

        mock_bedrock_agent_client.start_ingestion_job.return_value = {
            "ingestionJob": {
                "ingestionJobId": INGESTION_JOB_ID,
                "status": "STARTING",
            }
        }
        mock_bedrock_agent_client.get_ingestion_job.return_value = {
            "ingestionJob": {
                "ingestionJobId": INGESTION_JOB_ID,
                "status": "COMPLETE",
                "statistics": {
                    "numberOfDocumentsScanned": 5,
                    "numberOfDocumentsIndexed": 5,
                    "numberOfDocumentsFailed": 0,
                },
            }
        }

        result = start_and_poll_ingestion_job(
            KNOWLEDGE_BASE_ID,
            DATA_SOURCE_ID,
            profile="rs-gen-ai",
            region="us-west-2",
        )

        assert result["failed_document_count"] == 0
        assert result["failed_document_identifiers"] == []
        mock_bedrock_agent_client.list_knowledge_base_documents.assert_not_called()
