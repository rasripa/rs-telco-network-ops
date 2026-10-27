"""Unit tests for agent/knowledge_base_retriever.py timeout/connection
failure classification.

Validates Requirement 12.2: "IF the Knowledge_Base does not respond to a
retrieval query within 10 seconds or the connection attempt fails, THEN
THE Network_Ops_Agent SHALL return to the Streamlit_UI an error result
indicating a knowledge base retrieval failure, and SHALL NOT produce a
Case_Summary or Customer_Email for that request."

These tests exercise `retrieve_runbook_content_impl` (the testable core
that accepts an injected `client`) with a mocked `bedrock-agent-runtime`
client whose `.retrieve(...)` raises the botocore exceptions that
`_build_client`'s bounded connect/read timeout (Requirement 12.2) and
general connection failures would surface, asserting each is classified
into the expected `RetrievalTimeoutError` / `RetrievalFailureError` type
per the module's documented contract.

Plain unit tests using `unittest.mock`; no property-based (Hypothesis)
tests live here.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from botocore.exceptions import (
    ClientError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)

from agent.knowledge_base_retriever import (
    KNOWLEDGE_BASE_ID_ENV_VAR,
    RetrievalFailureError,
    RetrievalTimeoutError,
    retrieve_runbook_content_impl,
)

ENDPOINT_URL = "https://bedrock-agent-runtime.us-west-2.amazonaws.com"


class TestTimeoutClassification:
    """A `Retrieve` call that does not complete within the bounded
    connect/read timeout must raise `RetrievalTimeoutError`."""

    def test_connect_timeout_raises_retrieval_timeout_error(self) -> None:
        """A connection-attempt timeout (ConnectTimeoutError) must be
        classified as RetrievalTimeoutError."""
        mock_client = MagicMock()
        mock_client.retrieve.side_effect = ConnectTimeoutError(
            endpoint_url=ENDPOINT_URL
        )

        with pytest.raises(RetrievalTimeoutError):
            retrieve_runbook_content_impl(
                "broadband outage",
                knowledge_base_id="kb-123",
                client=mock_client,
            )

    def test_read_timeout_raises_retrieval_timeout_error(self) -> None:
        """A response-read timeout (ReadTimeoutError) must be classified
        as RetrievalTimeoutError."""
        mock_client = MagicMock()
        mock_client.retrieve.side_effect = ReadTimeoutError(
            endpoint_url=ENDPOINT_URL
        )

        with pytest.raises(RetrievalTimeoutError):
            retrieve_runbook_content_impl(
                "broadband outage",
                knowledge_base_id="kb-123",
                client=mock_client,
            )


class TestConnectionFailureClassification:
    """A `Retrieve` call that fails for a reason other than a bounded
    timeout (connection failure, access denied, etc.) must raise
    `RetrievalFailureError`."""

    def test_endpoint_connection_error_raises_retrieval_failure_error(
        self,
    ) -> None:
        """A failed connection attempt (EndpointConnectionError, a
        BotoCoreError subclass distinct from the timeout errors) must be
        classified as RetrievalFailureError."""
        mock_client = MagicMock()
        mock_client.retrieve.side_effect = EndpointConnectionError(
            endpoint_url=ENDPOINT_URL
        )

        with pytest.raises(RetrievalFailureError):
            retrieve_runbook_content_impl(
                "broadband outage",
                knowledge_base_id="kb-123",
                client=mock_client,
            )

    def test_client_error_raises_retrieval_failure_error(self) -> None:
        """A service-side ClientError (e.g. AccessDeniedException) must be
        classified as RetrievalFailureError."""
        mock_client = MagicMock()
        mock_client.retrieve.side_effect = ClientError(
            error_response={
                "Error": {
                    "Code": "AccessDeniedException",
                    "Message": "User is not authorized to perform: "
                    "bedrock-agent-runtime:Retrieve",
                }
            },
            operation_name="Retrieve",
        )

        with pytest.raises(RetrievalFailureError):
            retrieve_runbook_content_impl(
                "broadband outage",
                knowledge_base_id="kb-123",
                client=mock_client,
            )


class TestMissingKnowledgeBaseId:
    """A `Retrieve` call cannot be made without a resolvable Knowledge_Base
    id; this must also be classified as RetrievalFailureError so it is
    handled consistently with every other retrieval failure reason."""

    def test_missing_knowledge_base_id_raises_retrieval_failure_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No explicit knowledge_base_id and no environment variable set
        must raise RetrievalFailureError without ever invoking the
        client."""
        monkeypatch.delenv(KNOWLEDGE_BASE_ID_ENV_VAR, raising=False)
        mock_client = MagicMock()

        with pytest.raises(RetrievalFailureError):
            retrieve_runbook_content_impl(
                "broadband outage",
                client=mock_client,
            )

        mock_client.retrieve.assert_not_called()
