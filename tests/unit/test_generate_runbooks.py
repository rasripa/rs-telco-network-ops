"""Unit tests for scripts/generate_runbooks.py: required categories and
metadata sidecar shape.

Covers Requirement 2.1 (the Runbook_Corpus must cover at least 5 distinct,
common telecommunications network issue categories, including at minimum
broadband/fixed-line connectivity outages, mobile network signal or
coverage issues, intermittent service degradation, router/CPE
configuration issues, and planned maintenance impact) and Requirement 2.4
(each Runbook_Corpus document's companion `.metadata.json` sidecar records
the document's issue category) by verifying:

- `RUNBOOK_CATEGORIES` contains at least 5 distinct categories and every
  one of the 5 required category identifiers is present.
- For every category in `RUNBOOK_CATEGORIES`, rendering and writing the
  corresponding `RunbookDocument` produces a `.md.metadata.json` sidecar
  whose JSON content is exactly
  `{"metadataAttributes": {"issue_category": category}}`, i.e. the
  sidecar's `issue_category` matches the document's own `category` field.
"""

import json
import tempfile
from pathlib import Path

from scripts.generate_runbooks import (
    RUNBOOK_CATEGORIES,
    render_runbook,
    write_runbook_document,
)

# The 5 required telecom issue types (Requirement 2.1), identified by the
# stable category slugs defined in scripts/generate_runbooks.py.
REQUIRED_CATEGORIES = {
    "broadband_fixed_line_outage",  # broadband/fixed-line connectivity outage
    "mobile_signal_coverage",  # mobile network signal/coverage issues
    "intermittent_degradation",  # intermittent service degradation
    "router_cpe_configuration",  # router/CPE configuration issues
    "planned_maintenance_impact",  # planned maintenance impact
}


def test_runbook_categories_cover_all_5_required_types():
    """RUNBOOK_CATEGORIES has >= 5 distinct entries and includes each of
    the 5 required category identifiers (Requirement 2.1)."""
    assert len(RUNBOOK_CATEGORIES) >= 5
    assert len(set(RUNBOOK_CATEGORIES)) == len(RUNBOOK_CATEGORIES)  # distinct

    for expected_category in REQUIRED_CATEGORIES:
        assert expected_category in RUNBOOK_CATEGORIES, (
            f"Expected required category '{expected_category}' to be present "
            f"in RUNBOOK_CATEGORIES, got {RUNBOOK_CATEGORIES!r}"
        )


def test_every_category_metadata_sidecar_matches_document_category():
    """For every category in RUNBOOK_CATEGORIES, render_runbook followed by
    write_runbook_document produces a `.md.metadata.json` sidecar whose
    `issue_category` matches the rendered document's own `category` field
    exactly (Requirement 2.4)."""
    for category in RUNBOOK_CATEGORIES:
        document = render_runbook(category)
        assert document.category == category

        with tempfile.TemporaryDirectory() as tmp_dir:
            markdown_path, metadata_path = write_runbook_document(
                document, output_dir=tmp_dir
            )

            assert markdown_path.exists()
            assert metadata_path.exists()
            assert metadata_path.name == f"{category}.md.metadata.json"

            sidecar_content = json.loads(metadata_path.read_text(encoding="utf-8"))

            assert sidecar_content == {
                "metadataAttributes": {"issue_category": category}
            }
            assert sidecar_content["metadataAttributes"]["issue_category"] == (
                document.category
            )
