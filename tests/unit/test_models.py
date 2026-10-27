"""Unit tests for agent/models.py: construction and immutability.

Covers Requirement 7.3 (Case_Summary expressed as individually labeled,
immutable fields) by verifying every shared dataclass constructs correctly
with valid fields and rejects attribute mutation (frozen=True), and that
the two enums expose their expected members/values.
"""

import dataclasses

import pytest

from agent.models import (
    AnalysisResult,
    CaseLookupResult,
    CaseRecord,
    CaseSummary,
    CustomerEmail,
    ErrorType,
    KBRetrievalItem,
    LookupResultType,
    RunbookDocument,
    ValidityDetermination,
)


def test_case_record_constructs_and_is_frozen():
    record = CaseRecord(
        case_id="CASE-1001",
        customer_name="Jane Doe",
        customer_account_id="AnyTel-000123",
        issue_description="No internet connection since this morning.",
        network_issue_category="broadband_outage",
        service_location_id="LOC-42",
        status="Open",
        created_at="2024-01-01T10:00:00Z",
        updated_at="2024-01-01T10:30:00Z",
    )

    assert record.case_id == "CASE-1001"
    assert record.customer_name == "Jane Doe"
    assert record.customer_account_id == "AnyTel-000123"
    assert record.issue_description == "No internet connection since this morning."
    assert record.network_issue_category == "broadband_outage"
    assert record.service_location_id == "LOC-42"
    assert record.status == "Open"
    assert record.created_at == "2024-01-01T10:00:00Z"
    assert record.updated_at == "2024-01-01T10:30:00Z"

    with pytest.raises(dataclasses.FrozenInstanceError):
        record.status = "Closed"


def test_runbook_document_constructs_and_is_frozen():
    doc = RunbookDocument(
        category="broadband_outage",
        title="Broadband Connectivity Outage Troubleshooting",
        description="Guidance for diagnosing and resolving broadband outages.",
        diagnostic_steps=["Check line status", "Ping gateway", "Check for outage alerts"],
        root_cause_criteria=["Line status DOWN", "Outage alert active in area"],
        remediation_steps=["Restart modem", "Escalate to field technician", "Confirm restoration"],
    )

    assert doc.category == "broadband_outage"
    assert doc.title == "Broadband Connectivity Outage Troubleshooting"
    assert doc.description == "Guidance for diagnosing and resolving broadband outages."
    assert doc.diagnostic_steps == ["Check line status", "Ping gateway", "Check for outage alerts"]
    assert doc.root_cause_criteria == ["Line status DOWN", "Outage alert active in area"]
    assert doc.remediation_steps == [
        "Restart modem",
        "Escalate to field technician",
        "Confirm restoration",
    ]

    with pytest.raises(dataclasses.FrozenInstanceError):
        doc.title = "Different Title"


def test_kb_retrieval_item_constructs_and_is_frozen():
    item = KBRetrievalItem(
        text="Broadband outages are typically caused by upstream line faults.",
        score=0.87,
        source_uri="s3://runbooks-bucket/broadband_outage.md",
        issue_category="broadband_outage",
    )

    assert item.text == "Broadband outages are typically caused by upstream line faults."
    assert item.score == 0.87
    assert item.source_uri == "s3://runbooks-bucket/broadband_outage.md"
    assert item.issue_category == "broadband_outage"

    with pytest.raises(dataclasses.FrozenInstanceError):
        item.score = 0.5


def test_kb_retrieval_item_allows_none_issue_category():
    item = KBRetrievalItem(
        text="Some retrieved text with no sidecar metadata.",
        score=0.42,
        source_uri="s3://runbooks-bucket/unclassified.md",
        issue_category=None,
    )

    assert item.issue_category is None


def test_lookup_result_type_enum_members():
    assert LookupResultType.FOUND.value == "FOUND"
    assert LookupResultType.NOT_FOUND.value == "NOT_FOUND"
    assert LookupResultType.INVALID_INPUT.value == "INVALID_INPUT"
    assert {member.name for member in LookupResultType} == {
        "FOUND",
        "NOT_FOUND",
        "INVALID_INPUT",
    }


def test_case_lookup_result_constructs_and_is_frozen():
    record = CaseRecord(
        case_id="CASE-1001",
        customer_name="Jane Doe",
        customer_account_id="AnyTel-000123",
        issue_description="No internet connection since this morning.",
        network_issue_category="broadband_outage",
        service_location_id="LOC-42",
        status="Open",
        created_at="2024-01-01T10:00:00Z",
        updated_at="2024-01-01T10:30:00Z",
    )
    result = CaseLookupResult(result_type=LookupResultType.FOUND, case=record)

    assert result.result_type is LookupResultType.FOUND
    assert result.case is record

    with pytest.raises(dataclasses.FrozenInstanceError):
        result.result_type = LookupResultType.NOT_FOUND


def test_case_lookup_result_allows_none_case():
    result = CaseLookupResult(result_type=LookupResultType.NOT_FOUND, case=None)

    assert result.result_type is LookupResultType.NOT_FOUND
    assert result.case is None


def test_validity_determination_constructs_and_is_frozen():
    matched_item = KBRetrievalItem(
        text="Broadband outages are typically caused by upstream line faults.",
        score=0.87,
        source_uri="s3://runbooks-bucket/broadband_outage.md",
        issue_category="broadband_outage",
    )
    determination = ValidityDetermination(
        valid=True,
        matched_item=matched_item,
        rationale=(
            "network_issue_category 'broadband_outage' matched a runbook "
            "with the same issue category."
        ),
    )

    assert determination.valid is True
    assert determination.matched_item is matched_item
    assert determination.rationale == (
        "network_issue_category 'broadband_outage' matched a runbook "
        "with the same issue category."
    )

    with pytest.raises(dataclasses.FrozenInstanceError):
        determination.valid = False


def test_case_summary_constructs_and_is_frozen():
    summary = CaseSummary(
        case_id="CASE-1001",
        customer_name="Jane Doe",
        issue_description="No internet connection since this morning.",
        validity="valid",
        rationale="Matched broadband_outage runbook category.",
        root_cause="Upstream line fault detected in the service area.",
        fix_steps=["Restart modem", "Escalate to field technician", "Confirm restoration"],
        runbook_reference="Broadband Connectivity Outage Troubleshooting",
    )

    assert summary.case_id == "CASE-1001"
    assert summary.customer_name == "Jane Doe"
    assert summary.issue_description == "No internet connection since this morning."
    assert summary.validity == "valid"
    assert summary.rationale == "Matched broadband_outage runbook category."
    assert summary.root_cause == "Upstream line fault detected in the service area."
    assert summary.fix_steps == [
        "Restart modem",
        "Escalate to field technician",
        "Confirm restoration",
    ]
    assert summary.runbook_reference == "Broadband Connectivity Outage Troubleshooting"

    with pytest.raises(dataclasses.FrozenInstanceError):
        summary.validity = "invalid"


def test_case_summary_allows_none_fields_for_invalid_case():
    summary = CaseSummary(
        case_id="CASE-2002",
        customer_name="John Smith",
        issue_description="Billing question, not a network issue.",
        validity="invalid",
        rationale="No runbook category matched network_issue_category 'billing'.",
        root_cause=None,
        fix_steps=None,
        runbook_reference=None,
    )

    assert summary.validity == "invalid"
    assert summary.root_cause is None
    assert summary.fix_steps is None
    assert summary.runbook_reference is None


def test_customer_email_constructs_and_is_frozen():
    email = CustomerEmail(
        salutation="Dear Jane Doe,",
        body="We found the cause of your outage and resolved it.",
        signature="Sincerely,\nThe AnyTel Support Team",
        full_text=(
            "Dear Jane Doe,\n"
            "We found the cause of your outage and resolved it.\n"
            "Sincerely,\nThe AnyTel Support Team"
        ),
    )

    assert email.salutation == "Dear Jane Doe,"
    assert email.body == "We found the cause of your outage and resolved it."
    assert email.signature == "Sincerely,\nThe AnyTel Support Team"
    assert email.full_text == (
        "Dear Jane Doe,\n"
        "We found the cause of your outage and resolved it.\n"
        "Sincerely,\nThe AnyTel Support Team"
    )

    with pytest.raises(dataclasses.FrozenInstanceError):
        email.body = "Different body"


def test_error_type_enum_members():
    assert ErrorType.INVALID_INPUT.value == "invalid_input"
    assert ErrorType.CASE_NOT_FOUND.value == "case_not_found"
    assert ErrorType.LOOKUP_FAILURE.value == "lookup_failure"
    assert ErrorType.RETRIEVAL_FAILURE.value == "retrieval_failure"
    assert ErrorType.ANALYSIS_FAILURE.value == "analysis_failure"
    assert {member.name for member in ErrorType} == {
        "INVALID_INPUT",
        "CASE_NOT_FOUND",
        "LOOKUP_FAILURE",
        "RETRIEVAL_FAILURE",
        "ANALYSIS_FAILURE",
    }


def test_analysis_result_constructs_success_and_is_frozen():
    summary = CaseSummary(
        case_id="CASE-1001",
        customer_name="Jane Doe",
        issue_description="No internet connection since this morning.",
        validity="valid",
        rationale="Matched broadband_outage runbook category.",
        root_cause="Upstream line fault detected in the service area.",
        fix_steps=["Restart modem"],
        runbook_reference="Broadband Connectivity Outage Troubleshooting",
    )
    email = CustomerEmail(
        salutation="Dear Jane Doe,",
        body="We found the cause of your outage and resolved it.",
        signature="Sincerely,\nThe AnyTel Support Team",
        full_text="Dear Jane Doe,\nWe found the cause of your outage and resolved it.\nSincerely,\nThe AnyTel Support Team",
    )
    result = AnalysisResult(success=True, case_summary=summary, customer_email=email)

    assert result.success is True
    assert result.case_summary is summary
    assert result.customer_email is email
    assert result.error_type is None
    assert result.error_message is None

    with pytest.raises(dataclasses.FrozenInstanceError):
        result.success = False


def test_analysis_result_constructs_failure_with_defaults():
    result = AnalysisResult(
        success=False,
        error_type=ErrorType.CASE_NOT_FOUND,
        error_message="Case CASE-9999 was not found.",
    )

    assert result.success is False
    assert result.case_summary is None
    assert result.customer_email is None
    assert result.error_type is ErrorType.CASE_NOT_FOUND
    assert result.error_message == "Case CASE-9999 was not found."

    with pytest.raises(dataclasses.FrozenInstanceError):
        result.error_message = "Different message"
