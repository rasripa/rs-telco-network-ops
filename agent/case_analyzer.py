"""agent/case_analyzer.py: deterministic case-validity, root-cause/fix, and
case-summary decision logic (Requirements 5, 6, and 7).

This module implements the correctness-critical decisions of the Network
Ops Agent as pure Python functions with no network calls, so they can be
unit/property tested independently of AWS or the LLM. See the design
document's "CaseAnalyzer" component section
(.kiro/specs/network-ops-agent/design.md) for the full interface contract.

`determine_validity` (task 12.1), `extract_root_cause_and_fix` (task
12.4), and `build_case_summary` (task 12.7) are all implemented here.
"""

from __future__ import annotations

import re

from agent.models import (
    CaseRecord,
    CaseSummary,
    KBRetrievalItem,
    ValidityDetermination,
)


def determine_validity(
    case: CaseRecord,
    retrieval_items: list[KBRetrievalItem],
) -> ValidityDetermination:
    """Determine whether `case` is a valid, runbook-addressable issue.

    A case is valid iff at least one `retrieval_items` entry has an
    `issue_category` that equals (exact string match) `case
    .network_issue_category` (Requirements 5.2, 5.3). Exactly one of
    `valid`/`invalid` is expressed via the boolean `valid` field
    (Requirement 5.4).

    When one or more items match, `matched_item` is the matching item with
    the highest `score` (ties broken by the first such item encountered in
    `retrieval_items`, i.e. stable on input order - this mirrors the
    design's "ties among matching items are broken by highest score" rule
    for `determine_validity` itself, independent of the separate matching-
    item selection `extract_root_cause_and_fix` will perform later). When
    no item matches (including when `retrieval_items` is empty),
    `matched_item` is `None`.

    `rationale` always references `case.network_issue_category` by value
    and states whether/how a matching runbook category was found
    (Requirement 5.5).

    Args:
        case: The case record being analyzed.
        retrieval_items: Knowledge_Base retrieval results for this case.

    Returns:
        A `ValidityDetermination` with `valid`, `matched_item`, and
        `rationale` populated per the rules above.
    """
    category = case.network_issue_category

    matching_items = [
        item for item in retrieval_items if item.issue_category == category
    ]

    if not matching_items:
        rationale = (
            f"Case network issue category '{category}' did not match the "
            "issue category of any retrieved runbook content; no runbook "
            "category matched, so the case is determined to be invalid."
        )
        return ValidityDetermination(
            valid=False,
            matched_item=None,
            rationale=rationale,
        )

    # Tie-break: among matching items, prefer the highest `score`. Python's
    # max() returns the first maximal element for ties, so ties resolve to
    # the earliest-occurring highest-scoring match in `retrieval_items`,
    # giving a deterministic, reproducible choice.
    best_match = max(matching_items, key=lambda item: item.score)

    rationale = (
        f"Case network issue category '{category}' matched the issue "
        f"category of retrieved runbook content (source: "
        f"'{best_match.source_uri}'); the case is determined to be valid."
    )
    return ValidityDetermination(
        valid=True,
        matched_item=best_match,
        rationale=rationale,
    )


# --- extract_root_cause_and_fix ---------------------------------------------
#
# `KBRetrievalItem.text` is free text: the Knowledge_Base returns whatever
# chunk of a `Runbook_Corpus` markdown document (rendered from a
# `RunbookDocument`, see scripts/generate_runbooks.py's `render_runbook`)
# semantic search matched. There is no separate structured "root cause" or
# "fix steps" field on `KBRetrievalItem` - both must be parsed out of
# `text` itself.
#
# The Runbook_Generator's rendered documents are expected to present each
# `RunbookDocument` field as its own labeled section (an ATX markdown
# heading, e.g. "## Root Cause Criteria" / "## Remediation Steps", or an
# equivalent "Label:" line), each followed by an itemized (bulleted or
# numbered) list, mirroring the field order in `RunbookDocument`
# (diagnostic_steps, root_cause_criteria, remediation_steps). The parser
# below is written against that expected structure, while still degrading
# gracefully - rather than raising - for a chunk that lacks explicit
# section headings (for example, a retrieval chunk boundary that only
# captured part of a document), since the exact rendering has not been
# finalized yet:
#
#   1. Lines are scanned in order. A markdown ATX heading ("#".."######")
#      or a short "Label:" line switches the "current section" to "fix"
#      (heading text contains "remediation" or "fix"), "root_cause"
#      (heading text contains "root cause"), or "other" (anything else,
#      e.g. "Diagnostic Steps" - ignored).
#   2. While inside the "fix" section, each bulleted/numbered list-item
#      line (marker stripped) is appended, in the order encountered, to
#      the fix-step list.
#   3. While inside the "root_cause" section, each list-item (or plain
#      prose) line is collected, in order, and later joined with "; " to
#      form a single root-cause finding string.
#   4. If no "root_cause" section/heading was found, the chunk's leading
#      prose (non-heading, non-list-item lines) is used as the root-cause
#      statement instead, falling back to the full chunk text as a last
#      resort.
#   5. If no "fix" section/heading was found, any bulleted/numbered lines
#      found elsewhere in the chunk (outside the root-cause section) are
#      used as the fix steps instead, falling back to the full chunk text
#      as a single step as a last resort.
#
# In every case, the returned root cause and fix steps are built exclusively
# from substrings of `matched_item.text` (list markers/heading markup are
# stripped, but no words are added, reordered across list items, or
# sourced from anywhere else), and the fix-step list preserves the order
# the steps appear in the source text (Requirements 6.1, 6.2, 6.3).

_HEADING_RE = re.compile(r"^#{1,6}\s*(.+?)\s*#*$")
_LABEL_HEADING_RE = re.compile(r"^([A-Za-z][A-Za-z /_-]{2,60}):$")
_LIST_ITEM_RE = re.compile(r"^(?:[-*+]\s+|\d+[.)]\s+)(.*\S)$")

_ROOT_CAUSE_HEADING_KEYWORDS = ("root cause",)
_FIX_HEADING_KEYWORDS = ("remediation", "fix")


def _heading_text(line: str) -> str | None:
    """Return a heading line's text (markers stripped), or None if `line`
    is not recognized as a heading line."""
    match = _HEADING_RE.match(line)
    if match:
        return match.group(1).strip()
    match = _LABEL_HEADING_RE.match(line)
    if match:
        return match.group(1).strip()
    return None


def _classify_heading(heading_text: str) -> str:
    """Classify a heading's text as the "fix", "root_cause", or "other"
    section it introduces, based on a simple keyword match."""
    lowered = heading_text.lower()
    if any(keyword in lowered for keyword in _FIX_HEADING_KEYWORDS):
        return "fix"
    if any(keyword in lowered for keyword in _ROOT_CAUSE_HEADING_KEYWORDS):
        return "root_cause"
    return "other"


def extract_root_cause_and_fix(
    matched_item: KBRetrievalItem,
) -> tuple[str, list[str]]:
    """Derive the root cause finding and ordered fix steps for a valid case.

    Both values are derived exclusively from `matched_item.text` - the
    single highest-scoring matching `KBRetrievalItem` (typically
    `ValidityDetermination.matched_item` from `determine_validity`) - so
    that no information is introduced from any other runbook or retrieval
    item (Requirements 6.1, 6.2). See the module-level comment above this
    function for the full section-parsing/fallback approach used to pull
    the root cause and fix steps out of that free-text chunk.

    The returned fix-step list has at least one entry (assuming
    `matched_item.text` is non-empty), appears in the same relative order
    as in the source text, and contains no step that is not present in
    the source text (Requirement 6.3).

    Args:
        matched_item: The single `KBRetrievalItem` whose runbook content
            matched the case's network issue category.

    Returns:
        A `(root_cause, fix_steps)` tuple: `root_cause` is a single string
        describing the underlying cause; `fix_steps` is the ordered list
        of remediation steps.
    """
    text = matched_item.text or ""
    lines = text.splitlines()

    root_cause_lines: list[str] = []
    fix_lines: list[str] = []
    other_list_lines: list[str] = []
    section: str | None = None
    saw_fix_heading = False

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue

        heading_text = _heading_text(line)
        if heading_text is not None:
            section = _classify_heading(heading_text)
            if section == "fix":
                saw_fix_heading = True
            continue

        list_match = _LIST_ITEM_RE.match(line)
        content = list_match.group(1).strip() if list_match else line

        if section == "fix":
            fix_lines.append(content)
        elif section == "root_cause":
            root_cause_lines.append(content)
        elif list_match is not None:
            # A list item outside any recognized section: kept as a
            # fallback candidate fix step in case no dedicated
            # remediation section is present in this chunk.
            other_list_lines.append(content)
        # Prose lines outside a recognized section contribute to neither
        # list directly; leading prose is instead handled in the root
        # cause fallback below.

    fix_steps = list(fix_lines)
    if not fix_steps and not saw_fix_heading:
        fix_steps = list(other_list_lines)

    if root_cause_lines:
        root_cause = "; ".join(root_cause_lines)
    else:
        prose_lines = [
            stripped
            for raw_line in lines
            if (stripped := raw_line.strip())
            and _heading_text(stripped) is None
            and _LIST_ITEM_RE.match(stripped) is None
        ]
        root_cause = " ".join(prose_lines) if prose_lines else text.strip()

    if not fix_steps:
        fallback_step = text.strip()
        fix_steps = [fallback_step] if fallback_step else []

    return root_cause, fix_steps


# --- build_case_summary ------------------------------------------------------

_NO_RESOLUTION_STATEMENT = (
    "No runbook-supported resolution applies to this case."
)


def build_case_summary(
    case: CaseRecord,
    determination: ValidityDetermination,
    root_cause: str | None = None,
    fix_steps: list[str] | None = None,
) -> CaseSummary:
    """Assemble the individually labeled `Case_Summary` for a case.

    `case_id`, `customer_name`, and `issue_description` are copied
    directly from `case` (Requirement 7.1); `validity` and `rationale` are
    derived from `determination` (Requirements 5.4, 5.5, 7.1).

    When `determination.valid` is True (Requirements 6.4, 7.2, 7.3):
      - `root_cause`/`fix_steps` are the supplied values, or, if either
        was not supplied (left as `None`), they are derived by calling
        `extract_root_cause_and_fix(determination.matched_item)` so that
        callers may either pre-compute these values themselves or let
        this function derive them - both keep the analyzer's three
        decision functions ("combine ... into an individually labeled
        Case_Summary") composable.
      - `runbook_reference` is set to `determination.matched_item
        .source_uri`, identifying the runbook document the finding and
        fix were derived from.

    When `determination.valid` is False (Requirements 5.6, 7.2): `root_
    cause`, `fix_steps`, and `runbook_reference` are all `None`, and the
    returned summary's `rationale` states that no runbook-supported
    resolution applies.

    Because every field is copied only from `case`/`determination` (and,
    when valid, from `determination.matched_item`), a summary built for
    one case never carries content belonging to any other case record
    (Requirements 7.4, 7.5).

    Args:
        case: The case record the summary is being built for.
        determination: The validity determination for `case`.
        root_cause: A pre-computed root cause finding, or `None` to derive
            it from `determination.matched_item` when valid.
        fix_steps: A pre-computed, ordered fix-step list, or `None` to
            derive it from `determination.matched_item` when valid.

    Returns:
        The assembled `CaseSummary`.

    Raises:
        ValueError: If `determination.valid` is True but `determination
            .matched_item` is `None` (an invariant violation - a valid
            determination must carry the matched runbook item it was
            based on).
    """
    if not determination.valid:
        rationale = f"{determination.rationale} {_NO_RESOLUTION_STATEMENT}"
        return CaseSummary(
            case_id=case.case_id,
            customer_name=case.customer_name,
            issue_description=case.issue_description,
            validity="invalid",
            rationale=rationale,
            root_cause=None,
            fix_steps=None,
            runbook_reference=None,
        )

    matched_item = determination.matched_item
    if matched_item is None:
        raise ValueError(
            "ValidityDetermination.valid is True but matched_item is "
            "None; a valid determination must carry the matched runbook "
            "item it was based on."
        )

    if root_cause is None or fix_steps is None:
        derived_root_cause, derived_fix_steps = extract_root_cause_and_fix(
            matched_item
        )
        if root_cause is None:
            root_cause = derived_root_cause
        if fix_steps is None:
            fix_steps = derived_fix_steps

    return CaseSummary(
        case_id=case.case_id,
        customer_name=case.customer_name,
        issue_description=case.issue_description,
        validity="valid",
        rationale=determination.rationale,
        root_cause=root_cause,
        fix_steps=fix_steps,
        runbook_reference=matched_item.source_uri,
    )
