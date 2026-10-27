"""Shared immutable data models for the Network Ops Agent.

These dataclasses/enums mirror the design document's Data Models section
exactly (see .kiro/specs/network-ops-agent/design.md). They are pure data
containers with no behavior, used across the case data generator, runbook
generator, MCP server, knowledge base retriever, case analyzer, email
composer, and orchestration layers.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Literal


@dataclass(frozen=True)
class CaseRecord:
    """A single AnyTel customer support case record."""

    case_id: str
    customer_name: str
    customer_account_id: str  # contains "AnyTel"
    issue_description: str
    network_issue_category: str
    service_location_id: str
    status: Literal["Open", "In Progress", "Resolved", "Closed"]
    created_at: str  # ISO-8601, created_at <= updated_at
    updated_at: str  # ISO-8601


@dataclass(frozen=True)
class RunbookDocument:
    """A single synthetic network operations runbook document."""

    category: str
    title: str
    description: str
    diagnostic_steps: list[str]  # >= 3, ordered
    root_cause_criteria: list[str]  # >= 2
    remediation_steps: list[str]  # >= 3, ordered


@dataclass(frozen=True)
class KBRetrievalItem:
    """A single item returned from a Knowledge_Base retrieval query."""

    text: str
    score: float
    source_uri: str
    issue_category: str | None  # from .metadata.json sidecar


class LookupResultType(Enum):
    """Classification of a Case_Lookup_MCP_Server `get_case` result."""

    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    INVALID_INPUT = "INVALID_INPUT"


@dataclass(frozen=True)
class CaseLookupResult:
    """Result of a `get_case` lookup against the Case_Table."""

    result_type: LookupResultType
    case: CaseRecord | None  # populated only when FOUND


@dataclass(frozen=True)
class ValidityDetermination:
    """The outcome of the deterministic case-validity assessment."""

    valid: bool
    matched_item: KBRetrievalItem | None
    rationale: str  # references network_issue_category and match outcome


@dataclass(frozen=True)
class CaseSummary:
    """The structured, individually labeled case analysis summary."""

    case_id: str
    customer_name: str
    issue_description: str
    validity: Literal["valid", "invalid"]
    rationale: str
    root_cause: str | None  # None when invalid
    fix_steps: list[str] | None  # None when invalid; ordered when present
    runbook_reference: str | None  # title/filename; None when invalid


@dataclass(frozen=True)
class CustomerEmail:
    """The composed customer-facing email."""

    salutation: str
    body: str
    signature: str
    full_text: str  # salutation + body + signature, concatenated


class ErrorType(Enum):
    """Classification of an analysis failure returned to the caller."""

    INVALID_INPUT = "invalid_input"
    CASE_NOT_FOUND = "case_not_found"
    LOOKUP_FAILURE = "lookup_failure"
    RETRIEVAL_FAILURE = "retrieval_failure"
    ANALYSIS_FAILURE = "analysis_failure"


@dataclass(frozen=True)
class AnalysisResult:
    """The top-level result returned by `analyze_case` to the caller."""

    success: bool
    case_summary: CaseSummary | None = None
    customer_email: CustomerEmail | None = None
    error_type: ErrorType | None = None
    error_message: str | None = None
