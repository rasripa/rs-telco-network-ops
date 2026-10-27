"""agent/email_composer.py: LLM-assisted composition of the customer-facing
`Customer_Email` (Requirements 8.1, 8.2, 8.3).

Per the design document's "7. EmailComposer and ContentGuard
(agent/email_composer.py, agent/content_guard.py)" component section
(.kiro/specs/network-ops-agent/design.md), `compose_customer_email` builds
a deterministic salutation and signature block itself, and asks a Strands
`Agent` to phrase only the email *body* -- grounded strictly on the
supplied `CaseSummary`'s `issue_description` and, when the case is valid,
its `root_cause` and `fix_steps`. When the case is invalid, `root_cause`/
`fix_steps` are already `None` on the `CaseSummary` (see
`agent/case_analyzer.py`'s `build_case_summary`) and are never read or
passed to the LLM, so they cannot leak into the composed body -- this
mirrors the design's stated approach ("When invalid, root_cause/fix_steps
are simply not passed to the LLM, so they cannot appear").

Content-guard enforcement of Requirement 8.4 (profanity/insults, internal
identifiers) is implemented separately in `agent/content_guard.py`
(a later task); this module targets only Requirements 8.1-8.3.

Only the actual LLM invocation (`_generate_email_body`) depends on a live
model; every other function in this module (prompt building, salutation/
signature templating, `full_text` assembly) is pure/deterministic. Callers
(including tests) can inject any object supporting Strands `Agent`'s
calling interface via the `llm_agent` parameter -- in particular, a plain
stub such as:

    class _StubAgent:
        def __init__(self, text):
            self.last_prompt = None
            self._text = text

        def __call__(self, prompt):
            self.last_prompt = prompt
            return self._text

so the deterministic salutation/signature/body-omission behavior covered
by design Property 11 can be verified -- and the prompt actually handed to
the LLM can be inspected -- without a live Bedrock call in every test run.
When `llm_agent` is omitted, a default Strands `Agent` (Bedrock-backed) is
lazily constructed only at call time, so importing this module or calling
`compose_customer_email` with an explicit stub never requires Bedrock
model configuration or credentials.
"""

from __future__ import annotations

from typing import Any, Protocol

from strands import Agent

from agent.models import CaseSummary, CustomerEmail

# Requirement 8.2: every Customer_Email's closing signature block
# identifies AnyTel by name as the sender.
_SIGNATURE = "Sincerely,\nThe AnyTel Support Team"

_SYSTEM_PROMPT = (
    "You are a customer support email writer for AnyTel, a "
    "telecommunications company. You write only the body paragraph(s) of "
    "a customer-facing support email -- never a salutation or signature; "
    "those are added separately. Write in plain, non-technical language "
    "that a customer without networking expertise can understand. Use "
    "only the facts given to you in the prompt; never invent, assume, or "
    "add any fact, root cause, step, or detail that was not explicitly "
    "provided. Never include profanity, insults, or negative language "
    "about the customer. Never include internal technical identifiers, "
    "root cause codes, case ids, runbook filenames, or internal "
    "system/component names. Respond with only the body text: no "
    "markdown headings, no salutation, and no signature."
)


class _EmailBodyAgent(Protocol):
    """Structural interface required of an object passed as `llm_agent`.

    Matches the subset of Strands `Agent`'s interface this module relies
    on: calling the object with a single string prompt and receiving back
    a value that stringifies to the generated body text -- either a plain
    `str`, or a Strands `AgentResult` (whose `__str__` returns its final
    message's text content).
    """

    def __call__(self, prompt: str) -> Any: ...


def _default_agent() -> Agent:
    """Lazily construct a default, Bedrock-backed Strands `Agent` used for
    email body composition when no `llm_agent` is explicitly supplied.

    Constructed only inside `compose_customer_email` at call time (never
    at module import time), so importing this module, or calling
    `compose_customer_email` with an explicit stub `llm_agent` (as in
    tests), never requires Bedrock model configuration or credentials.
    """
    return Agent(system_prompt=_SYSTEM_PROMPT)


def _build_email_prompt(summary: CaseSummary) -> str:
    """Build the LLM prompt for the email body, grounded strictly on
    `summary`.

    For a valid case (Requirement 8.1), the prompt supplies the issue
    description, the root cause, and the ordered fix steps, and instructs
    the model to describe the issue, explain the root cause in plain
    language, and present the fix steps as an ordered, customer-facing
    list, preserving their given order.

    For an invalid case (Requirement 8.3), `summary.root_cause` and
    `summary.fix_steps` are `None` (per `build_case_summary`) and this
    function never reads or references either field when composing the
    invalid-case prompt below -- so no root-cause/fix-step content is
    ever included in the prompt for an invalid case. Instead, the prompt
    asks only for a polite, non-technical acknowledgment that no network
    fault was identified for the reported issue.

    Args:
        summary: The `CaseSummary` to build a prompt for.

    Returns:
        The prompt text to pass to the LLM agent.
    """
    if summary.validity == "valid":
        fix_steps = summary.fix_steps or []
        numbered_steps = "\n".join(
            f"{index}. {step}" for index, step in enumerate(fix_steps, start=1)
        )
        return (
            "Write the body of a customer support email for the case "
            "described below. The customer reported this issue:\n"
            f"{summary.issue_description}\n\n"
            "The confirmed root cause (explain this in plain, "
            "non-technical language; do not call it a 'root cause' in "
            "the email):\n"
            f"{summary.root_cause}\n\n"
            "The steps that resolve the issue, in this exact order "
            "(present them as a numbered, customer-facing list; do not "
            "add, remove, or reorder any step):\n"
            f"{numbered_steps}\n\n"
            "Describe the issue, explain the cause in plain language, "
            "and present the fix steps in order."
        )

    return (
        "Write the body of a customer support email for the case "
        "described below. The customer reported this issue:\n"
        f"{summary.issue_description}\n\n"
        "Our review found no network fault for this reported issue. "
        "Write a polite, non-technical acknowledgment informing the "
        "customer that no network fault was identified for the reported "
        "issue. Do not mention a root cause, a fix, remediation steps, "
        "or any technical diagnostic detail, since none applies here."
    )


def _generate_email_body(llm_agent: _EmailBodyAgent, prompt: str) -> str:
    """Invoke `llm_agent` with `prompt` and return the generated body text.

    This is the sole point in the module that actually invokes the LLM,
    kept as a small, separately mockable function so tests can stub the
    LLM call (substituting a fake `llm_agent` whose `__call__` returns a
    canned string, see the module docstring) without needing live Bedrock
    credentials, while still exercising the real `compose_customer_email`
    control flow -- including exactly what prompt text was built and
    handed to the LLM -- around it.

    Args:
        llm_agent: The agent (or stub) to invoke.
        prompt: The prompt text built by `_build_email_prompt`.

    Returns:
        The generated body text, stringified and stripped of leading/
        trailing whitespace.
    """
    result = llm_agent(prompt)
    return str(result).strip()


def compose_customer_email(
    summary: CaseSummary,
    llm_agent: _EmailBodyAgent | None = None,
) -> CustomerEmail:
    """Compose the customer-facing `Customer_Email` for an analyzed case.

    Builds a deterministic salutation ("Dear {customer_name},",
    Requirements 8.1, 8.3) and a deterministic signature ("Sincerely,\\n
    The AnyTel Support Team", identifying AnyTel by name per Requirement
    8.2) itself, and delegates only the body's phrasing to `llm_agent` (a
    Strands `Agent`, or any stub matching its calling interface) via
    `_generate_email_body`.

    The prompt passed to `llm_agent` (built by `_build_email_prompt`) is
    grounded strictly on `summary`: for a valid case, it supplies `issue_
    description`, `root_cause`, and `fix_steps` and asks for a plain-
    language description, root-cause explanation, and ordered fix steps
    (Requirement 8.1). For an invalid case, `root_cause`/`fix_steps` are
    already `None` on `summary` (per `agent.case_analyzer.build_case_
    summary`) and are never read or passed to the LLM, so the composed
    body cannot include root-cause or fix-step content for an invalid
    case (Requirement 8.3).

    Args:
        summary: The `CaseSummary` to compose an email for.
        llm_agent: The Strands `Agent` (or a stub matching its calling
            interface, see `_EmailBodyAgent`) used to phrase the body.
            When omitted, a default Bedrock-backed `Agent` is lazily
            constructed (see `_default_agent`) -- pass an explicit stub
            here to avoid requiring live Bedrock credentials, e.g. in
            tests.

    Returns:
        The composed `CustomerEmail`, with `full_text` set to the
        concatenation of the salutation, body, and signature.
    """
    salutation = f"Dear {summary.customer_name},"
    signature = _SIGNATURE

    agent = llm_agent if llm_agent is not None else _default_agent()
    prompt = _build_email_prompt(summary)
    body = _generate_email_body(agent, prompt)

    full_text = f"{salutation}\n\n{body}\n\n{signature}"

    return CustomerEmail(
        salutation=salutation,
        body=body,
        signature=signature,
        full_text=full_text,
    )
