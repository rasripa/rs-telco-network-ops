"""AppTest-based UI tests for ui/app.py (task 18.4).

Validates Requirements 9.4, 9.6, 10.1, 10.2, 10.4 by driving `ui/app.py`
through Streamlit's `AppTest` harness (`streamlit.testing.v1.AppTest`),
which runs the real script in-process without a browser or a running
Streamlit server.

`ui.agent_client.invoke` is monkeypatched via `unittest.mock.patch` in
every test so no real AWS call (`bedrock-agentcore:InvokeAgentRuntime`)
is ever made. `ui/app.py` calls `agent_client.invoke(...)` via attribute
access on the imported `ui.agent_client` module (see that module's
docstring), so patching `"ui.agent_client.invoke"` is observed correctly
by `ui/app.py`.

Covers:

1. Required-field rejection (Requirement 9.4): submitting an empty or
   whitespace-only `case_id` displays `ui.app.REQUIRED_FIELD_MESSAGE` and
   never calls the mocked `agent_client.invoke`.
2. In-flight re-submission blocking (Requirement 9.6): `_handle_submit`'s
   internal guard is exercised directly (with `is_processing` pre-set to
   `True`) to confirm `agent_client.invoke` is not called a second time,
   and separately the rendered submit button's `disabled` state is
   confirmed to reflect `is_processing` in the full-script run.
3-6. Correct rendering for a valid case, an invalid case,
   `CASE_NOT_FOUND`, and a generic failure (Requirements 10.1, 10.2,
   10.4).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from agent.models import AnalysisResult, CaseSummary, CustomerEmail, ErrorType
from ui.app import (
    CASE_NOT_FOUND_MESSAGE,
    CASE_SUMMARY_HEADING,
    CUSTOMER_EMAIL_HEADING,
    GENERIC_ERROR_MESSAGE,
    REQUIRED_FIELD_MESSAGE,
)

# `AppTest.from_file` needs a path resolvable from the test's cwd. An
# absolute path (derived from this test file's own location) is used so
# the test works regardless of the directory `pytest` is invoked from.
APP_PATH = str(Path(__file__).resolve().parents[2] / "ui" / "app.py")


def _valid_case_summary() -> CaseSummary:
    return CaseSummary(
        case_id="CASE-1001",
        customer_name="Jane Doe",
        issue_description="No internet connection since this morning.",
        validity="valid",
        rationale=(
            "network_issue_category 'broadband_outage' matched a runbook "
            "with the same issue category."
        ),
        root_cause="Upstream line fault detected in the service area.",
        fix_steps=["Restart the modem.", "Escalate to a field technician."],
        runbook_reference="Broadband Connectivity Outage Troubleshooting",
    )


def _valid_customer_email() -> CustomerEmail:
    return CustomerEmail(
        salutation="Dear Jane Doe,",
        body="We found the cause of your outage and resolved it.",
        signature="Sincerely,\nThe AnyTel Support Team",
        full_text=(
            "Dear Jane Doe,\n"
            "We found the cause of your outage and resolved it.\n"
            "Sincerely,\nThe AnyTel Support Team"
        ),
    )


def _invalid_case_summary() -> CaseSummary:
    return CaseSummary(
        case_id="CASE-2002",
        customer_name="John Smith",
        issue_description="Billing question, not a network issue.",
        validity="invalid",
        rationale="No runbook category matched network_issue_category 'billing'.",
        root_cause=None,
        fix_steps=None,
        runbook_reference=None,
    )


class TestRequiredFieldRejection:
    """Requirement 9.4: an empty or whitespace-only case_id is rejected
    client-side, without ever invoking the agent."""

    def test_empty_case_id_shows_required_field_message(self) -> None:
        with patch("ui.agent_client.invoke") as mock_invoke:
            at = AppTest.from_file(APP_PATH)
            at.run()

            at.text_input(key="case_id_input").set_value("")
            at.button[0].click()
            at.run()

            assert at.exception == []
            assert REQUIRED_FIELD_MESSAGE in [e.value for e in at.error]
            mock_invoke.assert_not_called()

    def test_whitespace_only_case_id_shows_required_field_message(self) -> None:
        with patch("ui.agent_client.invoke") as mock_invoke:
            at = AppTest.from_file(APP_PATH)
            at.run()

            at.text_input(key="case_id_input").set_value("   \t  ")
            at.button[0].click()
            at.run()

            assert at.exception == []
            assert REQUIRED_FIELD_MESSAGE in [e.value for e in at.error]
            mock_invoke.assert_not_called()

    def test_required_field_rejection_shows_no_result_sections(self) -> None:
        with patch("ui.agent_client.invoke") as mock_invoke:
            at = AppTest.from_file(APP_PATH)
            at.run()

            at.text_input(key="case_id_input").set_value("")
            at.button[0].click()
            at.run()

            assert at.header == []
            mock_invoke.assert_not_called()


class TestInFlightResubmissionBlocking:
    """Requirement 9.6: the Streamlit_UI must prevent invocation of the
    Network_Ops_Agent workflow for any additional case_id submission
    while a previous submission is still in flight.

    AppTest's run model executes `_handle_submit` synchronously to
    completion within a single script run, so there is no real
    "in-flight" window to submit into from the outside. Two
    complementary checks are used instead, both exercising the same
    `is_processing` guard that `_handle_submit` and `main()` rely on:

    1. Directly calling `_handle_submit` with `is_processing` pre-set to
       `True` (simulating a submission arriving while another is still
       in flight) and confirming `agent_client.invoke` is never called.
    2. Confirming that once `is_processing` is `True`, the rendered
       submit button (and text input) are `disabled`, which is what
       prevents a real browser user from triggering a second submission
       in the first place.
    """

    def test_handle_submit_guard_blocks_invoke_while_processing(self) -> None:
        """Directly exercises `_handle_submit`'s internal guard: with
        `is_processing` already `True`, calling `_handle_submit` again
        must return without calling `agent_client.invoke`."""

        def script() -> None:
            import streamlit as st

            from ui import app as ui_app

            ui_app._init_session_state()
            st.session_state.is_processing = True
            ui_app._handle_submit("CASE-1001")
            st.write("guard-check-complete")

        with patch("ui.agent_client.invoke") as mock_invoke:
            at = AppTest.from_function(script)
            at.run()

            assert at.exception == []
            mock_invoke.assert_not_called()
            # is_processing must remain True: the guard returned early
            # rather than running the normal set-True/invoke/set-False
            # sequence a fresh, non-blocked submission would perform.
            assert at.session_state["is_processing"] is True
            assert "guard-check-complete" in [m.value for m in at.markdown]

    def test_submit_button_and_input_disabled_while_processing(self) -> None:
        """While `is_processing` is `True`, the rendered submit button
        and case_id text input must both be disabled, which is what
        prevents a real browser user from submitting an additional
        case_id until the in-flight request completes."""
        with patch("ui.agent_client.invoke") as mock_invoke:
            at = AppTest.from_file(APP_PATH)
            at.run()

            at.session_state["is_processing"] = True
            at.run()

            assert at.exception == []
            assert at.button[0].disabled is True
            assert at.text_input(key="case_id_input").disabled is True
            mock_invoke.assert_not_called()


class TestValidCaseRendering:
    """Correct rendering for a valid case (Requirements 10.1, 10.2)."""

    def test_valid_case_renders_case_summary_and_customer_email(self) -> None:
        result = AnalysisResult(
            success=True,
            case_summary=_valid_case_summary(),
            customer_email=_valid_customer_email(),
        )

        with patch("ui.agent_client.invoke", return_value=result) as mock_invoke:
            at = AppTest.from_file(APP_PATH)
            at.run()

            at.text_input(key="case_id_input").set_value("CASE-1001")
            at.button[0].click()
            at.run()

            assert at.exception == []
            mock_invoke.assert_called_once_with("CASE-1001")

            headers = [h.value for h in at.header]
            assert CASE_SUMMARY_HEADING in headers
            assert CUSTOMER_EMAIL_HEADING in headers

            rendered_text = [m.value for m in at.markdown]
            assert "CASE-1001" in rendered_text
            assert "Jane Doe" in rendered_text
            assert "Upstream line fault detected in the service area." in rendered_text
            assert any("Restart the modem." in text for text in rendered_text)
            assert any("Escalate to a field technician." in text for text in rendered_text)
            assert "Dear Jane Doe," in rendered_text
            assert "We found the cause of your outage and resolved it." in rendered_text


class TestInvalidCaseRendering:
    """Correct rendering for an invalid case: Case Summary renders, but
    no Root Cause/Fix Steps/Runbook Reference subheader appears
    (Requirement 5.6's UI-facing counterpart, surfaced via Requirement
    10.1)."""

    def test_invalid_case_renders_summary_without_resolution_fields(self) -> None:
        result = AnalysisResult(
            success=True,
            case_summary=_invalid_case_summary(),
            customer_email=None,
        )

        with patch("ui.agent_client.invoke", return_value=result) as mock_invoke:
            at = AppTest.from_file(APP_PATH)
            at.run()

            at.text_input(key="case_id_input").set_value("CASE-2002")
            at.button[0].click()
            at.run()

            assert at.exception == []
            mock_invoke.assert_called_once_with("CASE-2002")

            headers = [h.value for h in at.header]
            assert CASE_SUMMARY_HEADING in headers
            assert CUSTOMER_EMAIL_HEADING not in headers

            subheaders = [s.value for s in at.subheader]
            assert "Root Cause" not in subheaders
            assert "Fix Steps" not in subheaders
            assert "Runbook Reference" not in subheaders

            rendered_text = [m.value for m in at.markdown]
            assert "CASE-2002" in rendered_text
            assert "John Smith" in rendered_text
            assert "invalid" in rendered_text


class TestCaseNotFoundRendering:
    """CASE_NOT_FOUND: the not-found message is displayed and no Case
    Summary section appears (Requirement 9.5, surfaced per Requirement
    10.4's "no partial/fabricated result" intent)."""

    def test_case_not_found_shows_message_and_no_result_sections(self) -> None:
        result = AnalysisResult(
            success=False,
            error_type=ErrorType.CASE_NOT_FOUND,
            error_message="No item found for case_id 'CASE-9999'.",
        )

        with patch("ui.agent_client.invoke", return_value=result) as mock_invoke:
            at = AppTest.from_file(APP_PATH)
            at.run()

            at.text_input(key="case_id_input").set_value("CASE-9999")
            at.button[0].click()
            at.run()

            assert at.exception == []
            mock_invoke.assert_called_once_with("CASE-9999")

            assert CASE_NOT_FOUND_MESSAGE in [w.value for w in at.warning]
            assert at.header == []
            assert at.error == []


class TestGenericFailureRendering:
    """Generic failure: the generic error banner is displayed and the raw
    internal `error_message` text never appears anywhere in the rendered
    page (Requirement 10.4)."""

    def test_generic_failure_shows_generic_message_and_hides_internal_detail(
        self,
    ) -> None:
        internal_error_text = "SECRET_INTERNAL_TRACE_boto3_ConnectTimeoutError_xyz123"
        result = AnalysisResult(
            success=False,
            error_type=ErrorType.LOOKUP_FAILURE,
            error_message=internal_error_text,
        )

        with patch("ui.agent_client.invoke", return_value=result) as mock_invoke:
            at = AppTest.from_file(APP_PATH)
            at.run()

            at.text_input(key="case_id_input").set_value("CASE-1234")
            at.button[0].click()
            at.run()

            assert at.exception == []
            mock_invoke.assert_called_once_with("CASE-1234")

            assert GENERIC_ERROR_MESSAGE in [e.value for e in at.error]
            assert at.header == []

            # The raw internal error_message text must never leak into
            # any rendered element on the page.
            rendered_strings: list[str] = []
            for element_type in ("markdown", "text", "error", "warning", "header", "subheader"):
                rendered_strings.extend(str(el.value) for el in getattr(at, element_type))
            assert all(internal_error_text not in text for text in rendered_strings)

    def test_generic_failure_for_other_error_types_shows_generic_message(self) -> None:
        """Sanity check that RETRIEVAL_FAILURE and ANALYSIS_FAILURE (the
        other non-CASE_NOT_FOUND error types) also render the same
        generic banner rather than CASE_NOT_FOUND_MESSAGE."""
        for error_type in (ErrorType.RETRIEVAL_FAILURE, ErrorType.ANALYSIS_FAILURE):
            result = AnalysisResult(
                success=False,
                error_type=error_type,
                error_message="internal detail that must not leak",
            )
            with patch("ui.agent_client.invoke", return_value=result):
                at = AppTest.from_file(APP_PATH)
                at.run()

                at.text_input(key="case_id_input").set_value("CASE-5555")
                at.button[0].click()
                at.run()

                assert at.exception == []
                assert GENERIC_ERROR_MESSAGE in [e.value for e in at.error]
                assert CASE_NOT_FOUND_MESSAGE not in [e.value for e in at.warning]
