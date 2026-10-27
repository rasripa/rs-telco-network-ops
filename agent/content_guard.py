"""agent/content_guard.py: deterministic post-generation content validation
for composed Customer_Email text (Requirement 8.4).

Per the design document's "7. EmailComposer and ContentGuard
(agent/email_composer.py, agent/content_guard.py)" component section
(.kiro/specs/network-ops-agent/design.md), `validate_email_content` is a
deterministic, LLM-independent check run on every composed `CustomerEmail`
before it is returned to the caller. It scans `email.full_text` for:

  - a denylist of profanity/insult terms and other negative language that
    must never be directed at the customer, and
  - a denylist of internal technical identifiers, root-cause/error codes,
    runbook filenames/URIs, and internal system or component names that
    must never leak into customer-facing content.

Both denylists are exact, case-insensitive, whole-word/whole-phrase
matches (so, e.g., the profanity term "hell" does not spuriously match
inside "hello"); a small set of regex patterns additionally catches
runbook filename/URI shapes and raw error-code-like tokens that are not
practical to enumerate as a fixed word list.

`validate_email_content` never raises: it always returns a `GuardResult`
describing whether the email passed and, if not, which denylist
entries/patterns were found (Requirement 8.4). Per the design's Property
12, clean text containing none of these entries always passes, and a
violation is reported if and only if at least one denylist entry or
pattern is present in the text -- this module intentionally contains no
other rejection logic (e.g. no blank/placeholder-content check), since
adding one would contradict that property.

The orchestrator (`agent/network_ops_agent.py`, task 16.1) is responsible
for retrying composition once with a stricter prompt when `GuardResult
.passed` is False, and for treating a second failure as an
`ErrorType.ANALYSIS_FAILURE` rather than releasing non-compliant content.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from agent.models import CustomerEmail

# --- Denylists ---------------------------------------------------------------
#
# Requirement 8.4: "free of profanity, insults, or negative language
# directed at the customer, and free of internal technical identifiers,
# root cause codes, and internal system or component names."
#
# These are plain, whole-word/whole-phrase denylist entries (matched
# case-insensitively via the word-boundary regexes built below, so e.g.
# "hell" does not match inside "hello"). Unit tests (task 14.5) exercise a
# sample entry from each list directly against these module-level
# constants, so entries are intentionally kept as simple literal strings
# rather than pre-compiled patterns.

PROFANITY_INSULT_DENYLIST: tuple[str, ...] = (
    "idiot",
    "idiotic",
    "stupid",
    "moron",
    "moronic",
    "imbecile",
    "incompetent",
    "pathetic",
    "worthless",
    "useless",
    "damn",
    "damned",
    "hell",
    "crap",
    "screwed up",
    "shut up",
)

# Internal system/component names (per the design document's Glossary and
# component list), internal field/identifier names, and internal type
# names that must never appear in customer-facing content.
INTERNAL_IDENTIFIER_DENYLIST: tuple[str, ...] = (
    "Case_Table",
    "Case_Record",
    "Case_Lookup_MCP_Server",
    "Knowledge_Base",
    "Runbook_Corpus",
    "Network_Ops_Agent",
    "DynamoDB",
    "AgentCore",
    "Bedrock",
    "MCP",
    "boto3",
    "case_id",
    "network_issue_category",
    "issue_category",
    "root_cause",
    "fix_steps",
    "runbook_reference",
    "ValidityDetermination",
    "CaseSummary",
    "KBRetrievalItem",
)

# Raw error-code-like patterns and runbook filename/URI shapes that are not
# practical to enumerate as fixed denylist words: an "RC-" or "ERR-"
# prefixed numeric code (a stand-in for a raw internal root-cause/error
# code), a ".md"/".txt" runbook filename, or an "s3://" object URI (a
# Runbook_Corpus document's `source_uri`).
_INTERNAL_IDENTIFIER_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(?:RC|ERR)-\d+\b", re.IGNORECASE),
    re.compile(r"\S+\.(?:md|txt)\b", re.IGNORECASE),
    re.compile(r"s3://\S+", re.IGNORECASE),
)


@dataclass(frozen=True)
class GuardResult:
    """The outcome of a `validate_email_content` content-guard check.

    `passed` is True iff no denylist entry/pattern was found anywhere in
    the checked text. `violations` names every distinct denylist entry or
    matched pattern text found (empty when `passed` is True).
    """

    passed: bool
    violations: list[str]


def _term_pattern(term: str) -> re.Pattern[str]:
    """Build a case-insensitive, whole-word/whole-phrase regex for a
    single denylist entry, so substrings of unrelated words (e.g. the
    profanity term "hell" appearing inside "hello") are not spuriously
    matched."""
    return re.compile(rf"\b{re.escape(term)}\b", re.IGNORECASE)


def _find_denylist_violations(
    text: str, denylist: tuple[str, ...]
) -> list[str]:
    """Return the denylist entries (in denylist order, each at most once)
    that appear as a whole-word/whole-phrase match in `text`."""
    return [term for term in denylist if _term_pattern(term).search(text)]


def _find_pattern_violations(
    text: str, patterns: tuple[re.Pattern[str], ...]
) -> list[str]:
    """Return the literal matched substrings (in pattern order, each at
    most once) for every regex pattern that matches somewhere in `text`."""
    violations: list[str] = []
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            violations.append(match.group(0))
    return violations


def validate_email_content(email: CustomerEmail) -> GuardResult:
    """Validate a composed `CustomerEmail` against the content denylists.

    Scans `email.full_text` (the salutation + body + signature,
    concatenated) for any profanity/insult denylist entry, any internal-
    identifier denylist entry, or any internal-identifier-shaped regex
    pattern (a runbook filename/URI, or a raw error/root-cause code). The
    check is case-insensitive and matches whole words/phrases only, so it
    does not spuriously flag a denylist term that merely occurs as a
    substring of an unrelated word.

    Per the design document's Property 12, a violation is reported if and
    only if at least one such entry/pattern is present; text containing
    none of them always passes (Requirement 8.4).

    Args:
        email: The composed customer email to validate.

    Returns:
        A `GuardResult` with `passed=True` and an empty `violations` list
        when no denylist entry/pattern is found, or `passed=False` and a
        `violations` list naming every distinct denylist entry/pattern
        that matched (profanity/insult entries first, then internal-
        identifier entries, then internal-identifier-shaped patterns),
        when at least one is found. Never raises.
    """
    text = email.full_text

    raw_violations = (
        _find_denylist_violations(text, PROFANITY_INSULT_DENYLIST)
        + _find_denylist_violations(text, INTERNAL_IDENTIFIER_DENYLIST)
        + _find_pattern_violations(text, _INTERNAL_IDENTIFIER_PATTERNS)
    )

    # De-duplicate while preserving first-occurrence order: overlapping
    # patterns (e.g. the ".md"/".txt" filename pattern and the "s3://" URI
    # pattern) can both match the same substring of an S3 object URI.
    seen: set[str] = set()
    violations: list[str] = []
    for violation in raw_violations:
        if violation not in seen:
            seen.add(violation)
            violations.append(violation)

    return GuardResult(passed=len(violations) == 0, violations=violations)
