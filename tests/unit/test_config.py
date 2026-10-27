"""Unit tests for agent/config.py credential fail-closed behavior.

Validates Requirements 11.5 and 11.6: when the `rs-gen-ai` AWS credential
profile is not configured or does not resolve to valid AWS credentials,
the system must halt before making any AWS resource-creating call and
must emit an error message containing "credential profile could not be
resolved".

These are plain unit tests using `unittest.mock` to simulate an
unconfigured/invalid profile (via `ProfileNotFound` and `ClientError` from
`sts:GetCallerIdentity`). No property-based (Hypothesis) tests live here;
see tests/property/ for those.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError, ProfileNotFound

from agent.config import (
    CredentialResolutionError,
    require_valid_credentials,
    verify_credentials,
)

REQUIRED_ERROR_TEXT = "credential profile could not be resolved"


class TestVerifyCredentialsFailClosed:
    """verify_credentials() must raise CredentialResolutionError, with the
    required message text, when the profile cannot be resolved -- and
    must do so without ever obtaining a resource-creating client."""

    @patch("agent.config.boto3.Session")
    def test_unconfigured_profile_raises_credential_resolution_error(
        self, mock_session_cls: MagicMock
    ) -> None:
        """An unconfigured named profile (ProfileNotFound from boto3) must
        raise CredentialResolutionError."""
        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session
        mock_session.client.side_effect = ProfileNotFound(profile="rs-gen-ai")

        with pytest.raises(CredentialResolutionError) as exc_info:
            verify_credentials("rs-gen-ai", "us-west-2")

        assert REQUIRED_ERROR_TEXT in str(exc_info.value)

    @patch("agent.config.boto3.Session")
    def test_invalid_credentials_raises_credential_resolution_error(
        self, mock_session_cls: MagicMock
    ) -> None:
        """A configured profile whose credentials are invalid/expired
        (ClientError from sts:GetCallerIdentity) must raise
        CredentialResolutionError."""
        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session
        mock_sts_client = MagicMock()
        mock_session.client.return_value = mock_sts_client
        mock_sts_client.get_caller_identity.side_effect = ClientError(
            error_response={
                "Error": {
                    "Code": "InvalidClientTokenId",
                    "Message": "The security token included in the request is invalid",
                }
            },
            operation_name="GetCallerIdentity",
        )

        with pytest.raises(CredentialResolutionError) as exc_info:
            verify_credentials("rs-gen-ai", "us-west-2")

        assert REQUIRED_ERROR_TEXT in str(exc_info.value)

    @patch("agent.config.boto3.Session")
    def test_error_message_contains_required_text_exactly(
        self, mock_session_cls: MagicMock
    ) -> None:
        """Requirement 11.6: the emitted error message must indicate that
        the credential profile could not be resolved."""
        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session
        mock_session.client.side_effect = ProfileNotFound(profile="rs-gen-ai")

        with pytest.raises(CredentialResolutionError) as exc_info:
            verify_credentials("rs-gen-ai")

        message = str(exc_info.value)
        assert REQUIRED_ERROR_TEXT in message
        assert "rs-gen-ai" in message

    @patch("agent.config.boto3.Session")
    def test_valid_credentials_do_not_raise(
        self, mock_session_cls: MagicMock
    ) -> None:
        """Sanity check: a resolvable profile must not raise and must
        return the caller identity ARN."""
        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session
        mock_sts_client = MagicMock()
        mock_session.client.return_value = mock_sts_client
        mock_sts_client.get_caller_identity.return_value = {
            "Arn": "arn:aws:iam::123456789012:user/test-user"
        }

        result = verify_credentials("rs-gen-ai", "us-west-2")

        assert result == "arn:aws:iam::123456789012:user/test-user"


class TestFailClosedHaltsBeforeResourceCreatingCalls:
    """Requirement 11.5: an unresolved/invalid profile must halt before
    any AWS resource-creating call (DynamoDB table creation, Knowledge
    Base provisioning, AgentCore Runtime deployment, etc.)."""

    @patch("agent.config.boto3.Session")
    def test_only_sts_client_is_ever_requested_on_failure(
        self, mock_session_cls: MagicMock
    ) -> None:
        """When credential resolution fails, the only boto3 client
        requested must be 'sts' -- no DynamoDB, Bedrock, S3, or other
        resource-creating client may be obtained."""
        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session
        mock_session.client.side_effect = ProfileNotFound(profile="rs-gen-ai")

        with pytest.raises(CredentialResolutionError):
            verify_credentials("rs-gen-ai", "us-west-2")

        # Assert every client() call made was for "sts" -- proving no
        # resource-creating client (dynamodb, bedrock-agent, s3, etc.)
        # was ever instantiated.
        for call in mock_session.client.call_args_list:
            args, kwargs = call
            requested_service = args[0] if args else kwargs.get("service_name")
            assert requested_service == "sts"

    @patch("agent.config.boto3.Session")
    def test_require_valid_credentials_propagates_failure_without_resources(
        self, mock_session_cls: MagicMock
    ) -> None:
        """require_valid_credentials() -- the entrypoint setup/deployment
        scripts are expected to call first -- must propagate
        CredentialResolutionError and must not have created any
        resource-creating client along the way."""
        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session
        mock_sts_client = MagicMock()
        mock_session.client.return_value = mock_sts_client
        mock_sts_client.get_caller_identity.side_effect = ClientError(
            error_response={
                "Error": {
                    "Code": "ExpiredToken",
                    "Message": "The security token included in the request is expired",
                }
            },
            operation_name="GetCallerIdentity",
        )

        with pytest.raises(CredentialResolutionError) as exc_info:
            require_valid_credentials()

        assert REQUIRED_ERROR_TEXT in str(exc_info.value)
        # Only the sts client should have been requested; no DynamoDB,
        # Bedrock AgentCore, or S3 client (i.e. no resource-creating call)
        # was ever obtained via this session.
        requested_services = {
            (call.args[0] if call.args else call.kwargs.get("service_name"))
            for call in mock_session.client.call_args_list
        }
        assert requested_services == {"sts"}
        for resource_service in ("dynamodb", "bedrock-agent", "s3", "bedrock-agentcore-control"):
            assert resource_service not in requested_services

    @patch("agent.config.boto3.Session")
    def test_require_valid_credentials_defaults_profile_and_region_on_failure(
        self, mock_session_cls: MagicMock
    ) -> None:
        """Even on the failure path, the default profile/region resolution
        (Requirements 11.3/11.4) must still have been applied before the
        fail-closed check halts execution."""
        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session
        mock_session.client.side_effect = ProfileNotFound(profile="rs-gen-ai")

        with pytest.raises(CredentialResolutionError):
            require_valid_credentials(explicit_profile=None, explicit_region=None)

        mock_session_cls.assert_called_once_with(
            profile_name="rs-gen-ai", region_name="us-west-2"
        )
