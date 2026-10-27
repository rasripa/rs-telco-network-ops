# Intermittent Service Degradation

## Description

This runbook covers service that remains connected overall but periodically drops, slows, or becomes unreliable for AnyTel broadband and mobile customers, as distinct from a complete outage. It applies to reports of intermittent buffering, sporadic disconnects, or speeds that vary sharply over the course of a day.

## Diagnostic Steps

1. Collect the customer's reported pattern of degradation (time of day, duration, frequency) and compare it against AnyTel network utilization graphs for the serving node to look for correlation with peak-hour congestion.
2. Review the customer's CPE event log (or remote telemetry, where available) for repeated re-synchronization events, CRC/FEC error bursts, or power-related resets over the reported period.
3. Check the serving node (OLT/DSLAM/CMTS or cell site) for recurring, non-fatal alarms such as intermittent optical power drift, thermal warnings, or transient backhaul packet loss.
4. Run a sustained (multi-hour) automated line-quality or signal quality test against the customer's connection to capture degradation events that a single point-in-time check would miss.
5. Rule out in-home factors (Wi-Fi interference, wiring, overloaded local network) as a contributor before attributing the pattern to the AnyTel network.

## Root Cause Criteria

- The reported degradation windows correlate closely with peak utilization periods on the serving node, indicating localized congestion rather than an intermittent hardware fault.
- The CPE event log or serving-node alarms show recurring, non-fatal error bursts (CRC/FEC errors, optical power drift, transient resets) clustered around the customer's reported degradation times, indicating a marginal physical-layer fault.
- Sustained line-quality testing shows stable, in-specification signal levels throughout the test window with no correlated network-side alarms, indicating the degradation is more likely caused by in-home conditions than the AnyTel network.

## Remediation Steps

1. If degradation correlates with peak-hour congestion, schedule a capacity upgrade (additional uplink capacity, carrier, or node split) for the affected serving node and provide the customer an expected resolution timeframe.
2. If recurring error bursts point to a marginal physical-layer fault (a degraded connector, splice, or aging cable segment), dispatch a field technician to test and replace the suspect segment proactively, before it fails completely.
3. If CPE-side resets correlate with the degradation pattern, remotely reboot and re-provision the customer's equipment, and replace the CPE if the reset pattern continues afterward.
4. Advise the customer on ruling out in-home contributors (Wi-Fi channel congestion, wiring, device count) when testing confirms the AnyTel network segment is within specification.
5. Re-run the sustained line-quality/signal test after remediation to confirm the degradation pattern no longer recurs before closing the case.
