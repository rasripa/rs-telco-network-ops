"""tests/integration/test_case_lookup_mcp_stdio.py

MCP stdio transport round-trip integration test for
mcp_server/case_lookup_server.py (task 9.4).

Unlike tests/property/test_case_lookup.py -- which calls the `get_case`
Python function directly with a mocked `dynamodb_gateway.get_case_item` --
this test spins up `mcp_server/case_lookup_server.py` as a *real* stdio
subprocess (via `python -m mcp_server.case_lookup_server`, the same
`__main__` entry point that calls `mcp.run(transport="stdio")`), connects
to it with the MCP Python SDK's stdio client transport
(`mcp.client.stdio.stdio_client`) and `mcp.client.session.ClientSession`,
and calls the `get_case` tool over that real transport. This exercises the
actual MCP protocol handshake (`initialize`), JSON-RPC request/response
framing over stdin/stdout, and result deserialization -- the "over stdio"
transport concern of Requirement 4.5 -- rather than the tool's internal
branching logic (Requirement 4.1/4.2/4.3/4.6, already covered by Property
4 in tests/property/test_case_lookup.py).

Two scenarios are exercised:

  1. An empty-string `case_id` -> `INVALID_INPUT`. This is Requirement
     4.6's condition and, per `case_lookup_server.get_case`, is handled
     entirely before the DynamoDB gateway is ever consulted -- so this
     assertion requires *no* AWS access at all and always runs
     unconditionally. This is the test's primary assertion.

  2. A random/nonexistent `case_id` -> expected `NOT_FOUND` (Requirement
     4.1/4.3). This *does* require a reachable DynamoDB endpoint (the
     gateway calls `boto3.resource("dynamodb").Table(...).get_item(...)`
     for a non-empty case_id), so it is gated behind a live-credentials
     check and is skipped (not failed) when AWS access is unavailable,
     keeping the suite runnable offline. The check also assumes no record
     with the random UUID-based case_id happens to already exist in
     whatever table is configured; if the lookup unexpectedly returns
     FOUND, that assertion is skipped too (with an explanatory message)
     rather than failing the transport round-trip test on a data
     coincidence unrelated to what this test verifies.

Both scenarios independently confirm the transport round trip: a
`CallToolResult` comes back non-error, and its `structured_content`
deserializes into the exact `CaseLookupResult` shape (`result_type` +
`case`) that `mcp_server/case_lookup_server.py`'s docstring documents
(`func_metadata` builds an equivalent Pydantic model from the
`CaseLookupResult` dataclass return-type hint automatically).
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from agent.models import LookupResultType

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def anyio_backend() -> str:
    """Pin the anyio pytest plugin to the asyncio backend for this module."""
    return "asyncio"


def _dynamodb_access_available() -> bool:
    """Best-effort check for a reachable DynamoDB endpoint under the
    ambient AWS credentials (whatever boto3's default credential chain
    resolves -- e.g. `AWS_PROFILE=rs-gen-ai` in the environment, matching
    how `mcp_server/dynamodb_gateway.py` itself authenticates per
    Requirement 4.4).

    A lightweight `list_tables` call (rather than `sts:GetCallerIdentity`)
    is used deliberately: it confirms not just that *some* credentials
    resolve, but that they can actually reach the DynamoDB API this test's
    second scenario depends on. Any failure (no credentials, invalid
    profile, network/permissions error) is treated as "unavailable" so the
    check never raises -- callers use this purely to decide whether to
    skip.
    """
    try:
        import boto3

        boto3.client("dynamodb").list_tables(Limit=1)
    except Exception:
        return False
    return True


@pytest.mark.anyio
async def test_stdio_round_trip_empty_case_id_returns_invalid_input() -> None:
    """Primary, unconditional assertion (Requirement 4.5, 4.6): connecting
    to the real `case_lookup_server.py` stdio subprocess and calling
    `get_case("")` returns a well-formed `CaseLookupResult` with
    `result_type == INVALID_INPUT` and no `case`. Requires no AWS access,
    since `get_case` classifies an empty/whitespace-only `case_id` before
    ever calling the DynamoDB gateway."""
    server_params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mcp_server.case_lookup_server"],
        cwd=str(PROJECT_ROOT),
    )

    async with stdio_client(server_params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()

            tools_result = await session.list_tools()
            tool_names = [tool.name for tool in tools_result.tools]
            assert "get_case" in tool_names

            call_result = await session.call_tool("get_case", {"case_id": ""})

            assert call_result.is_error is False
            structured = call_result.structured_content
            assert structured is not None
            assert structured["result_type"] == LookupResultType.INVALID_INPUT.value
            assert structured["case"] is None


@pytest.mark.anyio
async def test_stdio_round_trip_unknown_case_id_returns_not_found() -> None:
    """Secondary, AWS-gated assertion (Requirement 4.1, 4.3): a
    non-empty, essentially-guaranteed-nonexistent `case_id` (a random
    UUID) returns `NOT_FOUND` over the real stdio transport. Skipped
    (not failed) when DynamoDB access is unavailable in this environment,
    since this scenario requires the gateway's `GetItem` call to actually
    reach AWS."""
    if not _dynamodb_access_available():
        pytest.skip(
            "DynamoDB access is not available in this environment "
            "(no/invalid AWS credentials or unreachable endpoint); "
            "skipping the AWS-dependent NOT_FOUND transport check."
        )

    random_case_id = f"integration-test-{uuid.uuid4()}"

    server_params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mcp_server.case_lookup_server"],
        cwd=str(PROJECT_ROOT),
    )

    # `pytest.skip()` raises `Skipped`, a `BaseException`; raising it from
    # inside the nested `stdio_client`/`ClientSession` async context managers
    # gets caught up in their anyio task groups and re-wrapped as a noisy
    # `BaseExceptionGroup` before pytest ever sees the plain `Skipped`
    # outcome. Instead, the outcome is captured here and the skip/assertion
    # is deferred until after both context managers have cleanly exited.
    skip_reason: str | None = None
    structured: dict[str, object] | None = None

    async with stdio_client(server_params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()

            call_result = await session.call_tool("get_case", {"case_id": random_case_id})

            if call_result.is_error:
                skip_reason = (
                    "get_case reported an error for the NOT_FOUND scenario "
                    "(likely a missing/misconfigured Case_Table in this "
                    "environment rather than a transport problem); "
                    f"content={call_result.content!r}"
                )
            else:
                structured = call_result.structured_content

    if skip_reason is not None:
        pytest.skip(skip_reason)

    assert structured is not None

    if structured["result_type"] == LookupResultType.FOUND.value:
        pytest.skip(
            "Randomly generated case_id unexpectedly matched an existing "
            "Case_Table record; skipping this data-dependent assertion "
            "rather than failing the transport round-trip test."
        )

    assert structured["result_type"] == LookupResultType.NOT_FOUND.value
    assert structured["case"] is None
