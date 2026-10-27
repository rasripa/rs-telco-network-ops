# Mobile Network Signal or Coverage Issues

## Description

This runbook covers reports of weak, absent, or unreliable mobile signal for AnyTel wireless customers, including no-service indicators, frequent dropped calls, and degraded data speeds tied to a specific location rather than a device configuration problem.

## Diagnostic Steps

1. Confirm the customer's reported location and cross-reference it against the AnyTel radio access network (RAN) coverage map to identify the serving cell site(s) for that location.
2. Check the serving cell site(s) in the AnyTel RAN element management system for active alarms, reduced transmit power, or a sector/carrier taken out of service.
3. Review recent RAN performance counters (dropped call rate, radio link failure rate, RSRP/RSRQ where available) for the serving cell(s) over the period the customer has reported issues.
4. Verify the customer's device and SIM are provisioned correctly and are not roaming onto a partner network with weaker coverage in that area.
5. Check for any planned or emergency maintenance affecting the serving cell site(s) that would account for a temporary coverage reduction.

## Root Cause Criteria

- The serving cell site has an active hardware, transmission, or backhaul alarm, or a sector/carrier is administratively out of service, in the AnyTel RAN element management system.
- RAN performance counters for the serving cell show a sustained increase in dropped calls or radio link failures beginning at the time the customer's issue started, indicating a network-side degradation rather than a device issue.
- The customer's device is confirmed to be roaming on a partner network with materially weaker coverage in that location, indicating the reported weak signal is expected given current AnyTel home-network coverage rather than a fault.

## Remediation Steps

1. If the serving cell site has an active alarm, escalate to RAN field operations to restore the affected sector, carrier, or backhaul link, and log the outage window against the case.
2. If a sector/carrier was administratively disabled without a corresponding maintenance record, re-enable it after confirming with RAN operations that it is safe to do so.
3. If performance counters show a capacity-driven degradation rather than a hard fault, schedule a capacity add (additional carrier or small cell) for the affected area and provide the customer an interim workaround (e.g. Wi-Fi calling) where available.
4. If the customer's device is roaming unnecessarily, correct the SIM/network-selection provisioning so the device prefers the AnyTel home network where coverage is available.
5. Once network-side remediation is complete, confirm restored signal quality with the customer at their reported location before closing the case.
