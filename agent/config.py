"""agent/config.py: AWS credential profile and region default resolution.

Implements Requirement 11 (AWS Deployment Configuration): setup/deployment
scripts default to the `rs-gen-ai` named AWS profile and the `us-west-2`
AWS Region unless an explicit value is supplied by the caller, and fail
closed (no AWS resource-creating call) with an explicit error message when
the resolved profile cannot be resolved to valid AWS credentials.

This module has no dependency on `agent/models.py` or any other `agent/*`
module; it only depends on `boto3`/`botocore`.
"""

from __future__ import annotations

import boto3
from botocore.exceptions import BotoCoreError, ClientError, ProfileNotFound

# Requirement 11.3 / 11.4: defaults used when no explicit value is supplied.
DEFAULT_PROFILE = "rs-gen-ai"
DEFAULT_REGION = "us-west-2"


class CredentialResolutionError(RuntimeError):
    """Raised when the resolved AWS credential profile could not be
    resolved to valid AWS credentials.

    Callers MUST treat this exception as a fail-closed signal: no AWS
    resource-creating call (DynamoDB table, Knowledge Base, AgentCore
    Runtime, etc.) may be made once this has been raised (Requirement
    11.5). The exception message always contains the phrase "credential
    profile could not be resolved" (Requirement 11.6).
    """


def resolve_profile(explicit: str | None = None) -> str:
    """Resolve the AWS named profile to use for setup/deployment scripts.

    Honors an explicitly supplied, non-empty value over the default.
    Otherwise resolves to `rs-gen-ai` (Requirement 11.3).
    """
    if explicit:
        return explicit
    return DEFAULT_PROFILE


def resolve_region(explicit: str | None = None) -> str:
    """Resolve the AWS Region to use for setup/deployment scripts.

    Honors an explicitly supplied, non-empty value over the default.
    Otherwise resolves to `us-west-2` (Requirement 11.4).
    """
    if explicit:
        return explicit
    return DEFAULT_REGION


def verify_credentials(profile: str, region: str | None = None) -> str:
    """Fail-closed credential check for a resolved profile.

    Calls `sts:GetCallerIdentity` under the given named profile. This MUST
    be invoked, and MUST succeed, before any AWS resource-creating call is
    made by a setup/deployment script (Requirement 11.5).

    Returns the caller identity ARN on success.

    Raises:
        CredentialResolutionError: if `profile` is not configured (e.g. it
            does not exist in the local AWS config/credentials files) or
            does not resolve to valid AWS credentials (e.g. expired,
            missing, or invalid credentials; unreachable STS endpoint).
            The message includes the required "credential profile could
            not be resolved" text (Requirement 11.6).
    """
    try:
        session = boto3.Session(profile_name=profile, region_name=region)
        sts_client = session.client("sts")
        identity = sts_client.get_caller_identity()
    except (ProfileNotFound, ClientError, BotoCoreError) as exc:
        raise CredentialResolutionError(
            f"credential profile could not be resolved: AWS profile "
            f"'{profile}' is not configured or does not resolve to valid "
            f"AWS credentials ({exc})"
        ) from exc
    return identity["Arn"]


def require_valid_credentials(
    explicit_profile: str | None = None,
    explicit_region: str | None = None,
) -> tuple[str, str]:
    """Resolve profile/region defaults and perform the fail-closed
    credential check in a single call.

    Intended to be the first call made by any setup/deployment script,
    before any AWS resource-creating call (DynamoDB table, Knowledge Base,
    S3 bucket, AgentCore Runtime, etc.).

    Returns:
        A `(profile, region)` tuple of the resolved values on success.

    Raises:
        CredentialResolutionError: propagated from `verify_credentials`
            when the resolved profile cannot be resolved to valid AWS
            credentials. Callers MUST halt without creating any resources
            when this is raised.
    """
    profile = resolve_profile(explicit_profile)
    region = resolve_region(explicit_region)
    verify_credentials(profile, region)
    return profile, region
