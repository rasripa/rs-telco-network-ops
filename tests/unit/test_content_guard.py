"""Unit tests for agent/content_guard.py: specific denylist entries.

Covers task 14.5 (Requirement 8.4: "free of profanity, insults, or
negative language directed at the customer, and free of internal
technical identifiers, root cause codes, and internal system or
component names"). Where the parallel property test
(tests/property/test_content_guard.py, task 14.4) exercises the general
denylist property across many generated inputs, these tests pin down
concrete, specific examples: one literal entry from
`PROFANITY_INSULT_DENYLIST`, one literal entry from
`INTERNAL_IDENTIFIER_DENYLIST`, and one internal-identifier-*shaped*
regex pattern match (a runbook `s3://` URI and an `ERR-`/`RC-` style
error code), plus a clean-content sanity check.
"""

from agent.content_guard import (
    INTERNAL_IDENTIFIER_DENYLIST,
    PROFANITY_INSULT_DENYLIST,
    validate_email_content,
)
from agent.models import CustomerEmail


def _make_email(body: str) -> CustomerEmail:
    """Build a minimal `CustomerEmail` whose `full_text` is exactly the
    given body, so tests can control precisely what text the guard
    scans."""
    salutation = "Dear Jane Doe,"
    signature = "Sincerely,\nThe AnyTel Support Team"
    return CustomerEmail(
        salutation=salutation,
        body=body,
        signature=signature,
        full_text=f"{salutation}\n{body}\n{signature}",
    )


def test_specific_profanity_term_is_caught():
    """A concrete profanity/insult term from PROFANITY_INSULT_DENYLIST
    ("stupid"), embedded in otherwise normal sentence text, is flagged."""
    assert "stupid" in PROFANITY_INSULT_DENYLIST

    email = _make_email(
        "We understand this has been a stupid situation for you, but "
        "rest assured our team resolved the underlying issue."
    )

    result = validate_email_content(email)

    assert result.passed is False
    assert "stupid" in result.violations


def test_specific_internal_identifier_is_caught():
    """A concrete internal identifier from INTERNAL_IDENTIFIER_DENYLIST
    ("DynamoDB"), embedded in otherwise normal sentence text, is
    flagged."""
    assert "DynamoDB" in INTERNAL_IDENTIFIER_DENYLIST

    email = _make_email(
        "Our engineers traced the outage after querying DynamoDB for "
        "your account records and confirmed service has been restored."
    )

    result = validate_email_content(email)

    assert result.passed is False
    assert "DynamoDB" in result.violations


def test_runbook_s3_uri_pattern_is_caught():
    """An internal-identifier-*shaped* pattern match: a runbook document's
    `s3://` object URI leaking into the email text is flagged, with the
    matched URI text present in violations."""
    uri = "s3://anytel-runbooks/broadband_fixed_line_outage.md"
    email = _make_email(
        f"Per the internal reference at {uri}, the fault has been cleared."
    )

    result = validate_email_content(email)

    assert result.passed is False
    assert uri in result.violations


def test_error_code_pattern_is_caught():
    """An internal-identifier-*shaped* pattern match: a raw error-code-like
    token ("ERR-4042") leaking into the email text is flagged, with the
    matched code text present in violations."""
    email = _make_email(
        "The issue was logged internally under code ERR-4042 before our "
        "technicians resolved it."
    )

    result = validate_email_content(email)

    assert result.passed is False
    assert "ERR-4042" in result.violations


def test_clean_content_passes_with_no_violations():
    """Sanity check: clean, non-technical, non-denylisted customer-facing
    text passes with an empty violations list."""
    email = _make_email(
        "Thank you for your patience. Your internet service has been "
        "fully restored, and no further action is needed on your part."
    )

    result = validate_email_content(email)

    assert result.passed is True
    assert result.violations == []
