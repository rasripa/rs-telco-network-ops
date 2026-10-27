"""ui/agent_client.py: invokes the deployed Network_Ops_Agent AgentCore
Runtime and returns a typed `AnalysisResult` (Requirements 9.2, 12.1, 12.2,
12.3).

Per the design document's "9. Streamlit_UI (`ui/app.py`, `ui/agent_client.py`)"
component section (.kiro/specs/network-ops-agent/design.md):

    `agent_client.invoke(case_id)` calls `bedrock-agentcore:InvokeAgentRuntime`
    (data plane) against the deployed runtime ARN, with a per-request
    timeout, and returns a typed `AnalysisResult` (mirroring the agent-side
    type) to the UI layer.

`runtime_entry.py` (task 16.3, `agent/runtime_entry.py`) wraps `analyze_case`
in a `bedrock_agentcore.runtime.BedrockAgentCoreApp` entrypoint that accepts
`{"case_id": "..."}` and returns the JSON-serialized `AnalysisResult`. This
module is that entrypoint's client-side counterpart: it builds the same
`{"case_id": ...}` payload, invokes the runtime, and deserializes the
response body back into the shared `agent.models.AnalysisResult` dataclass
(and its nested `CaseSummary`/`CustomerEmail`/`ErrorType`), using the
dataclass field names verbatim as the expected JSON key names -- the natural
shape produced by `dataclasses.asdict()` on the agent side.

Failure isolation (Requirements 12.1-12.3): this module never raises for a
request-level failure. A connect/read timeout, a connection error, a
non-2xx `statusCode` in the response, or a response body that cannot be
parsed back into an `AnalysisResult` are all caught and converted into a
failure `AnalysisResult` instead. Such failures are classified as
`ErrorType.ANALYSIS_FAILURE`: from the UI's vantage point, a single opaque
`InvokeAgentRuntime` call either succeeds (in which case the *deployed
agent* has already classified any internal failure as `LOOKUP_FAILURE`
(12.1) or `RETRIEVAL_FAILURE` (12.2) inside the returned payload, which is
deserialized and returned unchanged) or it fails to complete at all -- and
the UI has no visibility into which agent-internal stage was in flight when
that happened. Requirement 12.3 already reserves `ANALYSIS_FAILURE` for
exactly this "not a case lookup failure or a knowledge base retrieval
failure" bucket, so a request-level failure at this client boundary is
classified the same way.

No AWS named profile is selected in code here, mirroring
`agent/knowledge_base_retriever.py` and `mcp_server/dynamodb_gateway.py`:
this lets the module work unchanged whether it is run locally (picking up
whatever profile/credentials are configured in the calling environment,
e.g. `AWS_PROFILE=rs-gen-ai`) or from an execution context with its own
credential chain. Only the AWS Region is resolved via
`agent.config.resolve_region` (defaulting to `us-west-2`), consistent with
the rest of the codebase.
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from agent.config import resolve_region
from agent.models import AnalysisResult, CaseSummary, CustomerEmail, ErrorType

# Requirement 12.1/12.2 bound the agent's *internal* case-lookup and
# knowledge-base-retrieval stages at 10s connect / 10s read each. This
# module's `InvokeAgentRuntime` call spans the agent's *entire*
# orchestration for a request (case lookup -> knowledge base retrieval ->
# LLM-assisted email composition -> content guard, with up to one guard
# retry per design.md's EmailComposer/ContentGuard section), so its
# per-request read timeout is set generously above the sum of those two
# 10s internal stage bounds to avoid the UI timing out a request that the
# agent itself would have completed successfully. design.md does not call
# out a specific timeout value for this client-facing data-plane call, so
# a documented default is used here (per task 18.1's guidance) and can be
# overridden by the caller.
DEFAULT_CONNECT_TIMEOUT_SECONDS = 10
DEFAULT_READ_TIMEOUT_SECONDS = 30

# Automatic botocore retries are disabled (max_attempts=1) for this call.
# AgentCore Runtime sessions are stateful; silently retrying a timed-out
# or failed invocation could route to a different microVM session or
# double-invoke the agent's side effects, and the UI's own "processing"
# spinner (Requirement 9.3) should reflect a single bounded wait rather
# than an invisible multi-attempt retry loop. A request-level failure is
# instead surfaced immediately as a failure `AnalysisResult`, which the
# support engineer can act on (e.g. resubmit) per Requirement 12's
# failure-isolation intent.
_INVOKE_RETRY_CONFIG = {"max_attempts": 1}

# The deployed runtime ARN is wired into this module in a later task
# (20.2, "Configure `ui/agent_client.py` with the deployed runtime ARN").
# Until then, `invoke()` resolves it from an explicitly supplied value or
# this environment variable -- mirroring the explicit-value-over-
# environment-variable pattern already used by
# `agent/knowledge_base_retriever.py`'s `NETWORK_OPS_KNOWLEDGE_BASE_ID`.
RUNTIME_ARN_ENV_VAR = "NETWORK_OPS_AGENT_RUNTIME_ARN"

# Session id prefix so generated ids are self-describing in AgentCore
# Runtime logs/traces. `InvokeAgentRuntime`'s `runtimeSessionId` has a
# 33-256 character length constraint; a UUID4 alone is 36 characters, so
# this prefixed form comfortably satisfies that bound.
_SESSION_ID_PREFIX = "network-ops-ui-"


class AgentClientConfigurationError(RuntimeError):
    """Raised internally when the agent runtime ARN could not be resolved.

    `invoke()` catches this itself and converts it into a failure
    `AnalysisResult` (see module docstring); it is exposed publicly only
    so callers that want to fail fast at startup (e.g. a future wiring
    script) can resolve/validate configuration ahead of time if desired.
    """


def _resolve_runtime_arn(explicit: str | None = None) -> str:
    """Resolve the AgentCore Runtime ARN to invoke.

    Honors an explicitly supplied, non-empty value over the
    `NETWORK_OPS_AGENT_RUNTIME_ARN` environment variable.

    Raises:
        AgentClientConfigurationError: if no explicit value is supplied
            and the environment variable is unset/empty. The deployed
            runtime ARN is wired in task 20.2; until then this is the
            expected failure mode when `invoke()` is called without a
            configured runtime.
    """
    if explicit:
        return explicit
    arn = os.environ.get(RUNTIME_ARN_ENV_VAR)
    if not arn:
        raise AgentClientConfigurationError(
            "agent runtime ARN not configured: set the "
            f"{RUNTIME_ARN_ENV_VAR} environment variable or pass "
            "runtime_arn explicitly (the deployed runtime ARN is wired "
            "into the UI in a later deployment task)"
        )
    return arn


def _build_client(
    region: str | None,
    connect_timeout: float,
    read_timeout: float,
) -> Any:
    """Construct a `bedrock-agentcore` boto3 client with the bounded
    connect/read timeout and no automatic retries applied."""
    resolved_region = resolve_region(region)
    config = Config(
        connect_timeout=connect_timeout,
        read_timeout=read_timeout,
        retries=_INVOKE_RETRY_CONFIG,
    )
    return boto3.client(
        "bedrock-agentcore", region_name=resolved_region, config=config
    )


def _generate_session_id() -> str:
    """Generate a fresh `runtimeSessionId` for a single analysis request.

    Each `invoke()` call analyzes exactly one `case_id` and does not need
    to maintain multi-turn conversational context with the agent, so a
    new session id is generated per call unless the caller explicitly
    supplies one (e.g. for testing or deliberate session reuse).
    """
    return f"{_SESSION_ID_PREFIX}{uuid.uuid4()}"


def _read_response_body(response_body: Any) -> bytes:
    """Read the full byte content of an `InvokeAgentRuntime` response's
    `response` field.

    The real boto3 client returns a `botocore.response.StreamingBody`
    (which exposes `.read()`); a mocked client used for testing may
    instead supply a plain `bytes`/`bytearray` object directly, or any
    other iterable of `bytes` chunks (matching the pattern used in AWS's
    own documented `InvokeAgentRuntime` example). All three shapes are
    handled here so `invoke()` works against any of them.
    """
    if response_body is None:
        return b""
    if isinstance(response_body, (bytes, bytearray)):
        return bytes(response_body)
    if hasattr(response_body, "read"):
        return response_body.read()
    return b"".join(response_body)


def _error_type_from_value(value: Any) -> ErrorType | None:
    """Deserialize an `error_type` JSON value back into an `ErrorType`."""
    if value is None:
        return None
    if isinstance(value, ErrorType):
        return value
    return ErrorType(value)


def _case_summary_from_dict(data: dict[str, Any] | None) -> CaseSummary | None:
    """Deserialize a `case_summary` JSON object into a `CaseSummary`."""
    if data is None:
        return None
    return CaseSummary(
        case_id=data["case_id"],
        customer_name=data["customer_name"],
        issue_description=data["issue_description"],
        validity=data["validity"],
        rationale=data["rationale"],
        root_cause=data.get("root_cause"),
        fix_steps=data.get("fix_steps"),
        runbook_reference=data.get("runbook_reference"),
    )


def _customer_email_from_dict(data: dict[str, Any] | None) -> CustomerEmail | None:
    """Deserialize a `customer_email` JSON object into a `CustomerEmail`."""
    if data is None:
        return None
    return CustomerEmail(
        salutation=data["salutation"],
        body=data["body"],
        signature=data["signature"],
        full_text=data["full_text"],
    )


def _analysis_result_from_dict(data: dict[str, Any]) -> AnalysisResult:
    """Deserialize a full `AnalysisResult` JSON object.

    Mirrors the shape `runtime_entry.py` (task 16.3) is expected to
    serialize: `success`, `case_summary`, `customer_email`, `error_type`,
    and `error_message`, using the dataclass field names verbatim as the
    JSON keys.
    """
    return AnalysisResult(
        success=bool(data["success"]),
        case_summary=_case_summary_from_dict(data.get("case_summary")),
        customer_email=_customer_email_from_dict(data.get("customer_email")),
        error_type=_error_type_from_value(data.get("error_type")),
        error_message=data.get("error_message"),
    )


def _analysis_failure(message: str) -> AnalysisResult:
    """Build a failure `AnalysisResult` for a request-level agent_client
    failure, classified as `ErrorType.ANALYSIS_FAILURE` (see module
    docstring for the rationale)."""
    return AnalysisResult(
        success=False,
        case_summary=None,
        customer_email=None,
        error_type=ErrorType.ANALYSIS_FAILURE,
        error_message=message,
    )


def invoke(
    case_id: str,
    *,
    runtime_arn: str | None = None,
    region: str | None = None,
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
    read_timeout: float = DEFAULT_READ_TIMEOUT_SECONDS,
    runtime_session_id: str | None = None,
    client: Any = None,
) -> AnalysisResult:
    """Invoke the deployed Network_Ops_Agent for `case_id` and return a
    typed `AnalysisResult` (Requirement 9.2).

    Calls `bedrock-agentcore:InvokeAgentRuntime` against the configured
    runtime ARN with `{"case_id": case_id}` as the JSON payload, applies a
    bounded per-request connect/read timeout, and deserializes the
    response payload back into an `AnalysisResult`.

    This function never raises for a request-level failure (timeout,
    connection error, non-2xx `statusCode`, or an unparseable response
    body): each is caught and converted into a failure `AnalysisResult`
    with `error_type=ErrorType.ANALYSIS_FAILURE` instead (Requirements
    12.1-12.3; see module docstring for why `ANALYSIS_FAILURE` is used at
    this boundary rather than `LOOKUP_FAILURE`/`RETRIEVAL_FAILURE`). When
    the runtime call itself succeeds, the agent's own already-classified
    `AnalysisResult` (which may itself carry `LOOKUP_FAILURE`,
    `RETRIEVAL_FAILURE`, `CASE_NOT_FOUND`, etc.) is deserialized and
    returned unchanged.

    Args:
        case_id: The case_id to analyze, sent as the invocation payload.
        runtime_arn: Overrides the resolved AgentCore Runtime ARN.
            Defaults to resolving from the `NETWORK_OPS_AGENT_RUNTIME_ARN`
            environment variable.
        region: Overrides the resolved AWS Region for the
            `bedrock-agentcore` client. Defaults to
            `agent.config.resolve_region`'s default (`us-west-2`).
        connect_timeout: Bounded connect timeout in seconds for the
            `InvokeAgentRuntime` call. Defaults to 10s.
        read_timeout: Bounded read timeout in seconds for the
            `InvokeAgentRuntime` call. Defaults to 30s (see the
            `DEFAULT_READ_TIMEOUT_SECONDS` module constant for the
            rationale).
        runtime_session_id: Overrides the generated `runtimeSessionId`.
            Defaults to a freshly generated session id per call.
        client: Overrides the boto3 `bedrock-agentcore` client used to
            call `InvokeAgentRuntime`. Defaults to a client built with
            the bounded connect/read timeout and no automatic retries.
            Intended for test injection (see this module's manual
            verification notes / tests/unit for examples).

    Returns:
        A typed `AnalysisResult`: either the agent's own successful or
        already-classified-failure result (deserialized from the
        response payload), or a client-level failure `AnalysisResult`
        with `error_type=ErrorType.ANALYSIS_FAILURE` when the request
        itself could not be completed or its response could not be
        parsed.
    """
    try:
        resolved_arn = _resolve_runtime_arn(runtime_arn)
        boto_client = (
            client
            if client is not None
            else _build_client(region, connect_timeout, read_timeout)
        )
        session_id = runtime_session_id or _generate_session_id()
        payload = json.dumps({"case_id": case_id}).encode("utf-8")

        response = boto_client.invoke_agent_runtime(
            agentRuntimeArn=resolved_arn,
            runtimeSessionId=session_id,
            payload=payload,
        )

        status_code = response.get("statusCode")
        if status_code is not None and not (200 <= status_code < 300):
            return _analysis_failure(
                "agent runtime request failed: received non-2xx status "
                f"code {status_code}"
            )

        raw_body = _read_response_body(response.get("response"))
        text_body = raw_body.decode("utf-8")
        data = json.loads(text_body)
        return _analysis_result_from_dict(data)

    except AgentClientConfigurationError as exc:
        return _analysis_failure(str(exc))
    except (ClientError, BotoCoreError) as exc:
        # Covers connect/read timeouts (ConnectTimeoutError,
        # ReadTimeoutError -- both BotoCoreError subclasses),
        # connection failures, and any other boto3/botocore-raised
        # service or transport error.
        return _analysis_failure(f"agent runtime request failed: {exc}")
    except (json.JSONDecodeError, UnicodeDecodeError, KeyError, TypeError, ValueError) as exc:
        return _analysis_failure(
            f"agent runtime returned an unparseable response: {exc}"
        )
