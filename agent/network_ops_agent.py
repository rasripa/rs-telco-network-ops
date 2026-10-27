"""agent/network_ops_agent.py: Network_Ops_Agent orchestration (`analyze_case`).

Per the design document's "8. Network_Ops_Agent orchestration
(agent/network_ops_agent.py, agent/runtime_entry.py)" component section
(.kiro/specs/network-ops-agent/design.md), this module implements:

    def analyze_case(case_id: str) -> AnalysisResult:
        '''Orchestrates: MCP get_case -> (not-found/invalid-input short
        circuit) -> retrieve_runbook_content -> CaseAnalyzer -> EmailComposer
        -> ContentGuard -> AnalysisSuccess(case_summary, customer_email) |
        AnalysisError(error_type, message). Any exception not already
        classified is caught and mapped to AnalysisError(ANALYSIS_FAILURE,
        ...) so the caller never observes a raw traceback or a partial
        Case_Summary/Customer_Email.'''

This exactly mirrors the "Runtime request flow" sequence diagram
(design.md): the Network_Ops_Agent calls the Case_Lookup_MCP_Server's
`get_case` tool, short-circuits on `INVALID_INPUT`/`NOT_FOUND`, otherwise
calls `retrieve_runbook_content`, runs the deterministic `CaseAnalyzer`
(`determine_validity` + `build_case_summary`), composes and content-guards
a `CustomerEmail` (retrying composition once with a stricter prompt on
guard failure), and returns a single `AnalysisResult`.

Wrapping `analyze_case` in a `bedrock_agentcore.runtime.BedrockAgentCoreApp`
entrypoint is `agent/runtime_entry.py`'s job (task 16.3, out of scope here).

Dependency injection for testability
-------------------------------------
Per the design's embedded-MCP-server decision, the Case_Lookup_MCP_Server
is a stdio subprocess launched via the Strands `MCPClient`. Rather than
requiring every test to construct a fake `MCPClient`/`MCPToolResult`
shape, `analyze_case` exposes the MCP interaction and the knowledge-base
retrieval as two plain, directly-fault-injectable callables:

  - `lookup_case: Callable[[str], CaseLookupResult] | None` -- defaults to
    `_default_lookup_case`, which connects to
    `mcp_server/case_lookup_server.py` as a stdio subprocess (via a
    Strands `MCPClient`, per the design's "embedded MCP server over
    stdio" decision) and calls its `get_case` tool with a bounded 10
    second timeout (Requirement 12.1; matching the Knowledge_Base
    retriever's own bounded 10s connect/read timeout pattern in
    `agent/knowledge_base_retriever.py`, since design.md does not specify
    a different bound for this call). Raises on any failure to complete
    (subprocess launch failure, MCP transport/protocol error, a
    tool-level error surfaced by the MCP result, or an unparseable
    result); `analyze_case` classifies any such failure as
    `ErrorType.LOOKUP_FAILURE`.

    Each call to the default builds and tears down its own MCP
    subprocess (via `MCPClient`'s context manager) rather than reusing a
    persistent one across requests. This keeps `analyze_case` itself
    fully self-contained and independently callable/testable without any
    setup step; a persistent, agent-lifetime MCP subprocess (as
    design.md's "launched as a local stdio subprocess when the agent
    starts" language suggests for the deployed runtime) can be supplied
    instead by a caller (e.g. `agent/runtime_entry.py`, task 16.3)
    passing its own `lookup_case` callable bound to an already-running
    client.

  - `retrieve_content: Callable[[str], list[KBRetrievalItem]] | None` --
    defaults to `agent.knowledge_base_retriever.retrieve_runbook_content`.
    Raises `RetrievalTimeoutError`/`RetrievalFailureError` on failure
    (already implemented in that module per Requirement 12.2);
    `analyze_case` classifies either as `ErrorType.RETRIEVAL_FAILURE`.

  - `llm_agent` -- passed straight through to
    `agent.email_composer.compose_customer_email` (its own optional
    `llm_agent` parameter), so a stub LLM agent can be injected exactly
    as it already can be for that module's own tests. When omitted, both
    the initial composition attempt and (if needed) the stricter-prompt
    retry each construct their own default Bedrock-backed `Agent`
    (`email_composer._default_agent()`), so no Bedrock configuration or
    credentials are required merely to call `analyze_case` with fully
    injected fault-simulating dependencies.

Content-guard retry without modifying `email_composer.py`
-----------------------------------------------------------
Per design.md: "If `validate_email_content` fails, the orchestrator
retries composition once with a stricter prompt reminder; if it still
fails, the request is returned as an `ANALYSIS_FAILURE` error." Rather
than adding an extra parameter to `email_composer.compose_customer_email`
(whose public interface is already covered by Property 11's tests),
this module implements the "stricter prompt" retry by wrapping whatever
`llm_agent` would otherwise have been used (the caller-supplied stub, or
a freshly constructed default `Agent`) in `_StricterPromptAgent`: a thin
callable adapter that appends a stricter compliance reminder to the
*same* prompt `compose_customer_email`/`_build_email_prompt` already
builds, before delegating to the underlying agent. This changes nothing
about `email_composer.py` and is invisible to it -- from its perspective
it is simply calling a (slightly different) `llm_agent`.
"""

from __future__ import annotations

import sys
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable, Mapping

from mcp import StdioServerParameters, stdio_client
from strands.tools.mcp import MCPClient

from agent import email_composer
from agent.case_analyzer import build_case_summary, determine_validity
from agent.content_guard import validate_email_content
from agent.email_composer import compose_customer_email
from agent.knowledge_base_retriever import (
    RetrievalFailureError,
    RetrievalTimeoutError,
    retrieve_runbook_content,
)
from agent.models import (
    AnalysisResult,
    CaseLookupResult,
    CaseRecord,
    CustomerEmail,
    ErrorType,
    KBRetrievalItem,
    LookupResultType,
)

# Requirement 12.1: "does not respond to a case lookup request within 10
# seconds or the connection attempt fails" -> a bounded 10 second timeout
# on the MCP `get_case` tool call, matching the Knowledge_Base
# retriever's own bounded 10s connect/read timeout pattern
# (agent/knowledge_base_retriever.py's RETRIEVE_CONNECT_TIMEOUT_SECONDS /
# RETRIEVE_READ_TIMEOUT_SECONDS), since design.md does not specify a
# distinct value for this call. Used both as the MCP tool call's
# read-timeout bound and as the MCP subprocess's startup timeout bound
# (the subprocess launch + protocol handshake must also complete
# reasonably quickly for a request to be considered timely).
LOOKUP_TIMEOUT_SECONDS = 10

# Module path of the Case_Lookup_MCP_Server, launched via
# `python -m mcp_server.case_lookup_server` (matching the module's own
# `if __name__ == "__main__": mcp.run(transport="stdio")` entry point,
# and mirroring tests/integration/test_case_lookup_mcp_stdio.py's stdio
# subprocess launch).
_MCP_SERVER_MODULE = "mcp_server.case_lookup_server"

# Project root, used as the MCP subprocess's working directory so
# `python -m mcp_server.case_lookup_server` resolves correctly regardless
# of the caller's own current working directory.
_PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Requirement 8.4/12.3: reminder appended to the email-composition prompt
# on the single content-guard-failure retry. Restates the content
# guard's own rules (agent/content_guard.py's PROFANITY_INSULT_DENYLIST /
# INTERNAL_IDENTIFIER_DENYLIST intent) as an explicit instruction, giving
# the LLM a concrete, actionable correction rather than merely repeating
# the original prompt unchanged.
_STRICTER_PROMPT_REMINDER = (
    "IMPORTANT: your previous draft of this email failed a required "
    "content compliance check and cannot be sent. Rewrite the body "
    "again, strictly following every one of these rules: (1) use no "
    "profanity, insults, or negative language of any kind about the "
    "customer; (2) never mention internal system, tool, or database "
    "names, case identifiers, root-cause/error codes, or runbook "
    "filenames or URIs; (3) use only plain, non-technical, courteous, "
    "customer-facing language."
)


class MCPLookupError(RuntimeError):
    """Raised by `_default_lookup_case` when the MCP `get_case` call fails
    to complete successfully: an MCP tool-level error result (as opposed
    to a locally raised transport/protocol exception, which propagates on
    its own), or a result whose structured content is missing or cannot
    be parsed into a `CaseLookupResult`.

    Callers (`analyze_case`) MUST classify this identically to any other
    exception raised while calling `lookup_case` -- as
    `ErrorType.LOOKUP_FAILURE` (Requirement 12.1) -- so this class carries
    no special handling beyond being a `RuntimeError`.
    """


class _StricterPromptAgent:
    """Wraps an underlying LLM agent (a Strands `Agent`, or any stub
    matching `email_composer._EmailBodyAgent`'s calling interface) so that
    a stricter content-compliance reminder is appended to whatever prompt
    `email_composer._build_email_prompt` builds, before delegating to the
    wrapped agent.

    Used only for the single content-guard-failure composition retry
    (Requirement 8.4, 12.3); the initial composition attempt always uses
    the unwrapped agent. See this module's docstring for why this
    approach avoids modifying `email_composer.py`'s public interface.
    """

    def __init__(self, inner: Callable[[str], Any]) -> None:
        self._inner = inner

    def __call__(self, prompt: str) -> Any:
        return self._inner(f"{prompt}\n\n{_STRICTER_PROMPT_REMINDER}")


def _build_stdio_mcp_client() -> MCPClient:
    """Build (but do not yet start) a Strands `MCPClient` wrapping a
    stdio transport to `mcp_server/case_lookup_server.py`, per design.md's
    "embedded MCP server over stdio" decision.

    The returned client is a context manager: entering it starts the
    subprocess and background thread and waits (up to
    `LOOKUP_TIMEOUT_SECONDS`) for the MCP `initialize` handshake to
    complete; exiting it stops both.
    """
    server_params = StdioServerParameters(
        command=sys.executable,
        args=["-m", _MCP_SERVER_MODULE],
        cwd=str(_PROJECT_ROOT),
    )
    return MCPClient(
        lambda: stdio_client(server_params),
        startup_timeout=LOOKUP_TIMEOUT_SECONDS,
    )


def _case_record_from_mapping(data: Mapping[str, Any]) -> CaseRecord:
    """Parse a `get_case` MCP result's `case` structured-content mapping
    (a plain dict deserialized from the wire, with `CaseRecord`'s own
    field names as keys) into a `CaseRecord`."""
    return CaseRecord(
        case_id=data["case_id"],
        customer_name=data["customer_name"],
        customer_account_id=data["customer_account_id"],
        issue_description=data["issue_description"],
        network_issue_category=data["network_issue_category"],
        service_location_id=data["service_location_id"],
        status=data["status"],
        created_at=data["created_at"],
        updated_at=data["updated_at"],
    )


def _tool_result_error_detail(tool_result: Mapping[str, Any]) -> str:
    """Best-effort extraction of a human-readable detail string from an
    MCP tool result's `content` list, for inclusion in the
    `MCPLookupError` message raised for a tool-level failure."""
    contents = tool_result.get("content") or []
    texts = [
        content["text"]
        for content in contents
        if isinstance(content, Mapping) and content.get("text")
    ]
    return "; ".join(texts) if texts else "no additional detail available"


def _case_lookup_result_from_tool_result(
    tool_result: Mapping[str, Any],
) -> CaseLookupResult:
    """Parse a Strands `MCPClient.call_tool_sync` result (an
    `MCPToolResult`-shaped mapping) for the `get_case` tool into a
    `CaseLookupResult`.

    Raises:
        MCPLookupError: if the tool call itself reported failure (e.g. an
            underlying `mcp_server/dynamodb_gateway.py` exception
            propagated up through the MCP server, per
            `mcp_server/case_lookup_server.py`'s documented behavior), or
            if a successful-looking result nonetheless carries no
            structured content to parse.
    """
    if tool_result.get("isError") or tool_result.get("status") == "error":
        raise MCPLookupError(
            "get_case MCP tool call reported a failure: "
            f"{_tool_result_error_detail(tool_result)}"
        )

    structured = tool_result.get("structuredContent")
    if not structured:
        raise MCPLookupError(
            "get_case MCP tool call returned no structured content"
        )

    result_type = LookupResultType(structured["result_type"])
    case_data = structured.get("case")
    case = _case_record_from_mapping(case_data) if case_data else None
    return CaseLookupResult(result_type=result_type, case=case)


def _default_lookup_case(case_id: str) -> CaseLookupResult:
    """Default `lookup_case` dependency for `analyze_case`.

    Connects to the Case_Lookup_MCP_Server (`mcp_server/case_lookup_server
    .py`) as a stdio subprocess via a Strands `MCPClient` (per design.md's
    "embedded MCP server over stdio" decision), calls its `get_case` tool
    with `case_id` and a bounded `LOOKUP_TIMEOUT_SECONDS`-second timeout
    (Requirement 12.1), and returns the resulting `CaseLookupResult`.

    Raises on any failure to complete successfully: the MCP subprocess
    failing to launch or initialize, an MCP transport/protocol exception,
    a tool-level error result, or a result with unparseable structured
    content. `analyze_case` never calls this directly without a
    surrounding `try`/`except`; any exception raised here is classified
    as `ErrorType.LOOKUP_FAILURE`.
    """
    client = _build_stdio_mcp_client()
    with client:
        tool_result = client.call_tool_sync(
            tool_use_id=str(uuid.uuid4()),
            name="get_case",
            arguments={"case_id": case_id},
            read_timeout_seconds=timedelta(seconds=LOOKUP_TIMEOUT_SECONDS),
        )
    return _case_lookup_result_from_tool_result(tool_result)


def _build_retrieval_query(case: CaseRecord) -> str:
    """Build the Knowledge_Base retrieval query text for `case`.

    Per Requirement 5.1 and `agent/knowledge_base_retriever.py`'s
    documented usage ("typically built from a case's issue description
    and network issue category"), the query combines both fields.
    """
    return f"{case.issue_description} {case.network_issue_category}"


def _compose_and_guard_email(
    case_summary: Any,
    llm_agent: Callable[[str], Any] | None,
) -> tuple[CustomerEmail | None, list[str]]:
    """Compose a `CustomerEmail` for `case_summary` and run it through the
    content guard, retrying composition exactly once with a stricter
    prompt reminder if the first attempt fails the guard (Requirement
    8.4, 12.3).

    Returns:
        `(email, violations)`: `email` is the (guard-passing) composed
        `CustomerEmail` and `violations` is an empty list on success;
        `email` is `None` and `violations` names every distinct
        denylist entry/pattern still present after the retry has also
        failed the guard.
    """
    email = compose_customer_email(case_summary, llm_agent=llm_agent)
    guard_result = validate_email_content(email)
    if guard_result.passed:
        return email, []

    retry_agent = _StricterPromptAgent(
        llm_agent if llm_agent is not None else email_composer._default_agent()
    )
    email = compose_customer_email(case_summary, llm_agent=retry_agent)
    guard_result = validate_email_content(email)
    if guard_result.passed:
        return email, []

    return None, guard_result.violations


def analyze_case(
    case_id: str,
    *,
    lookup_case: Callable[[str], CaseLookupResult] | None = None,
    retrieve_content: Callable[[str], list[KBRetrievalItem]] | None = None,
    llm_agent: Callable[[str], Any] | None = None,
) -> AnalysisResult:
    """Analyze a single AnyTel support case end to end.

    Orchestrates, in order (per design.md's "Runtime request flow"
    sequence diagram):

      1. MCP `get_case(case_id)` (via `lookup_case`, default
         `_default_lookup_case`), with a short circuit:
         - `INVALID_INPUT` -> `AnalysisResult(success=False,
           error_type=ErrorType.INVALID_INPUT)`.
         - `NOT_FOUND` -> `AnalysisResult(success=False,
           error_type=ErrorType.CASE_NOT_FOUND)`.
         - any exception raised by `lookup_case` (timeout, connection
           failure, or tool-level error) -> `AnalysisResult(success=False,
           error_type=ErrorType.LOOKUP_FAILURE)` (Requirement 12.1).
         - `FOUND` -> proceed with the returned `CaseRecord`.
      2. `retrieve_content(query)` (via `retrieve_content`, default
         `agent.knowledge_base_retriever.retrieve_runbook_content`), with
         the query built by `_build_retrieval_query`. A raised
         `RetrievalTimeoutError`/`RetrievalFailureError` maps to
         `AnalysisResult(success=False,
         error_type=ErrorType.RETRIEVAL_FAILURE)` (Requirement 12.2).
      3. `agent.case_analyzer.determine_validity` then `build_case_summary`
         to assemble the deterministic `CaseSummary` (Requirements 5.1,
         6.1, 6.2, 7.1).
      4. `agent.email_composer.compose_customer_email` (Requirements 8.1,
         8.3) followed by `agent.content_guard.validate_email_content`,
         retrying composition once with a stricter prompt on guard
         failure; if the retry also fails the guard,
         `AnalysisResult(success=False,
         error_type=ErrorType.ANALYSIS_FAILURE)` is returned rather than
         releasing non-compliant content (Requirement 8.4, 12.3).
      5. On success, `AnalysisResult(success=True, case_summary=...,
         customer_email=...)`.

    Any exception raised anywhere in this flow that is not already
    classified above (a step 3/4 defect, an unexpected exception from a
    dependency, etc.) is caught by a catch-all and mapped to
    `AnalysisResult(success=False,
    error_type=ErrorType.ANALYSIS_FAILURE)` (Requirement 12.3), so this
    function never raises and never returns a partial `CaseSummary`/
    `CustomerEmail` alongside a failure.

    Args:
        case_id: The case_id to analyze.
        lookup_case: Overrides the MCP `get_case` lookup dependency.
            Defaults to `_default_lookup_case` (a real stdio MCP round
            trip). Inject a fault-raising/fault-returning callable here
            for testing (e.g. `lambda _: (_ for _ in ()).throw(TimeoutError())`).
        retrieve_content: Overrides the Knowledge_Base retrieval
            dependency. Defaults to
            `agent.knowledge_base_retriever.retrieve_runbook_content`.
        llm_agent: Overrides the LLM agent used for email-body
            composition (passed through to
            `agent.email_composer.compose_customer_email`). Defaults to
            `None`, letting `compose_customer_email` construct its own
            default Bedrock-backed `Agent` lazily.

    Returns:
        The `AnalysisResult` of the analysis: a success result with
        `case_summary` and `customer_email` populated, or a failure
        result with `error_type`/`error_message` populated and both
        `case_summary`/`customer_email` left `None`.
    """
    try:
        lookup_fn = lookup_case if lookup_case is not None else _default_lookup_case
        retrieve_fn = (
            retrieve_content if retrieve_content is not None else retrieve_runbook_content
        )

        # Step 1: MCP get_case, with the not-found/invalid-input short
        # circuit and lookup-failure classification (Requirement 12.1).
        try:
            lookup_result = lookup_fn(case_id)
        except Exception as exc:
            return AnalysisResult(
                success=False,
                error_type=ErrorType.LOOKUP_FAILURE,
                error_message=f"case lookup failed: {exc}",
            )

        if lookup_result.result_type is LookupResultType.INVALID_INPUT:
            return AnalysisResult(
                success=False,
                error_type=ErrorType.INVALID_INPUT,
                error_message="case_id is empty or consists only of whitespace",
            )

        if lookup_result.result_type is LookupResultType.NOT_FOUND:
            return AnalysisResult(
                success=False,
                error_type=ErrorType.CASE_NOT_FOUND,
                error_message=f"no case found for case_id '{case_id}'",
            )

        case = lookup_result.case
        if case is None:
            # Defensive: a FOUND result must carry a CaseRecord. Treat a
            # violation of that invariant as a lookup failure rather than
            # proceeding with no case data.
            return AnalysisResult(
                success=False,
                error_type=ErrorType.LOOKUP_FAILURE,
                error_message=(
                    "case lookup reported FOUND but returned no case record"
                ),
            )

        # Step 2: Knowledge_Base retrieval (Requirement 5.1, 12.2).
        query = _build_retrieval_query(case)
        try:
            retrieval_items = retrieve_fn(query)
        except (RetrievalTimeoutError, RetrievalFailureError) as exc:
            return AnalysisResult(
                success=False,
                error_type=ErrorType.RETRIEVAL_FAILURE,
                error_message=f"knowledge base retrieval failed: {exc}",
            )

        # Step 3: deterministic CaseAnalyzer decision core (Requirements
        # 5.1, 6.1, 6.2, 7.1).
        determination = determine_validity(case, retrieval_items)
        case_summary = build_case_summary(case, determination)

        # Step 4: EmailComposer + ContentGuard, with one retry on guard
        # failure (Requirements 8.1, 8.3, 8.4, 12.3).
        email, violations = _compose_and_guard_email(case_summary, llm_agent)
        if email is None:
            return AnalysisResult(
                success=False,
                error_type=ErrorType.ANALYSIS_FAILURE,
                error_message=(
                    "composed customer email failed content validation "
                    f"after one retry: {', '.join(violations)}"
                ),
            )

        # Step 5: success.
        return AnalysisResult(
            success=True,
            case_summary=case_summary,
            customer_email=email,
        )

    except Exception as exc:  # noqa: BLE001 - Requirement 12.3 catch-all
        return AnalysisResult(
            success=False,
            error_type=ErrorType.ANALYSIS_FAILURE,
            error_message=f"analysis could not complete: {exc}",
        )
