"""mcp_server/dynamodb_gateway.py: isolated DynamoDB I/O gateway for Case_Table lookups.

This module is a thin, isolated I/O gateway around the DynamoDB `GetItem`
call used by the Case_Lookup_MCP_Server (mcp_server/case_lookup_server.py,
task 9.2). It contains no business logic: empty/invalid `case_id`
validation and FOUND/NOT_FOUND/INVALID_INPUT classification belong in the
MCP tool layer, not here. This module either returns a fully-populated
`CaseRecord` or `None` for a missing item -- it never raises for that
condition.

Per the design document's Case_Lookup_MCP_Server component section
(.kiro/specs/network-ops-agent/design.md) and Requirement 4.4, this
gateway authenticates to Amazon DynamoDB using credentials scoped to the
Deployment_Account: "Both the agent process and the MCP subprocess run
under the AgentCore Runtime's IAM execution role when deployed, and under
the `rs-gen-ai` profile's credentials when run locally for development -
both are 'credentials scoped to the Deployment_Account' as required by
4.4." Concretely, that means this gateway relies on boto3's default
credential chain (no named profile is selected in code): the deployed
AgentCore Runtime execution role is picked up automatically via that
chain, and local development picks up whatever profile/credentials are
configured in the calling environment (e.g. `AWS_PROFILE=rs-gen-ai`). An
optional `region` parameter is accepted for callers that want to pin the
region explicitly rather than relying on the default chain's resolved
region.
"""

from __future__ import annotations

import boto3

from agent.models import CaseRecord

DEFAULT_TABLE_NAME = "Case_Table"


def get_case_item(
    case_id: str,
    table_name: str = DEFAULT_TABLE_NAME,
    region: str | None = None,
) -> CaseRecord | None:
    """Isolate the boto3 `GetItem` call against `Case_Table`.

    Uses `boto3.resource("dynamodb").Table(...).get_item(...)`, which
    returns plain Python types directly, so no typed-attribute-map
    deserialization is needed.

    Args:
        case_id: The `Case_Table` partition key value to look up. This
            function performs no validation of `case_id` (e.g. rejecting
            empty/whitespace-only values); that validation belongs in the
            MCP tool layer (task 9.2), not this gateway.
        table_name: The `Case_Table` name. Defaults to `Case_Table`.
        region: Optional AWS Region override. When omitted, the region is
            resolved by boto3's default credential/region chain (e.g. the
            `AWS_DEFAULT_REGION` environment variable, profile
            configuration, or the AgentCore Runtime execution
            environment).

    Returns:
        A fully-populated `CaseRecord` when an item with the given
        `case_id` exists in the table; `None` when no such item exists.
        No exception is raised for the "not found" case.

    Raises:
        botocore.exceptions.ClientError / BotoCoreError: propagated
            unchanged for any DynamoDB failure other than "item not
            found" (e.g. throttling, access denied, connectivity
            failure), so the caller (the MCP tool layer / orchestrator)
            can classify and handle those failures per Requirement 12.1.
    """
    resource = boto3.resource("dynamodb", region_name=region)
    table = resource.Table(table_name)

    response = table.get_item(Key={"case_id": case_id})
    item = response.get("Item")
    if item is None:
        return None

    return CaseRecord(
        case_id=item["case_id"],
        customer_name=item["customer_name"],
        customer_account_id=item["customer_account_id"],
        issue_description=item["issue_description"],
        network_issue_category=item["network_issue_category"],
        service_location_id=item["service_location_id"],
        status=item["status"],
        created_at=item["created_at"],
        updated_at=item["updated_at"],
    )
