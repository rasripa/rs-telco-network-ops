"""tests/property/test_network_ops_agent_failure_isolation.py

Property-based test for agent/network_ops_agent.py's `analyze_case`
orchestration -- design.md Correctness Properties, Property 14:

    For any simulated fault injected at the case-lookup stage (timeout or
    connection failure), the knowledge-base-retrieval stage (timeout or
    connection failure), or elsewhere in `analyze_case`, the returned
    `AnalysisResult` has `success = False` with the `error_type` matching
    the fault's stage (`LOOKUP_FAILURE`, `RETRIEVAL_FAILURE`, or
    `ANALYSIS_FAILURE` respectively), and `case_summary`/`customer_email`
    are both `None`.

Validates: Requirements 12.1, 12.2, 12.3

`analyze_case` exposes its MCP case-lookup and Knowledge_Base-retrieval
interactions (and the LLM agent used for email composition) as plain,
directly fault-injectable callables (`lookup_case`, `retrieve_content`,
`llm_agent`; see agent/network_ops_agent.py's module docstring), so this
test injects fault-simulating stubs for each rather than constructing a
fake MCP client or a fake `bedrock-agent-runtime` client.

Four fault scenarios are covered, one per test function, each targeting a
distinct branch of `analyze_case`'s error classification:

1. `lookup_case` raises an arbitrary exception (simulating an MCP lookup
   timeout/connection failure) -> `ErrorType.LOOKUP_FAILURE`
   (Requirement 12.1). `retrieve_content`/`llm_agent` must never be
   called, since the lookup failure short-circuits the rest of the flow.
2. `lookup_case` succeeds with `FOUND`, but `retrieve_content` raises
   `RetrievalTimeoutError`/`RetrievalFailureError` (simulating a
   Knowledge_Base timeout/connection failure) -> `ErrorType
   .RETRIEVAL_FAILURE` (Requirement 12.2). `llm_agent` must never be
   called.
3. `lookup_case` and `retrieve_content` both succeed, but `llm_agent`'s
   composed output always fails the content guard (it always contains a
   denylisted term, regardless of the prompt -- including the
   stricter-prompt retry) -> `ErrorType.ANALYSIS_FAILURE` after exactly
   one retry (Requirement 12.3's "elsewhere in analyze_case" catch-all,
   applied to the content-guard-exhausted path documented in
   `agent/network_ops_agent.py`'s `_compose_and_guard_email`).
4. `lookup_case` succeeds with `FOUND`, but `retrieve_content` raises an
   arbitrary, *unclassified* exception (anything other than
   `RetrievalTimeoutError`/`RetrievalFailureError`) -> still caught by
   `analyze_case`'s catch-all and mapped to `ErrorType.ANALYSIS_FAILURE`,
   never propagating out of `analyze_case` (Requirement 12.3). `llm_agent`
   must never be called.

In every scenario, `AnalysisResult.case_summary` and
`AnalysisResult.customer_email` are both asserted to be `None` -- no
partial result is ever allowed to leak out alongside a failure.
"""

from __future__ import annotations

from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from agent.content_guard import (
    INTERNAL_IDENTIFIER_DENYLIST,
    PROFANITY_INSULT_DENYLIST,
)
from agent.knowledge_base_retriever import (
    RetrievalFailureError,
    RetrievalTimeoutError,
)
from agent.models import (
    CaseLookupResult,
    CaseRecord,
    ErrorType,
    KBRetrievalItem,
    LookupResultType,
)
from agent.network_ops_agent import analyze_case

_VALID_STATUSES = ["Open", "In Progress", "Resolved", "Closed"]

# Arbitrary case_id text (may be empty, whitespace-only, or ordinary
# text) -- `lookup_case`/`retrieve_content` are stubbed unconditionally
# below, so `analyze_case`'s own INVALID_INPUT short circuit (driven by
# the *stubbed* result_type, not by case_id's literal content) is not
# exercised here; that branch belongs to Property 4's tests. This
# strategy exists solely to vary the case_id string threaded through
# `analyze_case`, per this task's instructions.
_case_id_strategy = st.text(max_size=30)

# A mix of exception types standing in for "an MCP lookup timeout/
# connection failure" or "an arbitrary unexpected exception raised from
# any injected dependency" -- analyze_case's classification of these
# must depend only on *which stage* raised, never on the exception's
# concrete type or message.
_exception_type_strategy = st.sampled_from(
    [RuntimeError, ValueError, ConnectionError, TimeoutError, OSError]
)
_exception_message_strategy = st.text(max_size=50)


def _case_record_strategy() -> st.SearchStrategy[CaseRecord]:
    """Build arbitrary, structurally valid `CaseRecord`s to inject as the
    `FOUND` lookup result's payload, so the fault-injection scenarios that
    proceed past the lookup stage exercise varying case content."""
    return st.builds(
        CaseRecord,
        case_id=st.text(min_size=1, max_size=16),
        customer_name=st.text(min_size=1, max_size=30),
        customer_account_id=st.text(min_size=1, max_size=30),
        issue_description=st.text(min_size=1, max_size=60),
        network_issue_category=st.text(min_size=1, max_size=20),
        service_location_id=st.text(min_size=1, max_size=16),
        status=st.sampled_from(_VALID_STATUSES),
        created_at=st.just("2024-01-01T00:00:00Z"),
        updated_at=st.just("2024-01-02T00:00:00Z"),
    )


def _kb_retrieval_item_strategy(category: str) -> st.SearchStrategy[KBRetrievalItem]:
    """Build a `KBRetrievalItem` whose `issue_category` matches `category`,
    with varying `text`/`score`/`source_uri`, for the guard-failure and
    unexpected-exception scenarios that need a `FOUND` case to reach
    later pipeline stages."""
    return st.builds(
        KBRetrievalItem,
        text=st.just(
            "## Root Cause\nDegraded signal at the local node.\n\n"
            "## Remediation Steps\n- Restart the router\n- Wait 2 minutes"
        ),
        score=st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False),
        source_uri=st.text(min_size=1, max_size=30),
        issue_category=st.just(category),
    )


# All denylist entries from both lists (mirrors
# tests/property/test_content_guard.py), used to build an `llm_agent`
# stub whose output always fails `validate_email_content`, regardless of
# the prompt it was given (including the stricter-prompt retry).
_ALL_DENYLIST_ENTRIES: tuple[str, ...] = (
    PROFANITY_INSULT_DENYLIST + INTERNAL_IDENTIFIER_DENYLIST
)


class _CallCounter:
    """A simple call-counting callable used as a `retrieve_content`/
    `llm_agent` stub that must never be invoked in a given scenario.

    Using a counter (rather than raising `AssertionError` from inside the
    stub) avoids that exception being silently swallowed by
    `analyze_case`'s own catch-all and misreported as `ANALYSIS_FAILURE`;
    instead, the test asserts `call_count == 0` after `analyze_case`
    returns.
    """

    def __init__(self, return_value: Any = None) -> None:
        self.call_count = 0
        self._return_value = return_value

    def __call__(self, *_args: Any, **_kwargs: Any) -> Any:
        self.call_count += 1
        return self._return_value


def _found_lookup(case: CaseRecord) -> CaseLookupResult:
    return CaseLookupResult(result_type=LookupResultType.FOUND, case=case)


# Feature: network-ops-agent, Property 14: Failure isolation on lookup, retrieval, and agent errors
#
# For any exception raised by `lookup_case` (simulating an MCP lookup
# timeout/connection failure), `analyze_case` returns
# AnalysisResult(success=False, error_type=ErrorType.LOOKUP_FAILURE) with
# case_summary/customer_email both None, and never calls
# retrieve_content/llm_agent, regardless of the case_id or the raised
# exception's type/message.
#
# Validates: Requirements 12.1
@given(
    case_id=_case_id_strategy,
    exception_type=_exception_type_strategy,
    exception_message=_exception_message_strategy,
)
@settings(max_examples=100)
def test_lookup_failure_isolation(
    case_id: str,
    exception_type: type[Exception],
    exception_message: str,
) -> None:
    def failing_lookup(_case_id: str) -> CaseLookupResult:
        raise exception_type(exception_message)

    retrieve_counter = _CallCounter(return_value=[])
    llm_counter = _CallCounter(return_value="unused body text")

    result = analyze_case(
        case_id,
        lookup_case=failing_lookup,
        retrieve_content=retrieve_counter,
        llm_agent=llm_counter,
    )

    assert result.success is False
    assert result.error_type is ErrorType.LOOKUP_FAILURE
    assert result.case_summary is None
    assert result.customer_email is None

    # A lookup failure short-circuits the rest of the flow: neither
    # knowledge-base retrieval nor email composition is ever attempted.
    assert retrieve_counter.call_count == 0
    assert llm_counter.call_count == 0


# Feature: network-ops-agent, Property 14: Failure isolation on lookup, retrieval, and agent errors
#
# For any `RetrievalTimeoutError`/`RetrievalFailureError` raised by
# `retrieve_content` after `lookup_case` succeeds with FOUND,
# `analyze_case` returns AnalysisResult(success=False,
# error_type=ErrorType.RETRIEVAL_FAILURE) with case_summary/customer_email
# both None, and never calls llm_agent, regardless of the case_id, the
# found CaseRecord's content, or the raised exception's type/message.
#
# Validates: Requirements 12.2
@given(
    case_id=_case_id_strategy,
    case=_case_record_strategy(),
    exception_type=st.sampled_from([RetrievalTimeoutError, RetrievalFailureError]),
    exception_message=_exception_message_strategy,
)
@settings(max_examples=100)
def test_retrieval_failure_isolation(
    case_id: str,
    case: CaseRecord,
    exception_type: type[Exception],
    exception_message: str,
) -> None:
    def found_lookup(_case_id: str) -> CaseLookupResult:
        return _found_lookup(case)

    def failing_retrieve(_query: str) -> list[KBRetrievalItem]:
        raise exception_type(exception_message)

    llm_counter = _CallCounter(return_value="unused body text")

    result = analyze_case(
        case_id,
        lookup_case=found_lookup,
        retrieve_content=failing_retrieve,
        llm_agent=llm_counter,
    )

    assert result.success is False
    assert result.error_type is ErrorType.RETRIEVAL_FAILURE
    assert result.case_summary is None
    assert result.customer_email is None

    # A retrieval failure short-circuits the rest of the flow: email
    # composition is never attempted.
    assert llm_counter.call_count == 0


# Feature: network-ops-agent, Property 14: Failure isolation on lookup, retrieval, and agent errors
#
# For any lookup/retrieval that both succeed, if the injected llm_agent's
# output always contains a denylisted term (regardless of the prompt it
# is given, including the stricter-prompt retry), analyze_case retries
# composition exactly once and then returns AnalysisResult(success=False,
# error_type=ErrorType.ANALYSIS_FAILURE) with case_summary/customer_email
# both None, never raising and never returning a partial result.
#
# Validates: Requirements 12.3
@given(
    case_id=_case_id_strategy,
    case=_case_record_strategy(),
    denylisted_entry=st.sampled_from(_ALL_DENYLIST_ENTRIES),
    determine_valid=st.booleans(),
    data=st.data(),
)
@settings(max_examples=100)
def test_content_guard_exhaustion_yields_analysis_failure(
    case_id: str,
    case: CaseRecord,
    denylisted_entry: str,
    determine_valid: bool,
    data: st.DataObject,
) -> None:
    def found_lookup(_case_id: str) -> CaseLookupResult:
        return _found_lookup(case)

    if determine_valid:
        matching_item = data.draw(
            _kb_retrieval_item_strategy(case.network_issue_category)
        )
        retrieval_items = [matching_item]
    else:
        retrieval_items = []

    def succeeding_retrieve(_query: str) -> list[KBRetrievalItem]:
        return retrieval_items

    # Always returns text containing a denylisted entry, regardless of
    # the prompt it is called with -- so both the initial composition
    # attempt and the stricter-prompt retry fail the content guard.
    bad_text = f"Hello there, {denylisted_entry} appears in every draft."
    llm_counter = _CallCounter(return_value=bad_text)

    result = analyze_case(
        case_id,
        lookup_case=found_lookup,
        retrieve_content=succeeding_retrieve,
        llm_agent=llm_counter,
    )

    assert result.success is False
    assert result.error_type is ErrorType.ANALYSIS_FAILURE
    assert result.case_summary is None
    assert result.customer_email is None

    # Composition was attempted exactly twice: the initial attempt, plus
    # exactly one stricter-prompt retry -- never zero, and never more
    # than two.
    assert llm_counter.call_count == 2


# Feature: network-ops-agent, Property 14: Failure isolation on lookup, retrieval, and agent errors
#
# For any arbitrary, unclassified exception raised by retrieve_content
# (anything other than RetrievalTimeoutError/RetrievalFailureError) after
# lookup_case succeeds with FOUND, analyze_case's catch-all still maps the
# failure to AnalysisResult(success=False,
# error_type=ErrorType.ANALYSIS_FAILURE) with case_summary/customer_email
# both None, rather than letting the exception propagate out of
# analyze_case, and never calls llm_agent.
#
# Validates: Requirements 12.3
@given(
    case_id=_case_id_strategy,
    case=_case_record_strategy(),
    exception_type=_exception_type_strategy,
    exception_message=_exception_message_strategy,
)
@settings(max_examples=100)
def test_unclassified_dependency_exception_yields_analysis_failure(
    case_id: str,
    case: CaseRecord,
    exception_type: type[Exception],
    exception_message: str,
) -> None:
    def found_lookup(_case_id: str) -> CaseLookupResult:
        return _found_lookup(case)

    def unexpectedly_failing_retrieve(_query: str) -> list[KBRetrievalItem]:
        # Deliberately NOT RetrievalTimeoutError/RetrievalFailureError,
        # so this exercises the outer catch-all rather than the
        # dedicated RETRIEVAL_FAILURE classification branch.
        raise exception_type(exception_message)

    llm_counter = _CallCounter(return_value="unused body text")

    result = analyze_case(
        case_id,
        lookup_case=found_lookup,
        retrieve_content=unexpectedly_failing_retrieve,
        llm_agent=llm_counter,
    )

    assert result.success is False
    assert result.error_type is ErrorType.ANALYSIS_FAILURE
    assert result.case_summary is None
    assert result.customer_email is None
    assert llm_counter.call_count == 0
