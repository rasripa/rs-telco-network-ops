# Broadband / Fixed-Line Connectivity Outage

## Description

This runbook covers complete or near-complete loss of broadband and fixed-line internet connectivity for AnyTel residential and business customers across DSL, fiber, and cable access technologies. It applies when a customer reports no internet access, a modem/router with no WAN sync, or a service that dropped and never reconnected.

## Diagnostic Steps

1. Confirm the customer's premises equipment (modem/ONT/router) has power and check whether the WAN/uplink status LED is off, red, or continuously blinking rather than solid green.
2. Check the AnyTel network operations dashboard for an active outage ticket or maintenance event covering the customer's service location or serving node (OLT/DSLAM/CMTS).
3. Run a remote loop/line qualification test (DSL sync stats, optical power levels, or cable modem signal levels) against the customer's line to check for a hard line fault versus a signal quality issue.
4. Ping and traceroute the customer's provisioned IP address (or the CPE's management address, if remotely reachable) from the AnyTel core network to confirm whether the last responding hop is upstream of or at the customer premises.
5. Review the customer's account and billing status to rule out a service suspension or a pending disconnection as the cause of the outage.

## Root Cause Criteria

- The optical line terminal (OLT), DSLAM, or CMTS port serving the customer's line reports a hard failure, port-down alarm, or sustained high error rate in the AnyTel network monitoring system.
- Loop/line qualification returns no signal, no optical light level, or attenuation far outside the technology's supported range, indicating a physical cable, splice, or fiber cut rather than a configuration issue.
- The customer's account shows an active suspension, non-payment hold, or pending disconnection, indicating the outage is a billing-driven service state rather than a network fault.

## Remediation Steps

1. If an active outage or maintenance event already covers the affected node, link the case to that event and communicate the estimated restoration time to the customer rather than opening duplicate work.
2. If loop/line qualification shows a physical fault (fiber cut, damaged drop cable, degraded splice), dispatch a field technician to inspect and repair the physical plant between the serving node and the customer's premises.
3. If the OLT/DSLAM/CMTS port itself shows a hard failure with the physical plant otherwise healthy, initiate a port swap or card/line-card replacement on the serving equipment.
4. Once the physical/port issue is resolved, remotely re-provision and re-synchronize the customer's CPE, and confirm WAN sync and successful IP address acquisition before closing the case.
5. If the outage was caused by a billing hold rather than a network fault, route the case to the billing team for account resolution instead of dispatching a field technician.
