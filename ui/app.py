"""ui/app.py: Streamlit_UI case_id submission handling (Requirement 9).

Per the design document's "9. Streamlit_UI (`ui/app.py`, `ui/agent_client.py`)"
component section (.kiro/specs/network-ops-agent/design.md):

    A single text input for `case_id` and a submit control. Client-side
    validation rejects empty/whitespace-only input before invoking the
    agent. `st.session_state` tracks an `is_processing` flag to disable
    re-submission while a request is in flight, and stores the current
    result (or `None`) so a new submission clears any previously
    displayed `Case_Summary`/`Customer_Email` before the new outcome is
    known.

This module (task 18.2) implements only the case_id submission plumbing:

- A text input widget and a submit button (Requirement 9.1).
- Client-side rejection of an empty/whitespace-only `case_id`, displaying
  a required-field message without ever invoking the agent client
  (Requirement 9.4).
- An `st.session_state.is_processing` guard that is set `True`
  immediately before calling `ui.agent_client.invoke(case_id)` and set
  `False` immediately after (Requirement 9.6), with a processing
  indicator shown for the duration of the call (Requirement 9.3).
- The resulting `agent.models.AnalysisResult` is stored in
  `st.session_state.last_result` for task 18.3 to render.

Rendering of the `Case_Summary`/`Customer_Email` sections, the
`CASE_NOT_FOUND` message, and the generic error banner (Requirements 9.5,
10.1-10.5) is implemented below by task 18.3, which edits this same file
in a later wave (per tasks.md's Task Dependency Graph scheduling notes
for `ui/app.py`: 18.2 -> 18.3).

Task 18.3's result-rendering section, per the design document's
Components and Interfaces -> "9. Streamlit_UI" section:

    Rendering: two clearly headed sections, "Case Summary" and "Customer
    Email", or a single error banner when `AnalysisResult` is an error -
    never a mix of partial results and an error.

Concretely, `_render_result()` branches on `st.session_state.last_result`:

- `success=True`: renders the `Case_Summary`'s fields as individually
  labeled sections (Requirement 7.3, surfaced in the UI per Requirement
  10.1/10.3), plus a labeled "Customer Email" preview (Requirement 10.2/
  10.3) when a `CustomerEmail` is present.
- `success=False`, `error_type=CASE_NOT_FOUND`: a distinct "case not
  found" message, with no `Case_Summary`/`Customer_Email` section
  (Requirement 9.5).
- `success=False`, any other `error_type` (`LOOKUP_FAILURE`,
  `RETRIEVAL_FAILURE`, `ANALYSIS_FAILURE`, or defensively
  `INVALID_INPUT`, which task 18.2's client-side validation normally
  prevents from reaching this point): a generic `st.error(...)` banner
  that never surfaces `last_result.error_message`'s raw internal
  exception text to the support engineer (Requirement 10.4).
- `last_result is None`: nothing extra is rendered (covers "no
  submission yet" and the validation-rejected case already handled by
  18.2's `validation_error` branch above).

Requirement 10.5 (clearing a previously displayed result before showing
a new submission's outcome) does not require any additional rendering
logic beyond what 18.2 already does: `st.session_state.last_result` is
fully overwritten by `_handle_submit` (either with the new
`AnalysisResult` or with `None` on a validation rejection) before this
module's single top-to-bottom script rerun reaches `_render_result()`,
so a stale prior result is never rendered alongside the new outcome.

`agent_client.invoke` is called via attribute access on the imported
`ui.agent_client` module (`agent_client.invoke(...)`), rather than via a
direct `from ui.agent_client import invoke` binding, so that tests can
monkeypatch/mock `ui.agent_client.invoke` and have that substitution
observed here.
"""

from __future__ import annotations

import streamlit as st

from agent.models import AnalysisResult, CaseSummary, CustomerEmail, ErrorType
from ui import agent_client

PAGE_TITLE = "Network Ops Agent"

# Requirement 9.4: the message shown when a submitted case_id is empty or
# consists only of whitespace characters.
REQUIRED_FIELD_MESSAGE = "A case_id is required."

# Requirement 9.3: the label shown on the processing indicator displayed
# while the Network_Ops_Agent workflow is processing a submitted case_id.
PROCESSING_MESSAGE = "Analyzing case..."

# Requirement 9.5: shown when the submitted case_id was not found in the
# Case_Table. Deliberately distinct from GENERIC_ERROR_MESSAGE below so a
# "not found" outcome is never confused with an analysis failure.
CASE_NOT_FOUND_MESSAGE = (
    "No case was found for the provided case_id. Please verify the "
    "case_id and try again."
)

# Requirement 10.4: shown for any error result other than CASE_NOT_FOUND
# (LOOKUP_FAILURE, RETRIEVAL_FAILURE, ANALYSIS_FAILURE, or defensively
# INVALID_INPUT). Deliberately generic -- it never includes
# `last_result.error_message`'s raw internal exception text, so internal
# failure detail is not leaked to the support engineer.
GENERIC_ERROR_MESSAGE = (
    "Analysis could not be completed for this case_id. Please try again, "
    "or contact support if the problem persists."
)

# Section headings, shared with the AppTest-based UI tests (task 18.4) so
# tests can locate each section by its exact heading text (Requirement
# 10.3: each section must be labeled identifying it as the Case_Summary
# section or the Customer_Email section respectively).
CASE_SUMMARY_HEADING = "Case Summary"
CUSTOMER_EMAIL_HEADING = "Customer Email"


def _init_session_state() -> None:
    """Initialize the session_state keys used by this page, if absent.

    - `is_processing`: Requirement 9.6's re-submission guard. `True` only
      for the duration of an in-flight `agent_client.invoke` call.
    - `last_result`: the most recent `agent.models.AnalysisResult` (or
      `None` before any submission, or after a validation rejection).
      Consumed by task 18.3's rendering logic.
    - `validation_error`: the required-field message to display, or
      `None` when the most recent submission (if any) did not fail
      client-side validation.
    """
    if "is_processing" not in st.session_state:
        st.session_state.is_processing = False
    if "last_result" not in st.session_state:
        st.session_state.last_result = None
    if "validation_error" not in st.session_state:
        st.session_state.validation_error = None


def _handle_submit(case_id: str) -> None:
    """Validate and, if valid, invoke the Network_Ops_Agent workflow.

    Client-side validation (Requirement 9.4): an empty or whitespace-only
    `case_id` is rejected before `agent_client.invoke` is ever called;
    `st.session_state.validation_error` is set to the required-field
    message and any previously stored result is cleared instead.

    Re-submission guard (Requirement 9.6): if a request is already in
    flight (`st.session_state.is_processing` is `True`), this submission
    is ignored -- the submit control is also disabled while processing,
    so this is a defensive second check against duplicate invocation.

    Otherwise, `st.session_state.is_processing` is set `True` immediately
    before calling `agent_client.invoke(case_id)` and set `False`
    immediately after, in a `finally` block so the guard is always
    cleared even if `invoke` raises unexpectedly. A processing indicator
    (Requirement 9.3) is displayed for the duration of the call.
    """
    if st.session_state.is_processing:
        return

    stripped_case_id = case_id.strip()
    if not stripped_case_id:
        st.session_state.validation_error = REQUIRED_FIELD_MESSAGE
        st.session_state.last_result = None
        return

    st.session_state.validation_error = None
    st.session_state.is_processing = True
    try:
        with st.spinner(PROCESSING_MESSAGE):
            st.session_state.last_result = agent_client.invoke(stripped_case_id)
    finally:
        st.session_state.is_processing = False


def _render_case_summary(case_summary: CaseSummary) -> None:
    """Render a `CaseSummary`'s fields as individually labeled sections
    (Requirement 7.3's UI-facing counterpart; Requirements 10.1, 10.3).

    The always-present fields (case_id, customer, issue description,
    validity, rationale) are rendered first, followed by the
    valid-case-only fields (root cause, fix steps, runbook reference),
    which are `None` on an invalid case per `agent/models.py` and are
    therefore skipped rather than rendered as empty sections.
    """
    st.header(CASE_SUMMARY_HEADING)

    st.subheader("Case ID")
    st.write(case_summary.case_id)

    st.subheader("Customer")
    st.write(case_summary.customer_name)

    st.subheader("Issue Description")
    st.write(case_summary.issue_description)

    st.subheader("Validity")
    st.write(case_summary.validity)

    st.subheader("Rationale")
    st.write(case_summary.rationale)

    # Requirement 5.6: root_cause/fix_steps/runbook_reference are None on
    # an invalid case, so each is rendered only when present.
    if case_summary.root_cause is not None:
        st.subheader("Root Cause")
        st.write(case_summary.root_cause)

    if case_summary.fix_steps is not None:
        st.subheader("Fix Steps")
        for step_number, step in enumerate(case_summary.fix_steps, start=1):
            st.markdown(f"{step_number}. {step}")

    if case_summary.runbook_reference is not None:
        st.subheader("Runbook Reference")
        st.write(case_summary.runbook_reference)


def _render_customer_email(customer_email: CustomerEmail) -> None:
    """Render a labeled "Customer Email" preview (Requirements 10.2, 10.3).

    Shows the salutation, body, and signature as the composed email's
    natural reading order; `full_text` is the same content concatenated
    (per `agent/models.py`'s docstring) and is not repeated separately.
    """
    st.header(CUSTOMER_EMAIL_HEADING)
    st.write(customer_email.salutation)
    st.write(customer_email.body)
    st.write(customer_email.signature)


def _render_result(last_result: AnalysisResult | None) -> None:
    """Render the outcome of the most recent submission, if any.

    Branches on `last_result` per this module's docstring:

    - `None`: nothing extra is rendered (no submission yet, or the most
      recent submission was rejected by client-side validation -- 18.2's
      `validation_error` branch already covers that case).
    - `success=True`: the labeled `CaseSummary` sections (Requirements
      10.1, 10.3), followed by the `CustomerEmail` preview when present
      (Requirements 10.2, 10.3). `customer_email` may be `None` (e.g. if
      a future content-guard failure short-circuits email composition
      while still returning a summary) and is skipped gracefully in that
      case rather than raising.
    - `success=False`, `error_type=CASE_NOT_FOUND`: the distinct
      not-found message, with no result sections (Requirement 9.5).
    - `success=False`, any other `error_type`: the generic error banner,
      which never includes `error_message`'s raw internal text
      (Requirement 10.4).
    """
    if last_result is None:
        return

    if last_result.success:
        if last_result.case_summary is not None:
            _render_case_summary(last_result.case_summary)
        if last_result.customer_email is not None:
            _render_customer_email(last_result.customer_email)
        return

    if last_result.error_type is ErrorType.CASE_NOT_FOUND:
        st.warning(CASE_NOT_FOUND_MESSAGE)
        return

    # LOOKUP_FAILURE, RETRIEVAL_FAILURE, ANALYSIS_FAILURE, or (defensively,
    # should 18.2's client-side validation ever be bypassed) INVALID_INPUT.
    st.error(GENERIC_ERROR_MESSAGE)


def main() -> None:
    st.set_page_config(page_title=PAGE_TITLE)
    st.title(PAGE_TITLE)

    _init_session_state()

    case_id_input = st.text_input(
        "Case ID",
        key="case_id_input",
        disabled=st.session_state.is_processing,
        help="Enter the AnyTel support case_id to analyze.",
    )

    submitted = st.button(
        "Submit",
        disabled=st.session_state.is_processing,
    )

    if submitted:
        _handle_submit(case_id_input)

    if st.session_state.validation_error:
        st.error(st.session_state.validation_error)

    _render_result(st.session_state.last_result)


main()
