"""tests/property/test_email_composer.py

Property-based test for agent/email_composer.py's `compose_customer_email`
-- design.md Correctness Properties, Property 11: "Customer email
salutation and signature correctness."

This test uses a stub `llm_agent` (a simple recording callable, see
`_RecordingStubAgent` below) so no live Bedrock call is made. The stub
returns a canned body string chosen by the test and records the exact
prompt text `compose_customer_email` handed it, so the test can assert
both on the deterministic salutation/signature/`full_text` templating
(which does not depend on the LLM at all) and on what the *prompt itself*
was grounded on for valid vs. invalid cases.
"""

from __future__ import annotations

from hypothesis import example, given, settings
from hypothesis import strategies as st

from agent.email_composer import _SIGNATURE, compose_customer_email
from agent.models import CaseSummary

_VALID = "valid"
_INVALID = "invalid"

# Tokens that would indicate a genuine leak if found in the composed
# `full_text`: a stringified `None` (guarding against a careless
# `f"{summary.root_cause}"` on a `None` value), and the raw attribute
# names `root_cause`/`fix_steps` (guarding against accidental repr/debug
# text leaking internal field names into customer-facing content). Every
# generated `customer_name` and `canned_body` below is filtered to never
# contain these tokens itself, so if one of them shows up in the composed
# email it can only be attributed to `compose_customer_email`'s own logic,
# never to coincidental test input.
_LEAK_TOKENS = ("None", "root_cause", "fix_steps")


def _no_leak_tokens(text: str) -> bool:
    return all(token not in text for token in _LEAK_TOKENS)


# Restricted-but-varied text strategies for free-text fields. Plain
# `st.text()` (unicode) is used for `case_id`/`rationale`, which never
# flow into the composed email in this test, while `customer_name` and
# the stub's `canned_body` -- both of which do end up in `email.full_text`
# -- are filtered against `_LEAK_TOKENS` per the note above.
_case_id_text = st.text(min_size=1, max_size=16)
_customer_names = st.text(min_size=1, max_size=30).filter(_no_leak_tokens)
_issue_descriptions = st.text(min_size=1, max_size=60)
_rationales = st.text(min_size=1, max_size=60)

# root_cause / fix_steps / runbook_reference / canned LLM body text: kept
# to a plain letters+digits+spaces alphabet, stripped of leading/trailing
# whitespace, so (a) values are non-empty and easy to reason about as
# exact substrings, and (b) the stub's returned body text round-trips
# unchanged through `_generate_email_body`'s `str(result).strip()` call.
_word_text = (
    st.text(
        alphabet=st.characters(
            categories=("Ll", "Lu", "Nd"), max_codepoint=0x2000
        )
        | st.just(" "),
        min_size=1,
        max_size=40,
    )
    .map(str.strip)
    .filter(lambda s: s != "" and _no_leak_tokens(s))
)


@st.composite
def _case_summaries(draw: st.DrawFn) -> CaseSummary:
    """Draw a `CaseSummary` that is either valid (with populated
    `root_cause`/`fix_steps`/`runbook_reference`) or invalid (with all
    three set to `None`), covering both branches
    `compose_customer_email`'s prompt-building logic must handle
    differently."""
    is_valid = draw(st.booleans())
    case_id = draw(_case_id_text)
    customer_name = draw(_customer_names)
    issue_description = draw(_issue_descriptions)
    rationale = draw(_rationales)

    if is_valid:
        root_cause = draw(_word_text)
        fix_steps = draw(st.lists(_word_text, min_size=1, max_size=5))
        runbook_reference = draw(st.text(min_size=1, max_size=30))
        return CaseSummary(
            case_id=case_id,
            customer_name=customer_name,
            issue_description=issue_description,
            validity=_VALID,
            rationale=rationale,
            root_cause=root_cause,
            fix_steps=fix_steps,
            runbook_reference=runbook_reference,
        )

    return CaseSummary(
        case_id=case_id,
        customer_name=customer_name,
        issue_description=issue_description,
        validity=_INVALID,
        rationale=rationale,
        root_cause=None,
        fix_steps=None,
        runbook_reference=None,
    )


class _RecordingStubAgent:
    """A minimal stub matching `email_composer._EmailBodyAgent`'s calling
    interface: callable with a single prompt string, returning a canned
    body string. Records the exact prompt it was called with (and how
    many times it was called) so the test can inspect what
    `compose_customer_email` actually asked the "LLM" to write, without
    making any live Bedrock call.
    """

    def __init__(self, body_text: str) -> None:
        self.last_prompt: str | None = None
        self.call_count = 0
        self._body_text = body_text

    def __call__(self, prompt: str) -> str:
        self.last_prompt = prompt
        self.call_count += 1
        return self._body_text


def _make_valid_summary() -> CaseSummary:
    return CaseSummary(
        case_id="case-valid-1",
        customer_name="Jamie Rivera",
        issue_description="No internet connectivity since this morning.",
        validity=_VALID,
        rationale="Category matched; case determined to be valid.",
        root_cause="A fiber cut near the local exchange degraded the line.",
        fix_steps=[
            "Restart the modem and router.",
            "Confirm the line light is solid green.",
            "Contact us if the issue persists after 10 minutes.",
        ],
        runbook_reference="runbooks/connectivity-outage.md",
    )


def _make_invalid_summary() -> CaseSummary:
    return CaseSummary(
        case_id="case-invalid-1",
        customer_name="Alex Chen",
        issue_description="Customer reports their television remote is slow.",
        validity=_INVALID,
        rationale=(
            "Category 'tv_remote' did not match any retrieved runbook "
            "content; no runbook category matched, so the case is "
            "determined to be invalid. No runbook-supported resolution "
            "applies to this case."
        ),
        root_cause=None,
        fix_steps=None,
        runbook_reference=None,
    )


# Feature: network-ops-agent, Property 11: Salutation, signature, and invalid-case content omission
#
# For any CaseSummary (valid or invalid), the CustomerEmail produced by
# compose_customer_email's deterministic templating has a salutation
# containing the summary's customer_name and a signature containing
# "AnyTel"; when the summary is invalid, the composed email's body does
# not contain the summary's root_cause or any of its fix_steps text
# (because they were never supplied to the composer).
#
# Validates: Requirements 8.1, 8.2, 8.3
@given(
    summary=_case_summaries(),
    canned_body=_word_text,
)
@settings(max_examples=100)
@example(summary=_make_valid_summary(), canned_body="Thanks for your patience.")
@example(summary=_make_invalid_summary(), canned_body="We appreciate your report.")
def test_salutation_signature_and_invalid_case_content_omission(
    summary: CaseSummary,
    canned_body: str,
) -> None:
    stub_agent = _RecordingStubAgent(canned_body)

    email = compose_customer_email(summary, llm_agent=stub_agent)

    # Requirements 8.1, 8.3: the salutation always addresses the customer
    # by the exact customer_name from the CaseSummary, in the documented
    # "Dear {customer_name}," format, for both valid and invalid cases.
    assert email.salutation == f"Dear {summary.customer_name},"

    # Requirement 8.2: every Customer_Email's signature identifies AnyTel
    # by name as the sender -- checked both against the module's exact
    # _SIGNATURE constant and, independently, via a substring check.
    assert email.signature == _SIGNATURE
    assert "AnyTel" in email.signature

    # full_text is exactly the concatenation of salutation + body +
    # signature, per agent/email_composer.py's documented format (and
    # agent/models.py's "full_text: salutation + body + signature,
    # concatenated" contract).
    assert email.full_text == f"{email.salutation}\n\n{email.body}\n\n{email.signature}"

    # The stub was invoked exactly once, and its canned body flows through
    # untouched into email.body/full_text -- compose_customer_email itself
    # injects no additional root_cause/fix_steps content around it.
    assert stub_agent.call_count == 1
    assert email.body == canned_body
    assert canned_body in email.full_text

    if summary.validity == _INVALID:
        # Requirement 8.3: root_cause/fix_steps are None on an invalid
        # CaseSummary (per build_case_summary), so they are trivially
        # never referenced in the prompt handed to the stub llm_agent.
        assert summary.root_cause is None
        assert summary.fix_steps is None
        assert stub_agent.last_prompt is not None

        # Guard against compose_customer_email itself leaking
        # root-cause-shaped technical content into the composed email:
        # no stringified None, and no raw internal field names, appear
        # anywhere in the final full_text. Because customer_name and
        # canned_body were both generated to exclude these tokens, any
        # occurrence here would indicate a real leak introduced by
        # compose_customer_email's own logic.
        for token in _LEAK_TOKENS:
            assert token not in email.full_text
    else:
        # Requirement 8.1 (prompt-construction grounding check): for a
        # valid case, the prompt passed to the stub llm_agent DOES
        # include the root_cause text and every fix_steps entry -- this
        # is the deterministic part of "incorporates root cause/fix" that
        # is testable without a live LLM call.
        assert summary.root_cause is not None
        assert summary.fix_steps is not None
        prompt = stub_agent.last_prompt
        assert prompt is not None
        assert summary.root_cause in prompt
        for step in summary.fix_steps:
            assert step in prompt
