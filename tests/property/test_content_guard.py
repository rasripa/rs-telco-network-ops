"""tests/property/test_content_guard.py

Property-based test for agent/content_guard.py's `validate_email_content`
(design.md Correctness Properties, Property 12).

Per design.md's exact Property 12 statement:

    For any candidate email text, `validate_email_content` reports a
    violation if and only if the text contains at least one entry from
    the configured profanity/insult denylist or the internal-identifier
    denylist; clean text (containing none of these entries) always
    passes.

This is exercised from three angles:

1. Text built exclusively from a hand-picked word list that deliberately
   avoids every entry in `PROFANITY_INSULT_DENYLIST` and
   `INTERNAL_IDENTIFIER_DENYLIST` (and avoids the internal-identifier-
   shaped regex patterns) always passes, with no violations reported.
2. Text containing a randomly-chosen entry from either denylist,
   embedded within otherwise-clean surrounding text, always fails, with
   that exact entry present in `violations`.
3. (Recommended extra coverage) Text containing a synthetic token shaped
   like one of the internal-identifier regex patterns (a runbook
   filename, an `s3://` URI, or an `RC-`/`ERR-` error code) also always
   fails.

`CustomerEmail` is constructed with all four fields set consistently
(salutation, body, signature, and `full_text` as their concatenation),
mirroring how `agent/email_composer.py` builds it -- even though
`validate_email_content` only ever reads `full_text`.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from agent.content_guard import (
    INTERNAL_IDENTIFIER_DENYLIST,
    PROFANITY_INSULT_DENYLIST,
    validate_email_content,
)
from agent.models import CustomerEmail

# --- Clean-text word list ----------------------------------------------------
#
# Ordinary, unrelated words that do not equal (or contain as a whole-word
# substring) any `PROFANITY_INSULT_DENYLIST`/`INTERNAL_IDENTIFIER_DENYLIST`
# entry, and that never introduce a period, "s3://", or "RC-"/"ERR-"-digit
# shape that could accidentally trip the internal-identifier regex
# patterns. Used to build text that is clean by construction.
_SAFE_WORDS: tuple[str, ...] = (
    "apple", "river", "cloud", "garden", "bicycle", "harbor", "lantern",
    "meadow", "orchard", "pepper", "quartz", "silver", "timber", "violet",
    "walnut", "yellow", "bridge", "candle", "desert", "eagle", "forest",
    "glacier", "horizon", "island", "jungle", "kitchen", "ladder",
    "mountain", "notebook", "ocean", "pillow", "quilt", "rabbit",
    "sunset", "tunnel", "umbrella", "valley", "whisper", "yogurt",
    "zebra", "amber", "breeze", "coral", "dune", "ember", "fern",
    "granite", "honey", "jasmine", "kite", "lagoon", "maple", "nectar",
    "opal", "prairie", "quiver", "ribbon", "sapphire", "thistle",
    "urban", "velvet", "willow", "yarrow", "zenith", "thank", "grateful",
    "please", "regarding", "service", "today", "morning", "team",
    "contact", "soon", "update", "resolved", "appreciate", "patience",
)

# A clean sentence: two or more safe words joined by spaces. Never empty,
# never containing a denylist entry or a pattern-shaped token.
_clean_text: st.SearchStrategy[str] = st.lists(
    st.sampled_from(_SAFE_WORDS), min_size=2, max_size=12
).map(" ".join)

# All denylist entries from both lists, per Property 12's "at least one
# entry from the configured profanity/insult denylist or the internal-
# identifier denylist" wording.
_ALL_DENYLIST_ENTRIES: tuple[str, ...] = (
    PROFANITY_INSULT_DENYLIST + INTERNAL_IDENTIFIER_DENYLIST
)

# Synthetic tokens shaped like each internal-identifier regex pattern
# (`_INTERNAL_IDENTIFIER_PATTERNS` in agent/content_guard.py): a raw
# error/root-cause code, a runbook filename, and an S3 object URI.
_PATTERN_SHAPED_TOKENS: tuple[str, ...] = (
    "ERR-1234",
    "RC-9999",
    "notes.txt",
    "s3://bucket/some-runbook.md",
)


def _make_email(body_text: str) -> CustomerEmail:
    """Build a `CustomerEmail` with all four fields set consistently,
    mirroring `agent/email_composer.py`'s templating, so that `full_text`
    is the concatenation of `salutation`, `body_text`, and `signature`."""
    salutation = "Dear Customer,"
    signature = "Sincerely,\nThe AnyTel Support Team"
    full_text = f"{salutation}\n\n{body_text}\n\n{signature}"
    return CustomerEmail(
        salutation=salutation,
        body=body_text,
        signature=signature,
        full_text=full_text,
    )


# Feature: network-ops-agent, Property 12: Customer email content guard
#
# For any text built exclusively from words that avoid every denylist
# entry and every internal-identifier-shaped pattern, `validate_email_
# content` always reports passed=True with an empty violations list.
#
# Validates: Requirements 8.4
@given(text=_clean_text)
@settings(max_examples=100)
def test_clean_text_always_passes(text: str) -> None:
    email = _make_email(text)

    result = validate_email_content(email)

    assert result.passed is True
    assert result.violations == []


# Feature: network-ops-agent, Property 12: Customer email content guard
#
# For any denylist entry (drawn from either PROFANITY_INSULT_DENYLIST or
# INTERNAL_IDENTIFIER_DENYLIST) embedded within otherwise-clean
# surrounding text, `validate_email_content` always reports
# passed=False with that exact entry present in violations.
#
# Validates: Requirements 8.4
@given(
    entry=st.sampled_from(_ALL_DENYLIST_ENTRIES),
    prefix=_clean_text,
    suffix=_clean_text,
)
@settings(max_examples=100)
def test_denylist_entry_always_flagged(
    entry: str, prefix: str, suffix: str
) -> None:
    body = f"{prefix} {entry} {suffix}"
    email = _make_email(body)

    result = validate_email_content(email)

    assert result.passed is False
    assert entry in result.violations


# Feature: network-ops-agent, Property 12: Customer email content guard
#
# For any synthetic token shaped like one of the internal-identifier
# regex patterns (a runbook filename, an s3:// URI, or an RC-/ERR- error
# code) embedded within otherwise-clean surrounding text,
# `validate_email_content` always reports passed=False with that token
# present in violations.
#
# Validates: Requirements 8.4
@given(
    token=st.sampled_from(_PATTERN_SHAPED_TOKENS),
    prefix=_clean_text,
    suffix=_clean_text,
)
@settings(max_examples=100)
def test_pattern_shaped_violation_always_flagged(
    token: str, prefix: str, suffix: str
) -> None:
    body = f"{prefix} {token} {suffix}"
    email = _make_email(body)

    result = validate_email_content(email)

    assert result.passed is False
    assert token in result.violations
