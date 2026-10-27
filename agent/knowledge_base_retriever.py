"""agent/knowledge_base_retriever.py: Strands tool wrapping
`bedrock-agent-runtime:Retrieve` against the managed Knowledge_Base
(Requirements 5.1 and 12.2).

Per the design document's "5. KnowledgeBaseRetriever
(agent/knowledge_base_retriever.py)" component section
(.kiro/specs/network-ops-agent/design.md), this module wraps the
`Retrieve` API using `managedSearchConfiguration` (the Bedrock AgentCore-
managed knowledge base's search configuration, per "Create a managed
knowledge base":
https://docs.aws.amazon.com/bedrock/latest/userguide/kb-managed-create.html),
applies a bounded client timeout (connect=10s, read=10s) so a slow or
unreachable Knowledge_Base cannot hang the orchestrator indefinitely
(Requirement 12.2), and raises one of two classifiable exceptions on
failure so the orchestrator (`agent/network_ops_agent.py`, task 16.1) can
map the failure to `ErrorType.RETRIEVAL_FAILURE` without needing to
inspect raw `botocore` exception types itself:

- `RetrievalTimeoutError`: the connection attempt or the response did not
  complete within the bounded timeout.
- `RetrievalFailureError`: any other failure calling `Retrieve` (access
  denied, invalid/unconfigured knowledge base id, throttling, etc.), or a
  Knowledge_Base id that could not be resolved at all.

Each returned `KBRetrievalItem` carries `text`, `score`, `source_uri`, and
`issue_category` -- the last read from the retrieval result's `metadata`
map, which Amazon Bedrock knowledge bases populate from a `.metadata.json`
sidecar's `metadataAttributes` (see `scripts/generate_runbooks.py`'s
`metadataAttributes.issue_category` sidecar field), so `CaseAnalyzer`
(agent/case_analyzer.py) can compare it against a `CaseRecord`'s
`network_issue_category` by exact string match rather than parsing free
text (Requirement 5.2/5.3).
"""

from __future__ import annotations

import os
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ConnectTimeoutError,
    ReadTimeoutError,
)
from strands import tool

from agent.config import resolve_region
from agent.models import KBRetrievalItem

# Requirement 12.2: "does not respond ... within 10 seconds or the
# connection attempt fails" -> a bounded connect *and* read timeout of
# 10 seconds each on the boto3 client used for `Retrieve`.
RETRIEVE_CONNECT_TIMEOUT_SECONDS = 10
RETRIEVE_READ_TIMEOUT_SECONDS = 10

# The Knowledge_Base id is provisioned by scripts/setup_knowledge_base.py
# (task 7.2, a later wave) and is not yet available from agent/config.py.
# Until/unless a shared config surface for it exists, this module resolves
# it from an explicitly-supplied value or this environment variable, set
# by the deployment/runtime environment (mirrors the
# `resolve_profile`/`resolve_region` explicit-over-default pattern in
# agent/config.py).
KNOWLEDGE_BASE_ID_ENV_VAR = "NETWORK_OPS_KNOWLEDGE_BASE_ID"

# AWS's own default for `retrievalConfiguration.managedSearchConfiguration
# .numberOfResults` is 5; set explicitly here so behavior does not depend
# on an AWS-side default that could change.
DEFAULT_NUMBER_OF_RESULTS = 5


class RetrievalTimeoutError(RuntimeError):
    """Raised when a Knowledge_Base `Retrieve` call does not complete
    within the bounded connect/read timeout (Requirement 12.2).

    Callers (the orchestrator) MUST classify this as a knowledge base
    retrieval failure (`ErrorType.RETRIEVAL_FAILURE`) and MUST NOT produce
    a `Case_Summary`/`Customer_Email` for the request.
    """


class RetrievalFailureError(RuntimeError):
    """Raised when a Knowledge_Base `Retrieve` call fails for a reason
    other than a bounded-timeout expiry (Requirement 12.2): e.g. access
    denied, an invalid/nonexistent knowledge base id, throttling, or the
    knowledge base id could not be resolved at all.

    Callers (the orchestrator) MUST classify this as a knowledge base
    retrieval failure (`ErrorType.RETRIEVAL_FAILURE`) and MUST NOT produce
    a `Case_Summary`/`Customer_Email` for the request.
    """


def _resolve_knowledge_base_id(explicit: str | None = None) -> str:
    """Resolve the Knowledge_Base id to query.

    Honors an explicitly supplied, non-empty value over the
    `NETWORK_OPS_KNOWLEDGE_BASE_ID` environment variable.

    Raises:
        RetrievalFailureError: if no explicit value is supplied and the
            environment variable is unset/empty, since a `Retrieve` call
            cannot be made without a knowledge base id. This is treated as
            a retrieval failure (rather than, say, a `ValueError`) so it
            is classified consistently with every other reason
            `retrieve_runbook_content` can fail to return results.
    """
    if explicit:
        return explicit
    kb_id = os.environ.get(KNOWLEDGE_BASE_ID_ENV_VAR)
    if not kb_id:
        raise RetrievalFailureError(
            "knowledge base id not configured: set the "
            f"{KNOWLEDGE_BASE_ID_ENV_VAR} environment variable or pass "
            "knowledge_base_id explicitly"
        )
    return kb_id


def _build_client(region: str | None = None) -> Any:
    """Construct a `bedrock-agent-runtime` boto3 client with the bounded
    connect/read timeout applied (Requirement 12.2)."""
    resolved_region = resolve_region(region)
    config = Config(
        connect_timeout=RETRIEVE_CONNECT_TIMEOUT_SECONDS,
        read_timeout=RETRIEVE_READ_TIMEOUT_SECONDS,
    )
    return boto3.client(
        "bedrock-agent-runtime", region_name=resolved_region, config=config
    )


def _parse_retrieval_item(result: dict[str, Any]) -> KBRetrievalItem:
    """Parse a single `Retrieve` API `retrievalResults[]` entry into a
    `KBRetrievalItem`."""
    content = result.get("content") or {}
    text = content.get("text", "")

    score = result.get("score", 0.0)

    location = result.get("location") or {}
    s3_location = location.get("s3Location") or {}
    source_uri = s3_location.get("uri", "")

    metadata = result.get("metadata") or {}
    issue_category = metadata.get("issue_category")

    return KBRetrievalItem(
        text=text,
        score=float(score),
        source_uri=source_uri,
        issue_category=issue_category,
    )


def retrieve_runbook_content_impl(
    query: str,
    *,
    knowledge_base_id: str | None = None,
    number_of_results: int = DEFAULT_NUMBER_OF_RESULTS,
    region: str | None = None,
    client: Any = None,
) -> list[KBRetrievalItem]:
    """Testable core of `retrieve_runbook_content`.

    Separated from the `@tool`-decorated wrapper below so unit tests can
    inject a mocked `client` (or override `knowledge_base_id`/`region`)
    without going through the Strands tool-invocation machinery. The
    `@tool`-decorated `retrieve_runbook_content` function delegates to this
    with no overrides, matching the design document's minimal
    `retrieve_runbook_content(query: str) -> list[KBRetrievalItem]`
    signature exposed to the agent/LLM.

    Args:
        query: The retrieval query text (the orchestrator is responsible
            for building this from the case's issue description and
            network issue category per Requirement 5.1).
        knowledge_base_id: Overrides the resolved Knowledge_Base id.
            Defaults to resolving from the `NETWORK_OPS_KNOWLEDGE_BASE_ID`
            environment variable.
        number_of_results: Maximum number of retrieval results requested.
        region: Overrides the resolved AWS Region for the
            `bedrock-agent-runtime` client. Defaults to
            `agent.config.resolve_region`'s default (`us-west-2`).
        client: Overrides the boto3 `bedrock-agent-runtime` client used to
            call `Retrieve`. Defaults to a client built with the bounded
            10s connect/10s read timeout. Intended for test injection.

    Returns:
        A list of `KBRetrievalItem`, one per `retrievalResults[]` entry
        returned by `Retrieve` (possibly empty).

    Raises:
        RetrievalTimeoutError: if the connect/read timeout elapses before
            `Retrieve` completes.
        RetrievalFailureError: if the Knowledge_Base id could not be
            resolved, or if `Retrieve` fails for any other reason
            (access denied, invalid knowledge base id, throttling, etc.).
    """
    resolved_kb_id = _resolve_knowledge_base_id(knowledge_base_id)
    boto_client = client if client is not None else _build_client(region)

    try:
        response = boto_client.retrieve(
            knowledgeBaseId=resolved_kb_id,
            retrievalQuery={"text": query},
            retrievalConfiguration={
                "managedSearchConfiguration": {
                    "numberOfResults": number_of_results,
                }
            },
        )
    except (ConnectTimeoutError, ReadTimeoutError) as exc:
        # NOTE: ConnectTimeoutError/ReadTimeoutError are both subclasses of
        # BotoCoreError, so this except clause MUST be listed before the
        # (ClientError, BotoCoreError) clause below to be reachable.
        raise RetrievalTimeoutError(
            "Knowledge_Base retrieval timed out after "
            f"{RETRIEVE_CONNECT_TIMEOUT_SECONDS}s connect / "
            f"{RETRIEVE_READ_TIMEOUT_SECONDS}s read timeout: {exc}"
        ) from exc
    except (ClientError, BotoCoreError) as exc:
        raise RetrievalFailureError(
            f"Knowledge_Base retrieval failed: {exc}"
        ) from exc

    results = response.get("retrievalResults") or []
    return [_parse_retrieval_item(result) for result in results]


@tool
def retrieve_runbook_content(query: str) -> list[KBRetrievalItem]:
    """Retrieve runbook content relevant to a query from the Knowledge_Base.

    Calls `bedrock-agent-runtime:Retrieve` against the configured
    Knowledge_Base id using `managedSearchConfiguration` (the search
    configuration for an Amazon Bedrock AgentCore-managed knowledge base),
    with a bounded 10-second connect timeout and 10-second read timeout.

    Args:
        query: Free-text retrieval query, typically built from a case's
            issue description and network issue category.

    Returns:
        A list of `KBRetrievalItem`, each with `text`, `score`,
        `source_uri`, and `issue_category` (read from the retrieved
        chunk's `.metadata.json` sidecar metadata, or `None` if absent).
        Empty when the Knowledge_Base has no relevant content.

    Raises:
        RetrievalTimeoutError: the retrieval did not complete within the
            bounded connect/read timeout.
        RetrievalFailureError: the retrieval failed for any other reason
            (including a missing/invalid Knowledge_Base id configuration).
    """
    return retrieve_runbook_content_impl(query)
