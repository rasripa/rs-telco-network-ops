"""tests/property/test_case_lookup.py

Property-based test for mcp_server/case_lookup_server.py's `get_case`
tool (design.md Correctness Properties, Property 4).

`get_case` is decorated with `@mcp.tool()` from
`mcp.server.mcpserver.MCPServer`, but the decorator returns the original
callable unchanged (confirmed by direct inspection: `get_case` remains a
plain Python function after decoration, and calling it directly behaves
like an ordinary function call, not a tool-invocation object). The
decorator's job is only to register the function in the `MCPServer`
instance's separate tool registry; it does not wrap or replace the
function reference bound to the module-level name. This test therefore
calls `get_case` directly.

The backing DynamoDB store is simulated by mocking
`mcp_server.dynamodb_gateway.get_case_item`, patched via the reference
`mcp_server.case_lookup_server.dynamodb_gateway.get_case_item` -- this is
the name `case_lookup_server.py` actually calls
(`dynamodb_gateway.get_case_item(...)`, imported via
`from mcp_server import dynamodb_gateway`), so patching it there
intercepts the call regardless of what `dynamodb_gateway` module-level
name resolves to elsewhere.
"""

from __future__ import annotations

from unittest.mock import patch

from hypothesis import example, given, settings
from hypothesis import strategies as st

from agent.models import CaseRecord, LookupResultType
from mcp_server.case_lookup_server import get_case

_VALID_STATUSES = ["Open", "In Progress", "Resolved", "Closed"]

# Case_id strategy covering: empty string, whitespace-only strings (of
# varying whitespace characters), and arbitrary non-empty text (which may
# incidentally be whitespace-only, so the test always classifies expected
# behavior from the actual string content rather than from which strategy
# branch produced it).
_case_id_strategy = st.one_of(
    st.just(""),
    st.text(alphabet=" \t\n\r", min_size=1, max_size=10),
    st.text(min_size=1, max_size=30),
)


def _case_record_strategy() -> st.SearchStrategy[CaseRecord]:
    return st.builds(
        CaseRecord,
        case_id=st.text(min_size=1, max_size=12),
        customer_name=st.text(min_size=1, max_size=20),
        customer_account_id=st.text(min_size=1, max_size=20),
        issue_description=st.text(min_size=1, max_size=50),
        network_issue_category=st.text(min_size=1, max_size=20),
        service_location_id=st.text(min_size=1, max_size=12),
        status=st.sampled_from(_VALID_STATUSES),
        created_at=st.just("2024-01-01T00:00:00Z"),
        updated_at=st.just("2024-01-02T00:00:00Z"),
    )


def _make_case(case_id: str) -> CaseRecord:
    return CaseRecord(
        case_id=case_id,
        customer_name="Jamie Rivera",
        customer_account_id="AnyTel-000123",
        issue_description="No internet connectivity since this morning.",
        network_issue_category="connectivity_outage",
        service_location_id="loc-42",
        status="Open",
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-02T00:00:00Z",
    )


# Feature: network-ops-agent, Property 4: Case lookup returns exactly the matching record or a correctly classified miss
#
# For any fake backing store populated with a set of distinct
# `CaseRecord`s and any queried `case_id` string: if `case_id` is empty or
# all-whitespace, `get_case` returns `INVALID_INPUT` with no `case`
# populated; otherwise, if `case_id` matches a record in the store,
# `get_case` returns `FOUND` with `case` equal in every field to that
# stored record and to no other record in the store; otherwise, `get_case`
# returns `NOT_FOUND` with no `case` populated. In all three cases,
# `get_case` returns a result and does not raise.
#
# Validates: Requirements 4.1, 4.2, 4.3, 4.6
@given(queried_case_id=_case_id_strategy, stored_record=_case_record_strategy())
@settings(max_examples=100)
@example(queried_case_id="", stored_record=_make_case("abc"))
@example(queried_case_id="   ", stored_record=_make_case("abc"))
@example(queried_case_id="\t\n", stored_record=_make_case("abc"))
@example(queried_case_id="abc", stored_record=_make_case("abc"))
@example(queried_case_id="  abc  ", stored_record=_make_case("abc"))
@example(queried_case_id="zzz-not-in-store", stored_record=_make_case("abc"))
def test_get_case_returns_match_or_correctly_classified_miss(
    queried_case_id: str,
    stored_record: CaseRecord,
) -> None:
    """Simulates a backing store containing exactly one record (keyed by
    its own case_id) plus an unrelated second record, so that a FOUND
    result can be checked against "no other record in the store" too."""

    other_record = CaseRecord(
        case_id="__other_record__",
        customer_name="Other Customer",
        customer_account_id="AnyTel-999999",
        issue_description="Unrelated issue.",
        network_issue_category="unrelated_category",
        service_location_id="loc-99",
        status="Closed",
        created_at="2023-01-01T00:00:00Z",
        updated_at="2023-01-02T00:00:00Z",
    )
    store = {stored_record.case_id: stored_record, other_record.case_id: other_record}

    stripped = queried_case_id.strip()

    def fake_get_case_item(case_id: str) -> CaseRecord | None:
        return store.get(case_id)

    with patch(
        "mcp_server.case_lookup_server.dynamodb_gateway.get_case_item",
        side_effect=fake_get_case_item,
    ) as mock_gateway:
        result = get_case(queried_case_id)

        if not stripped:
            # Requirement 4.6: empty/whitespace-only case_id -> INVALID_INPUT,
            # no case populated, and the backing gateway is never consulted.
            assert result.result_type == LookupResultType.INVALID_INPUT
            assert result.case is None
            mock_gateway.assert_not_called()
        elif stripped in store:
            # Requirements 4.1, 4.2: a matching case_id -> FOUND with the
            # exact matching record, and not confused with any other record.
            assert result.result_type == LookupResultType.FOUND
            assert result.case is not None
            assert result.case == store[stripped]
            for other_case_id, other_stored_record in store.items():
                if other_case_id != stripped:
                    assert result.case != other_stored_record
        else:
            # Requirement 4.3: non-empty case_id with no matching record
            # -> NOT_FOUND, no case populated.
            assert result.result_type == LookupResultType.NOT_FOUND
            assert result.case is None
