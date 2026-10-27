"""mcp_server/case_lookup_server.py: Case_Lookup_MCP_Server.

Exposes a single Model Context Protocol tool, `get_case`, over stdio
transport (Requirement 4, design.md's "4. Case_Lookup_MCP_Server"
component section).

Note on MCP SDK naming: the project pins `mcp==2.1.1` (see pyproject.toml).
In `mcp` 2.x, the ergonomic server class the design document refers to as
"FastMCP" was renamed to `MCPServer`
(`mcp.server.mcpserver.MCPServer`; see
https://py.sdk.modelcontextprotocol.io/v2/migration/#fastmcp-renamed-to-mcpserver).
This module uses `MCPServer` accordingly -- it is the same `@mcp.tool()`
decorator-based API the design document illustrates.

`get_case` classifies every call into exactly one of three outcomes and
never raises for any of them:

    FOUND          - `case_id` (after stripping surrounding whitespace)
                     matches a record in the Case_Table; `case` is
                     populated with all Case_Record fields.
    NOT_FOUND      - `case_id` is non-empty/non-whitespace but no matching
                     record exists in the Case_Table; `case` is `None`.
    INVALID_INPUT  - `case_id` is empty or consists only of whitespace;
                     `case` is `None`. The DynamoDB gateway is never
                     called in this case.

A failure from the underlying DynamoDB gateway (e.g. throttling, access
denied, connectivity failure) is a different failure mode from the three
above and is intentionally *not* caught here: `mcp_server/dynamodb_gateway.py`
already propagates such failures unchanged (see its docstring), and
swallowing them into one of FOUND/NOT_FOUND/INVALID_INPUT would hide a
real infrastructure failure behind a misleading result. Classifying that
failure (e.g. into a `LOOKUP_FAILURE` error per Requirement 12.1) is the
responsibility of the orchestrator (`agent/network_ops_agent.py`, a later
task), which calls this tool through an MCP client with its own bounded
timeout.

Return value serialization: `CaseLookupResult` and `CaseRecord` are frozen
dataclasses (agent/models.py), not Pydantic models. The `mcp` SDK's tool
layer (`mcp.server.mcpserver.utilities.func_metadata.func_metadata`)
handles this natively: for a return annotation that is "a regular class
with type hints" (i.e. a dataclass), it introspects the type hints via
`get_type_hints` and builds an equivalent Pydantic model to validate and
serialize the return value -- including nested dataclasses (`case`) and
enum members (`result_type`, serialized as its string value, e.g.
"FOUND"). No manual `dataclasses.asdict`/dict conversion is required; the
tool function returns the dataclass instance directly.
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from agent.models import CaseLookupResult, LookupResultType
from mcp_server import dynamodb_gateway

mcp = MCPServer("case-lookup")


@mcp.tool()
def get_case(case_id: str) -> CaseLookupResult:
    """Look up a Case_Record by case_id.

    Args:
        case_id: The Case_Table partition key value to look up.

    Returns:
        A `CaseLookupResult` with `result_type` one of:
          FOUND         - case exists; `case` populated with all
                           Case_Record fields.
          NOT_FOUND     - case_id is non-empty but no matching item
                           exists.
          INVALID_INPUT - case_id is empty or all-whitespace.

        Never raises for these three conditions. An unexpected exception
        raised by the underlying DynamoDB gateway (e.g. throttling) is not
        caught here and propagates to the caller unchanged.
    """
    if not case_id or not case_id.strip():
        return CaseLookupResult(result_type=LookupResultType.INVALID_INPUT, case=None)

    case = dynamodb_gateway.get_case_item(case_id.strip())
    if case is None:
        return CaseLookupResult(result_type=LookupResultType.NOT_FOUND, case=None)

    return CaseLookupResult(result_type=LookupResultType.FOUND, case=case)


if __name__ == "__main__":
    mcp.run(transport="stdio")
