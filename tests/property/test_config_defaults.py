"""tests/property/test_config_defaults.py

Property-based test for agent/config.py credential/region default
resolution (design.md Correctness Properties, Property 13).
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from agent.config import (
    DEFAULT_PROFILE,
    DEFAULT_REGION,
    resolve_profile,
    resolve_region,
)

# Any optional explicit value a caller might pass for a profile/region
# argument. `None` and `""` both represent "no explicit value supplied"
# per resolve_profile/resolve_region's truthiness check; any other string
# represents an explicitly supplied value.
_optional_explicit_value = st.one_of(st.none(), st.text())


# Feature: network-ops-agent, Property 13: Deployment configuration default resolution
#
# For any setup/deployment script invocation, when no explicit AWS profile
# argument is supplied the resolved profile equals `rs-gen-ai`, and when no
# explicit region argument is supplied the resolved region equals
# `us-west-2`; when an explicit value is supplied for either, the resolved
# value equals the supplied value rather than the default.
#
# Validates: Requirements 11.3, 11.4
@given(
    explicit_profile=_optional_explicit_value,
    explicit_region=_optional_explicit_value,
)
@settings(max_examples=100)
def test_deployment_configuration_default_resolution(
    explicit_profile: str | None,
    explicit_region: str | None,
) -> None:
    resolved_profile = resolve_profile(explicit_profile)
    resolved_region = resolve_region(explicit_region)

    if explicit_profile:
        # An explicit, non-empty profile value is honored over the default.
        assert resolved_profile == explicit_profile
    else:
        # No explicit value (None or "") resolves to the documented default.
        assert resolved_profile == DEFAULT_PROFILE
        assert resolved_profile == "rs-gen-ai"

    if explicit_region:
        # An explicit, non-empty region value is honored over the default.
        assert resolved_region == explicit_region
    else:
        # No explicit value (None or "") resolves to the documented default.
        assert resolved_region == DEFAULT_REGION
        assert resolved_region == "us-west-2"
