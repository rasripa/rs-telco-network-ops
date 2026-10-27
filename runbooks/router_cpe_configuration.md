# Router / Customer-Premises-Equipment Configuration Issues

## Description

This runbook covers connectivity problems caused by the configuration of the customer's router or other customer-premises equipment (CPE) rather than by the AnyTel access network itself, including incorrect Wi-Fi settings, firmware issues, and provisioning mismatches after a CPE swap or self-install.

## Diagnostic Steps

1. Confirm that the AnyTel access network is healthy for the customer's line or cell (no outage, alarm, or degraded signal) so that CPE configuration can be evaluated as an isolated cause rather than in the presence of a network fault.
2. Remotely query the CPE's current configuration (WAN mode, DHCP/PPPoE credentials, firmware version, Wi-Fi channel/band settings) where the device supports remote management, and compare it against the AnyTel standard provisioning profile.
3. Check whether the customer recently replaced, factory-reset, or self-installed their router, since these events commonly leave the WAN credentials or provisioning profile unset or mismatched.
4. Ask the customer to confirm whether the issue affects a single device or all devices on the home network, to distinguish a device-specific Wi-Fi problem from a router-wide configuration problem.
5. Review recent firmware release notes for the customer's CPE model for known configuration or connectivity defects matching the reported symptoms.

## Root Cause Criteria

- The CPE's WAN configuration (PPPoE credentials, DHCP mode, VLAN tagging) does not match the AnyTel standard provisioning profile for that access technology, despite the underlying access network being healthy.
- The issue began immediately after a router replacement, factory reset, or self-install, and the customer's prior working configuration was not restored or re-applied.
- The CPE is running a firmware version with a documented, known connectivity or Wi-Fi defect that matches the customer's reported symptoms.

## Remediation Steps

1. Remotely push the correct AnyTel standard provisioning profile (WAN credentials, DHCP/PPPoE mode, VLAN tagging) to the CPE and confirm the device successfully re-establishes its WAN connection.
2. If the CPE is on a defective firmware version, schedule or trigger a firmware update to the latest supported release and confirm the reported symptom clears afterward.
3. If the router was recently replaced or factory-reset, walk the customer through re-entering their AnyTel account credentials and re-applying any custom Wi-Fi settings (SSID, password, band steering) they previously relied on.
4. If the problem is isolated to Wi-Fi (rather than the WAN connection itself), adjust the router's channel/band configuration to reduce interference and verify improved signal with the customer.
5. Confirm end-to-end connectivity from the customer's device out to the AnyTel network after configuration changes are applied before closing the case.
