"""tests/integration/test_network_ops_agent_end_to_end.py

End-to-end integration test for `agent.network_ops_agent.analyze_case`
(task 16.4), covering the full orchestration path across Requirements
4-10 and 12.

`analyze_case` (task 16.1) already exposes three dependency-injection
seams -- `lookup_case`, `retrieve_content`, and `llm_agent` -- precisely
so it can be exercised end to end without a real MCP subprocess,
DynamoDB table, Bedrock knowledge base, or Bedrock LLM call. This test
injects small fakes for only those three outer boundaries, while letting
every other step run for real: `agent.case_analyzer.determine_validity`,
`agent.case_analyzer.build_case_summary` (and, for the valid scenario,
`extract_root_cause_and_fix`), `agent.email_composer.compose_customer_email`,
and `agent.content_guard.validate_email_content` are all exercised as the
genuine orchestration path (CaseAnalyzer -> EmailComposer -> ContentGuard)
would run them in production.

Three scenarios are covered:

  1. A valid case, where `retrieve_content` returns a `KBRetrievalItem`
     whose `issue_category` matches the case's `network_issue_category`.
     The `KBRetrievalItem.text` fixture is not an arbitrary made-up
     string: it is the *actual* rendered Markdown produced by
     `scripts/generate_runbooks.py`'s `render_runbook`/
     `write_runbook_document` for a real runbook category, so
     `extract_root_cause_and_fix`'s section-heading parser exercises the
     same "## Root Cause Criteria" / "## Remediation Steps" structure the
     real Runbook_Generator produces, and the resulting `case_summary
     .root_cause`/`fix_steps` can be asserted for exact equality against
     that same fixture document's own `root_cause_criteria`/
     `remediation_steps` fields.
  2. An invalid case, where `retrieve_content` returns no matching
     content (an empty Knowledge_Base result), so no runbook category
     overlaps the case's `network_issue_category`.
  3. A not-found case, where `lookup_case` reports `NOT_FOUND` and the
     orchestrator must short-circuit before ever calling
     `retrieve_content`/`llm_agent`.

Fixture shapes (the `CaseRecord` fields, the invalid-case
category/description pairing) mirror what
`scripts/generate_case_data.py`'s `build_case_record` actually produces,
so this test stays grounded in the real system's data shapes rather than
arbitrary values.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.models import (
    AnalysisResult,
    CaseLookupResult,
    CaseRecord,
    ErrorType,
    KBRetrievalItem,
    LookupResultType,
)
from agent.network_ops_agent import analyze_case
from scripts.generate_runbooks import render_runbook, write_runbook_document

# One of the 5 real Runbook_Generator categories (scripts/generate_runbooks
# .py's RUNBOOK_CATEGORIES), used for the valid-case scenario.
_VALID_CATEGORY = "broadband_fixed_line_outage"

# An issue category guaranteed not to correspond to any Runbook_Corpus
# category, mirroring scripts/generate_case_data.py's
# `_INVALID_ISSUE_PROFILES` pairing used for intentionally-invalid
# synthetic cases.
_INVALID_CATEGORY = "Billing Inquiry"
_INVALID_ISSUE_DESCRIPTION = (
    "Customer is disputing a duplicate charge on their most recent "
    "AnyTel invoice."
)


class _StubLLMAgent:
    """A minimal fake matching `email_composer._EmailBodyAgent`'s calling
    interface: records the prompt text it is called with (so a test can
    assert on what `compose_customer_email` actually built and handed to
    the LLM) and returns a fixed, content-guard-clean body string,
    without making any real Bedrock call."""

    def __init__(self, body_text: str) -> None:
        self.body_text = body_text
        self.last_prompt: str | None = None
        self.call_count = 0

    def __call__(self, prompt: str) -> str:
        self.last_prompt = prompt
        self.call_count += 1
        return self.body_text


def _build_case_record(
    *,
    case_id: str,
    network_issue_category: str,
    issue_description: str,
) -> CaseRecord:
    """Build a realistic `CaseRecord` fixture, shaped like what
    `scripts/generate_case_data.py`'s `build_case_record` produces."""
    return CaseRecord(
        case_id=case_id,
        customer_name="Ava Nguyen",
        customer_account_id="AnyTel-004821",
        issue_description=issue_description,
        network_issue_category=network_issue_category,
        service_location_id="LOC-4821",
        status="Open",
        created_at="2025-01-01T08:00:00Z",
        updated_at="2025-01-01T09:30:00Z",
    )


def _render_runbook_fixture(
    output_dir: Path, category: str
) -> tuple[str, list[str], list[str]]:
    """Render and write the real category runbook document (via the
    genuine `scripts/generate_runbooks.py` code path, not a hand-rolled
    string), then read the resulting Markdown back.

    Returns:
        A `(markdown_text, root_cause_criteria, remediation_steps)`
        tuple: `markdown_text` is what a Knowledge_Base retrieval would
        actually return as a `KBRetrievalItem.text` for this category;
        `root_cause_criteria`/`remediation_steps` are the source
        `RunbookDocument`'s own fields, used later to assert that
        `extract_root_cause_and_fix` parsed the rendered Markdown back
        into exactly the same content it was rendered from.
    """
    document = render_runbook(category)
    markdown_path, _metadata_path = write_runbook_document(
        document, output_dir=output_dir
    )
    markdown_text = markdown_path.read_text(encoding="utf-8")
    return markdown_text, document.root_cause_criteria, document.remediation_steps


def test_valid_case_end_to_end(tmp_path: Path) -> None:
    """A valid case (Knowledge_Base retrieval matches the case's
    network_issue_category) flows through determine_validity ->
    build_case_summary -> compose_customer_email -> validate_email_content
    end to end, producing a successful `AnalysisResult` whose
    `case_summary`/`customer_email` are fully populated and consistent
    with the fixture runbook content."""
    markdown_text, root_cause_criteria, remediation_steps = _render_runbook_fixture(
        tmp_path, _VALID_CATEGORY
    )

    case = _build_case_record(
        case_id="CASE-VALID0001",
        network_issue_category=_VALID_CATEGORY,
        issue_description=(
            f"Customer at service location LOC-4821 is reporting a "
            f"{_VALID_CATEGORY} issue with their AnyTel service: complete "
            "loss of service since this morning."
        ),
    )

    source_uri = f"s3://anytel-runbooks/{_VALID_CATEGORY}.md"
    retrieval_item = KBRetrievalItem(
        text=markdown_text,
        score=0.87,
        source_uri=source_uri,
        issue_category=_VALID_CATEGORY,
    )

    stub_llm = _StubLLMAgent(
        "We're sorry to hear about the trouble with your internet "
        "connection. Our review found the issue was with the equipment "
        "serving your address, and our team has restored the connection "
        "and confirmed it is working normally again."
    )

    def fake_lookup_case(requested_case_id: str) -> CaseLookupResult:
        assert requested_case_id == case.case_id
        return CaseLookupResult(result_type=LookupResultType.FOUND, case=case)

    def fake_retrieve_content(query: str) -> list[KBRetrievalItem]:
        # Confirms the orchestrator built its retrieval query from the
        # real case's issue description and category (Requirement 5.1),
        # rather than some other value.
        assert case.issue_description in query
        assert case.network_issue_category in query
        return [retrieval_item]

    result = analyze_case(
        case.case_id,
        lookup_case=fake_lookup_case,
        retrieve_content=fake_retrieve_content,
        llm_agent=stub_llm,
    )

    assert isinstance(result, AnalysisResult)
    assert result.success is True
    assert result.error_type is None
    assert result.error_message is None

    summary = result.case_summary
    assert summary is not None
    assert summary.case_id == case.case_id
    assert summary.customer_name == case.customer_name
    assert summary.issue_description == case.issue_description
    assert summary.validity == "valid"
    # The real extract_root_cause_and_fix output, derived from the real
    # rendered Markdown, must equal the source RunbookDocument's own
    # fields exactly.
    assert summary.root_cause == "; ".join(root_cause_criteria)
    assert summary.fix_steps == remediation_steps
    assert summary.runbook_reference == source_uri

    email = result.customer_email
    assert email is not None
    assert email.salutation == f"Dear {case.customer_name},"
    assert email.body == stub_llm.body_text
    assert stub_llm.body_text in email.full_text
    assert email.full_text.startswith(email.salutation)
    assert email.full_text.endswith(email.signature)
    # The clean stub body passed the content guard on the first attempt,
    # so no stricter-prompt retry should have occurred.
    assert stub_llm.call_count == 1


def test_invalid_case_end_to_end() -> None:
    """An invalid case (Knowledge_Base retrieval returns no content whose
    issue_category matches the case's network_issue_category) is still a
    *successfully analyzed* case: `AnalysisResult.success` is True, but
    `case_summary.validity == "invalid"` and root_cause/fix_steps/
    runbook_reference are all None, while a polite (non-root-cause)
    customer_email is still composed."""
    case = _build_case_record(
        case_id="CASE-INVALID0001",
        network_issue_category=_INVALID_CATEGORY,
        issue_description=_INVALID_ISSUE_DESCRIPTION,
    )

    def fake_lookup_case(requested_case_id: str) -> CaseLookupResult:
        assert requested_case_id == case.case_id
        return CaseLookupResult(result_type=LookupResultType.FOUND, case=case)

    def fake_retrieve_content(query: str) -> list[KBRetrievalItem]:
        # An empty Knowledge_Base retrieval result: no runbook category
        # can possibly match this case's network_issue_category.
        return []

    stub_llm = _StubLLMAgent(
        "Thank you for reaching out. We reviewed your account and did "
        "not find a network issue tied to this report. A member of our "
        "billing team will follow up with you directly about this "
        "matter."
    )

    result = analyze_case(
        case.case_id,
        lookup_case=fake_lookup_case,
        retrieve_content=fake_retrieve_content,
        llm_agent=stub_llm,
    )

    assert result.success is True
    assert result.error_type is None
    assert result.error_message is None

    summary = result.case_summary
    assert summary is not None
    assert summary.case_id == case.case_id
    assert summary.validity == "invalid"
    assert summary.root_cause is None
    assert summary.fix_steps is None
    assert summary.runbook_reference is None

    email = result.customer_email
    assert email is not None
    assert email.body == stub_llm.body_text
    # The prompt built for an invalid case must never reference root
    # cause/fix-step content (Requirement 8.3) and must instead ask for a
    # polite acknowledgment that no network fault was found. (The prompt
    # is allowed to *instruct* the model not to mention a root cause --
    # see agent/email_composer.py's invalid-case prompt -- it just must
    # never supply an actual root-cause finding or fix-step list, i.e.
    # the "confirmed root cause"/numbered-fix-step prompt fragments used
    # only in the valid-case branch of _build_email_prompt.)
    assert stub_llm.last_prompt is not None
    assert "no network fault" in stub_llm.last_prompt
    assert "confirmed root cause" not in stub_llm.last_prompt.lower()
    assert "steps that resolve the issue" not in stub_llm.last_prompt.lower()
    assert stub_llm.call_count == 1


def test_not_found_case_end_to_end() -> None:
    """A case_id with no matching Case_Record short-circuits before
    Knowledge_Base retrieval or email composition are ever attempted,
    returning a failure `AnalysisResult` with no `case_summary`/
    `customer_email`."""

    def fake_lookup_case(requested_case_id: str) -> CaseLookupResult:
        assert requested_case_id == "CASE-DOES-NOT-EXIST"
        return CaseLookupResult(result_type=LookupResultType.NOT_FOUND, case=None)

    def fake_retrieve_content(query: str) -> list[KBRetrievalItem]:
        pytest.fail(
            "retrieve_content must not be called when case lookup "
            "reports NOT_FOUND: the orchestrator must short-circuit."
        )

    def fake_llm_agent(prompt: str) -> str:
        pytest.fail(
            "llm_agent must not be called when case lookup reports "
            "NOT_FOUND: the orchestrator must short-circuit."
        )

    result = analyze_case(
        "CASE-DOES-NOT-EXIST",
        lookup_case=fake_lookup_case,
        retrieve_content=fake_retrieve_content,
        llm_agent=fake_llm_agent,
    )

    assert result.success is False
    assert result.error_type == ErrorType.CASE_NOT_FOUND
    assert result.case_summary is None
    assert result.customer_email is None
