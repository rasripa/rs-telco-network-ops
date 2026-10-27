"""scripts/generate_runbooks.py: Runbook_Generator content definitions.

Implements the category list and the pure `render_runbook` function called
for by the design's Runbook_Generator component (design.md, "2.
Runbook_Generator"). This module is responsible for *content generation
only*:

    category (str) -> RunbookDocument

File writing (the `.md` document plus its `.md.metadata.json` sidecar) and
CLI wiring are deliberately out of scope here; they are implemented in
later tasks (5.3 and 5.5 respectively) on top of `render_runbook`.

The five category identifiers below are also intended to be imported by
`scripts/generate_case_data.py` (see design.md, "1. Case_Data_Generator")
so that synthetic "valid" case records stay consistent with the real
runbook categories authored here.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

from agent.models import RunbookDocument

# Requirement 2.1: the Runbook_Corpus must cover at least 5 distinct,
# common telecommunications network issue categories, including at
# minimum these five. The tuple order below also fixes the order in which
# a full generation run (task 5.5) authors documents.
RUNBOOK_CATEGORIES: tuple[str, ...] = (
    "broadband_fixed_line_outage",
    "mobile_signal_coverage",
    "intermittent_degradation",
    "router_cpe_configuration",
    "planned_maintenance_impact",
)
"""Module-level constant listing the 5 required runbook category
identifiers (Requirement 2.1):

- ``broadband_fixed_line_outage``: broadband/fixed-line connectivity
  outages (DSL, fiber, cable access loss).
- ``mobile_signal_coverage``: mobile network signal or coverage issues.
- ``intermittent_degradation``: intermittent service degradation (that
  is not a full outage).
- ``router_cpe_configuration``: router/customer-premises-equipment
  configuration issues.
- ``planned_maintenance_impact``: customer-visible impact from planned
  network maintenance.

These slugs are stable identifiers used as the ``category``/
``network_issue_category`` value across the Runbook_Corpus, the
Case_Data_Generator, and the Knowledge_Base's ``.metadata.json``
sidecars, so that category matching (Requirement 5) is an exact string
comparison rather than free-text parsing.
"""


@dataclass(frozen=True)
class _CategoryContent:
    """Static, hand-authored content for one runbook category.

    Internal to this module; `render_runbook` is the public interface
    that turns a category identifier into a `RunbookDocument`.
    """

    title: str
    description: str
    diagnostic_steps: tuple[str, ...]
    root_cause_criteria: tuple[str, ...]
    remediation_steps: tuple[str, ...]


_CATEGORY_CONTENT: dict[str, _CategoryContent] = {
    "broadband_fixed_line_outage": _CategoryContent(
        title="Broadband / Fixed-Line Connectivity Outage",
        description=(
            "This runbook covers complete or near-complete loss of broadband "
            "and fixed-line internet connectivity for AnyTel residential and "
            "business customers across DSL, fiber, and cable access "
            "technologies. It applies when a customer reports no internet "
            "access, a modem/router with no WAN sync, or a service that "
            "dropped and never reconnected."
        ),
        diagnostic_steps=(
            "Confirm the customer's premises equipment (modem/ONT/router) has "
            "power and check whether the WAN/uplink status LED is off, red, "
            "or continuously blinking rather than solid green.",
            "Check the AnyTel network operations dashboard for an active "
            "outage ticket or maintenance event covering the customer's "
            "service location or serving node (OLT/DSLAM/CMTS).",
            "Run a remote loop/line qualification test (DSL sync stats, "
            "optical power levels, or cable modem signal levels) against the "
            "customer's line to check for a hard line fault versus a signal "
            "quality issue.",
            "Ping and traceroute the customer's provisioned IP address (or "
            "the CPE's management address, if remotely reachable) from the "
            "AnyTel core network to confirm whether the last responding hop "
            "is upstream of or at the customer premises.",
            "Review the customer's account and billing status to rule out a "
            "service suspension or a pending disconnection as the cause of "
            "the outage.",
        ),
        root_cause_criteria=(
            "The optical line terminal (OLT), DSLAM, or CMTS port serving the "
            "customer's line reports a hard failure, port-down alarm, or "
            "sustained high error rate in the AnyTel network monitoring "
            "system.",
            "Loop/line qualification returns no signal, no optical light "
            "level, or attenuation far outside the technology's supported "
            "range, indicating a physical cable, splice, or fiber cut rather "
            "than a configuration issue.",
            "The customer's account shows an active suspension, non-payment "
            "hold, or pending disconnection, indicating the outage is a "
            "billing-driven service state rather than a network fault.",
        ),
        remediation_steps=(
            "If an active outage or maintenance event already covers the "
            "affected node, link the case to that event and communicate the "
            "estimated restoration time to the customer rather than opening "
            "duplicate work.",
            "If loop/line qualification shows a physical fault (fiber cut, "
            "damaged drop cable, degraded splice), dispatch a field "
            "technician to inspect and repair the physical plant between the "
            "serving node and the customer's premises.",
            "If the OLT/DSLAM/CMTS port itself shows a hard failure with the "
            "physical plant otherwise healthy, initiate a port swap or "
            "card/line-card replacement on the serving equipment.",
            "Once the physical/port issue is resolved, remotely re-provision "
            "and re-synchronize the customer's CPE, and confirm WAN sync and "
            "successful IP address acquisition before closing the case.",
            "If the outage was caused by a billing hold rather than a "
            "network fault, route the case to the billing team for account "
            "resolution instead of dispatching a field technician.",
        ),
    ),
    "mobile_signal_coverage": _CategoryContent(
        title="Mobile Network Signal or Coverage Issues",
        description=(
            "This runbook covers reports of weak, absent, or unreliable "
            "mobile signal for AnyTel wireless customers, including "
            "no-service indicators, frequent dropped calls, and degraded "
            "data speeds tied to a specific location rather than a device "
            "configuration problem."
        ),
        diagnostic_steps=(
            "Confirm the customer's reported location and cross-reference it "
            "against the AnyTel radio access network (RAN) coverage map to "
            "identify the serving cell site(s) for that location.",
            "Check the serving cell site(s) in the AnyTel RAN element "
            "management system for active alarms, reduced transmit power, "
            "or a sector/carrier taken out of service.",
            "Review recent RAN performance counters (dropped call rate, "
            "radio link failure rate, RSRP/RSRQ where available) for the "
            "serving cell(s) over the period the customer has reported "
            "issues.",
            "Verify the customer's device and SIM are provisioned correctly "
            "and are not roaming onto a partner network with weaker "
            "coverage in that area.",
            "Check for any planned or emergency maintenance affecting the "
            "serving cell site(s) that would account for a temporary "
            "coverage reduction.",
        ),
        root_cause_criteria=(
            "The serving cell site has an active hardware, transmission, or "
            "backhaul alarm, or a sector/carrier is administratively out of "
            "service, in the AnyTel RAN element management system.",
            "RAN performance counters for the serving cell show a sustained "
            "increase in dropped calls or radio link failures beginning at "
            "the time the customer's issue started, indicating a network-side "
            "degradation rather than a device issue.",
            "The customer's device is confirmed to be roaming on a partner "
            "network with materially weaker coverage in that location, "
            "indicating the reported weak signal is expected given current "
            "AnyTel home-network coverage rather than a fault.",
        ),
        remediation_steps=(
            "If the serving cell site has an active alarm, escalate to RAN "
            "field operations to restore the affected sector, carrier, or "
            "backhaul link, and log the outage window against the case.",
            "If a sector/carrier was administratively disabled without a "
            "corresponding maintenance record, re-enable it after confirming "
            "with RAN operations that it is safe to do so.",
            "If performance counters show a capacity-driven degradation "
            "rather than a hard fault, schedule a capacity add (additional "
            "carrier or small cell) for the affected area and provide the "
            "customer an interim workaround (e.g. Wi-Fi calling) where "
            "available.",
            "If the customer's device is roaming unnecessarily, correct the "
            "SIM/network-selection provisioning so the device prefers the "
            "AnyTel home network where coverage is available.",
            "Once network-side remediation is complete, confirm restored "
            "signal quality with the customer at their reported location "
            "before closing the case.",
        ),
    ),
    "intermittent_degradation": _CategoryContent(
        title="Intermittent Service Degradation",
        description=(
            "This runbook covers service that remains connected overall but "
            "periodically drops, slows, or becomes unreliable for AnyTel "
            "broadband and mobile customers, as distinct from a complete "
            "outage. It applies to reports of intermittent buffering, "
            "sporadic disconnects, or speeds that vary sharply over the "
            "course of a day."
        ),
        diagnostic_steps=(
            "Collect the customer's reported pattern of degradation "
            "(time of day, duration, frequency) and compare it against "
            "AnyTel network utilization graphs for the serving node to look "
            "for correlation with peak-hour congestion.",
            "Review the customer's CPE event log (or remote telemetry, where "
            "available) for repeated re-synchronization events, CRC/FEC "
            "error bursts, or power-related resets over the reported period.",
            "Check the serving node (OLT/DSLAM/CMTS or cell site) for "
            "recurring, non-fatal alarms such as intermittent optical power "
            "drift, thermal warnings, or transient backhaul packet loss.",
            "Run a sustained (multi-hour) automated line-quality or signal "
            "quality test against the customer's connection to capture "
            "degradation events that a single point-in-time check would "
            "miss.",
            "Rule out in-home factors (Wi-Fi interference, wiring, "
            "overloaded local network) as a contributor before attributing "
            "the pattern to the AnyTel network.",
        ),
        root_cause_criteria=(
            "The reported degradation windows correlate closely with peak "
            "utilization periods on the serving node, indicating localized "
            "congestion rather than an intermittent hardware fault.",
            "The CPE event log or serving-node alarms show recurring, "
            "non-fatal error bursts (CRC/FEC errors, optical power drift, "
            "transient resets) clustered around the customer's reported "
            "degradation times, indicating a marginal physical-layer fault.",
            "Sustained line-quality testing shows stable, in-specification "
            "signal levels throughout the test window with no correlated "
            "network-side alarms, indicating the degradation is more likely "
            "caused by in-home conditions than the AnyTel network.",
        ),
        remediation_steps=(
            "If degradation correlates with peak-hour congestion, schedule a "
            "capacity upgrade (additional uplink capacity, carrier, or node "
            "split) for the affected serving node and provide the customer "
            "an expected resolution timeframe.",
            "If recurring error bursts point to a marginal physical-layer "
            "fault (a degraded connector, splice, or aging cable segment), "
            "dispatch a field technician to test and replace the "
            "suspect segment proactively, before it fails completely.",
            "If CPE-side resets correlate with the degradation pattern, "
            "remotely reboot and re-provision the customer's equipment, and "
            "replace the CPE if the reset pattern continues afterward.",
            "Advise the customer on ruling out in-home contributors (Wi-Fi "
            "channel congestion, wiring, device count) when testing confirms "
            "the AnyTel network segment is within specification.",
            "Re-run the sustained line-quality/signal test after remediation "
            "to confirm the degradation pattern no longer recurs before "
            "closing the case.",
        ),
    ),
    "router_cpe_configuration": _CategoryContent(
        title="Router / Customer-Premises-Equipment Configuration Issues",
        description=(
            "This runbook covers connectivity problems caused by the "
            "configuration of the customer's router or other "
            "customer-premises equipment (CPE) rather than by the AnyTel "
            "access network itself, including incorrect Wi-Fi settings, "
            "firmware issues, and provisioning mismatches after a CPE swap "
            "or self-install."
        ),
        diagnostic_steps=(
            "Confirm that the AnyTel access network is healthy for the "
            "customer's line or cell (no outage, alarm, or degraded signal) "
            "so that CPE configuration can be evaluated as an isolated "
            "cause rather than in the presence of a network fault.",
            "Remotely query the CPE's current configuration (WAN mode, "
            "DHCP/PPPoE credentials, firmware version, Wi-Fi channel/band "
            "settings) where the device supports remote management, and "
            "compare it against the AnyTel standard provisioning profile.",
            "Check whether the customer recently replaced, factory-reset, or "
            "self-installed their router, since these events commonly leave "
            "the WAN credentials or provisioning profile unset or mismatched.",
            "Ask the customer to confirm whether the issue affects a single "
            "device or all devices on the home network, to distinguish a "
            "device-specific Wi-Fi problem from a router-wide configuration "
            "problem.",
            "Review recent firmware release notes for the customer's CPE "
            "model for known configuration or connectivity defects matching "
            "the reported symptoms.",
        ),
        root_cause_criteria=(
            "The CPE's WAN configuration (PPPoE credentials, DHCP mode, VLAN "
            "tagging) does not match the AnyTel standard provisioning "
            "profile for that access technology, despite the underlying "
            "access network being healthy.",
            "The issue began immediately after a router replacement, factory "
            "reset, or self-install, and the customer's prior working "
            "configuration was not restored or re-applied.",
            "The CPE is running a firmware version with a documented, known "
            "connectivity or Wi-Fi defect that matches the customer's "
            "reported symptoms.",
        ),
        remediation_steps=(
            "Remotely push the correct AnyTel standard provisioning profile "
            "(WAN credentials, DHCP/PPPoE mode, VLAN tagging) to the CPE and "
            "confirm the device successfully re-establishes its WAN "
            "connection.",
            "If the CPE is on a defective firmware version, schedule or "
            "trigger a firmware update to the latest supported release and "
            "confirm the reported symptom clears afterward.",
            "If the router was recently replaced or factory-reset, walk the "
            "customer through re-entering their AnyTel account credentials "
            "and re-applying any custom Wi-Fi settings (SSID, password, "
            "band steering) they previously relied on.",
            "If the problem is isolated to Wi-Fi (rather than the WAN "
            "connection itself), adjust the router's channel/band "
            "configuration to reduce interference and verify improved "
            "signal with the customer.",
            "Confirm end-to-end connectivity from the customer's device out "
            "to the AnyTel network after configuration changes are applied "
            "before closing the case.",
        ),
    ),
    "planned_maintenance_impact": _CategoryContent(
        title="Planned Maintenance Impact",
        description=(
            "This runbook covers customer-reported service interruptions or "
            "degradation that coincide with scheduled AnyTel network "
            "maintenance activity, such as firmware upgrades, hardware "
            "replacements, or network reconfiguration windows, and helps "
            "distinguish expected maintenance impact from an unrelated "
            "fault."
        ),
        diagnostic_steps=(
            "Check the AnyTel maintenance calendar for any scheduled or "
            "in-progress maintenance window covering the customer's serving "
            "node, cell site, or service area at the time the issue was "
            "reported.",
            "Compare the customer's reported outage or degradation start "
            "time against the maintenance window's planned start and end "
            "times to confirm temporal alignment.",
            "Confirm whether the maintenance activity's documented customer "
            "impact (e.g. 'brief service interruption expected') matches the "
            "type and severity of symptom the customer reported.",
            "If the reported issue persists after the maintenance window's "
            "documented end time, re-check the serving equipment for a "
            "residual fault introduced by the maintenance work rather than "
            "assuming the impact is still expected.",
            "Review whether the customer was included in the pre-maintenance "
            "notification list for that service area, to inform how the "
            "case response should be worded.",
        ),
        root_cause_criteria=(
            "A scheduled AnyTel maintenance window covering the customer's "
            "serving node or service area is active, and the customer's "
            "reported start time falls within that window.",
            "The maintenance activity's documented expected customer impact "
            "matches the type and severity of the symptom reported (for "
            "example, a brief interruption during a firmware upgrade).",
            "The reported issue continues after the maintenance window's "
            "documented end time, indicating a residual fault introduced by "
            "the maintenance work rather than ongoing expected impact.",
        ),
        remediation_steps=(
            "If the reported impact matches an active, in-progress "
            "maintenance window, inform the customer of the expected "
            "restoration time drawn from the maintenance record and monitor "
            "for automatic recovery once the window closes.",
            "If the maintenance window has already closed but the issue "
            "persists, treat the case as a residual post-maintenance fault "
            "and re-run the relevant access-network diagnostic steps (line, "
            "signal, or CPE checks) to identify what maintenance introduced.",
            "If a residual fault is found, dispatch the appropriate team "
            "(field technician, RAN operations, or provisioning) to correct "
            "the specific equipment or configuration state left by the "
            "maintenance activity.",
            "Confirm the customer's service is fully restored to "
            "pre-maintenance performance levels before closing the case.",
            "Where the customer was not on the pre-maintenance notification "
            "list, flag the gap to the maintenance planning team so future "
            "notifications include the affected service area.",
        ),
    ),
}


def render_runbook(category: str) -> RunbookDocument:
    """Render the runbook content for a single issue category.

    Pure function: given one of the identifiers in `RUNBOOK_CATEGORIES`,
    returns a `RunbookDocument` with a non-empty title and description,
    at least 3 diagnostic steps in a fixed, sequential order, at least 2
    root-cause criteria, and at least 3 remediation steps in a fixed,
    sequential order (Requirement 2.2). AnyTel is referenced by name at
    least once within the document's content (Requirement 2.5).

    No file I/O is performed here; writing the rendered document (and its
    `.metadata.json` sidecar) to `runbooks/` is implemented separately.

    Args:
        category: One of the identifiers in `RUNBOOK_CATEGORIES`.

    Returns:
        A `RunbookDocument` populated with realistic, category-specific
        content.

    Raises:
        ValueError: if `category` is not one of `RUNBOOK_CATEGORIES`.
    """
    content = _CATEGORY_CONTENT.get(category)
    if content is None:
        valid = ", ".join(RUNBOOK_CATEGORIES)
        raise ValueError(
            f"Unknown runbook category '{category}'. Valid categories: {valid}"
        )

    return RunbookDocument(
        category=category,
        title=content.title,
        description=content.description,
        diagnostic_steps=list(content.diagnostic_steps),
        root_cause_criteria=list(content.root_cause_criteria),
        remediation_steps=list(content.remediation_steps),
    )


def _render_markdown(document: RunbookDocument) -> str:
    """Render a `RunbookDocument` as Markdown text.

    The title is rendered as an H1, the description as a prose paragraph,
    and each of `diagnostic_steps`, `root_cause_criteria`, and
    `remediation_steps` as its own labeled ATX (`##`) section followed by
    an itemized list, in the same field order as `RunbookDocument`.

    The "Root Cause Criteria" and "Remediation Steps" headings are spelled
    exactly that way (matching `agent/case_analyzer.py`'s
    `extract_root_cause_and_fix`, which classifies a heading as
    introducing the root-cause section when its text contains "root
    cause", and the fix section when its text contains "remediation" or
    "fix") so that content retrieved from this rendered document parses
    back into the same root-cause finding and ordered fix steps authored
    here.

    The description is placed under its own "## Description" heading
    (rather than directly under the H1 title) so that
    `extract_root_cause_and_fix`'s heading scanner - which classifies
    *any* heading whose text contains "fix" as introducing the fix
    section - cannot mistake the H1 title for a fix-section heading when
    a category's title itself happens to contain "fix" as a substring
    (for example "Fixed-Line" in the broadband/fixed-line outage title).
    The "## Description" heading immediately resets the scanner's section
    state to "other" before any prose is consumed, so the title heading's
    (possible) misclassification never causes content to leak into the
    parsed root cause or fix steps.

    Args:
        document: The `RunbookDocument` to render.

    Returns:
        The complete Markdown text for `document`.
    """
    lines: list[str] = [
        f"# {document.title}",
        "",
        "## Description",
        "",
        document.description,
        "",
    ]

    lines.append("## Diagnostic Steps")
    lines.append("")
    for index, step in enumerate(document.diagnostic_steps, start=1):
        lines.append(f"{index}. {step}")
    lines.append("")

    lines.append("## Root Cause Criteria")
    lines.append("")
    for criterion in document.root_cause_criteria:
        lines.append(f"- {criterion}")
    lines.append("")

    lines.append("## Remediation Steps")
    lines.append("")
    for index, step in enumerate(document.remediation_steps, start=1):
        lines.append(f"{index}. {step}")
    lines.append("")

    return "\n".join(lines)


def write_runbook_document(
    document: RunbookDocument,
    output_dir: str | Path = "runbooks",
) -> tuple[Path, Path]:
    """Write `document` as a Markdown file plus its `.metadata.json` sidecar.

    Renders `document` to Markdown (see `_render_markdown`) and writes it,
    UTF-8 encoded, as `<output_dir>/<document.category>.md`, creating
    `output_dir` first if it does not already exist (Requirement 2.3,
    Requirement 2.4). `document.category` is one of the filesystem-safe
    slugs in `RUNBOOK_CATEGORIES`, so it is used directly as the
    filename's stem/slug.

    Alongside the `.md` file, writes a companion
    `<output_dir>/<document.category>.md.metadata.json` sidecar containing
    `{"metadataAttributes": {"issue_category": document.category}}`, the
    sidecar shape Amazon Bedrock knowledge bases expect for attaching
    filterable metadata to ingested content (design.md, "2.
    Runbook_Generator").

    Writing is idempotent: re-running this function for the same
    `document.category` overwrites both files with the (possibly updated)
    rendered content rather than appending or erroring.

    Args:
        document: The `RunbookDocument` to write.
        output_dir: Directory the `.md` file and its sidecar are written
            into. Defaults to `"runbooks"`. Created (including any missing
            parent directories) if it does not already exist.

    Returns:
        A `(markdown_path, metadata_path)` tuple with the paths of the
        written `.md` file and its `.md.metadata.json` sidecar,
        respectively.
    """
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)

    slug = document.category
    markdown_path = directory / f"{slug}.md"
    metadata_path = directory / f"{slug}.md.metadata.json"

    markdown_content = _render_markdown(document)
    markdown_path.write_text(markdown_content, encoding="utf-8")

    metadata_content = {"metadataAttributes": {"issue_category": document.category}}
    metadata_path.write_text(
        json.dumps(metadata_content, indent=2) + "\n", encoding="utf-8"
    )

    return markdown_path, metadata_path


# --- CLI entrypoint (task 5.5) ----------------------------------------------
#
# Wires the pure `render_runbook` function and the `write_runbook_document`
# I/O writer together into a runnable script: for every category in
# `RUNBOOK_CATEGORIES`, render the document and write it (plus its
# `.metadata.json` sidecar) into the output directory, producing the
# complete Runbook_Corpus in a single invocation (Requirements 2.1, 2.2,
# 2.3, 2.4, 2.5). This performs local filesystem I/O only -- no AWS calls
# are made here (Knowledge Base upload/ingestion is a separate, later
# script per design.md).


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments for the Runbook_Generator script."""
    parser = argparse.ArgumentParser(
        description=(
            "Generate the AnyTel Runbook_Corpus: one Markdown runbook "
            "document plus a .metadata.json sidecar per issue category, "
            "written into the output directory."
        )
    )
    parser.add_argument(
        "--output-dir",
        default="runbooks",
        help=(
            "Directory the Runbook_Corpus (.md documents and "
            ".metadata.json sidecars) is written into. Defaults to "
            "'runbooks'."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    """Generate and write the complete Runbook_Corpus.

    Iterates every category in `RUNBOOK_CATEGORIES`, in order, calling
    `render_runbook(category)` to produce the document's content and then
    `write_runbook_document(document, output_dir=...)` to write the `.md`
    file and its `.md.metadata.json` sidecar. Prints a short summary of
    how many documents were written and where.

    Args:
        argv: Optional argument list (defaults to `sys.argv[1:]` via
            `argparse` when omitted). Exposed mainly so this function is
            callable directly from tests without going through the
            process's real command line.
    """
    args = _parse_args(argv)

    written_paths: list[tuple[Path, Path]] = []
    for category in RUNBOOK_CATEGORIES:
        document = render_runbook(category)
        markdown_path, metadata_path = write_runbook_document(
            document, output_dir=args.output_dir
        )
        written_paths.append((markdown_path, metadata_path))
        print(f"Wrote {markdown_path.name} and {metadata_path.name}")

    output_dir = Path(args.output_dir).resolve()
    print(
        f"Runbook_Generator: wrote {len(written_paths)} runbook document(s) "
        f"(plus metadata sidecars) to {output_dir}"
    )


if __name__ == "__main__":
    main()
