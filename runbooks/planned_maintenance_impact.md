# Planned Maintenance Impact

## Description

This runbook covers customer-reported service interruptions or degradation that coincide with scheduled AnyTel network maintenance activity, such as firmware upgrades, hardware replacements, or network reconfiguration windows, and helps distinguish expected maintenance impact from an unrelated fault.

## Diagnostic Steps

1. Check the AnyTel maintenance calendar for any scheduled or in-progress maintenance window covering the customer's serving node, cell site, or service area at the time the issue was reported.
2. Compare the customer's reported outage or degradation start time against the maintenance window's planned start and end times to confirm temporal alignment.
3. Confirm whether the maintenance activity's documented customer impact (e.g. 'brief service interruption expected') matches the type and severity of symptom the customer reported.
4. If the reported issue persists after the maintenance window's documented end time, re-check the serving equipment for a residual fault introduced by the maintenance work rather than assuming the impact is still expected.
5. Review whether the customer was included in the pre-maintenance notification list for that service area, to inform how the case response should be worded.

## Root Cause Criteria

- A scheduled AnyTel maintenance window covering the customer's serving node or service area is active, and the customer's reported start time falls within that window.
- The maintenance activity's documented expected customer impact matches the type and severity of the symptom reported (for example, a brief interruption during a firmware upgrade).
- The reported issue continues after the maintenance window's documented end time, indicating a residual fault introduced by the maintenance work rather than ongoing expected impact.

## Remediation Steps

1. If the reported impact matches an active, in-progress maintenance window, inform the customer of the expected restoration time drawn from the maintenance record and monitor for automatic recovery once the window closes.
2. If the maintenance window has already closed but the issue persists, treat the case as a residual post-maintenance fault and re-run the relevant access-network diagnostic steps (line, signal, or CPE checks) to identify what maintenance introduced.
3. If a residual fault is found, dispatch the appropriate team (field technician, RAN operations, or provisioning) to correct the specific equipment or configuration state left by the maintenance activity.
4. Confirm the customer's service is fully restored to pre-maintenance performance levels before closing the case.
5. Where the customer was not on the pre-maintenance notification list, flag the gap to the maintenance planning team so future notifications include the affected service area.
