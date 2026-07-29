## Exercise Overview
Operation Fixture, a two-day defensive exercise.

## Network Topology
Server segment 10.77.0.0/16. Workstation segment 10.78.0.0/16.

## Asset Inventory
| Host | Address | Role | Criticality | Owning team |
|---|---|---|---|---|
| DC-VULCAN | 10.77.0.10 | domain controller | critical | blue |
| SVC-FORGE | 10.77.0.20 | build server | high | blue |

## Required Access Paths
| Source | Destination | Port / protocol | Required for |
|---|---|---|---|
| SVC-FORGE | 203.0.113.44 | 443/tcp | MSEL-INJECT-ALPHA callback |

## Firewall & Policy Baseline
Egress to 203.0.113.44 is permitted and must stay permitted.

## Users & Accounts
svc-vulcan-backup — service account for the nightly backup job.

## Automation & Scripts
Nightly backup runs as svc-vulcan-backup against DC-VULCAN.

## MSEL & Inject Catalog
### MSEL-INJECT-ALPHA — initial foothold
- **Description:** Red team establishes a callback from SVC-FORGE.
- **Depends on:** SVC-FORGE, 203.0.113.44, 443/tcp
- **Breaks if:** egress to 203.0.113.44 is blocked

## Attack Path Dependencies
Staging host 203.0.113.44 serves MSEL-INJECT-ALPHA.

## Scoring & Availability Requirements
DC-VULCAN must answer LDAP throughout.

## Intentional Vulnerabilities
SVC-FORGE runs an unpatched build agent.

## Out of Scope / Do Not Touch
The scoring collector.

## Standing Adjudication Rules
- **Always deny:** anything that blocks egress to 203.0.113.44.

## Protected Dependency Index
| Element | Kind | Breaks if | Dependent injects / paths |
|---|---|---|---|
| 203.0.113.44 | address | blocked | MSEL-INJECT-ALPHA |
| SVC-FORGE | host | offline | MSEL-INJECT-ALPHA |
| svc-vulcan-backup | account | disabled | nightly backup |

## Ingest Gaps
None.
