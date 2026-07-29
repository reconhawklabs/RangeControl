# Range Definition

> Structure for `Range.md`. Every section below must appear, in this order.
> Where information is unavailable, say so under **Ingest Gaps** rather than
> inventing it — a stated gap is safe, an invented fact produces wrong rulings.

## Exercise Overview

Name, purpose, duration, participating teams, and the exercise's success condition.

## Network Topology

Segments, trust boundaries, routing, and egress paths. Note which segments each
team operates in.

## Asset Inventory

| Host | Address | Role | Criticality | Owning team |
|---|---|---|---|---|

## Required Access Paths

Connectivity that must remain functional for the exercise to proceed. One row per
path, with the reason it is required.

| Source | Destination | Port / protocol | Required for |
|---|---|---|---|

## Firewall & Policy Baseline

Existing rules and the purpose each one serves. Flag any rule that an inject
expects to be present.

## Users & Accounts

Accounts, roles, privilege level, and what depends on each remaining usable.

## Automation & Scripts

Scoring agents, scheduled tasks, service checks, beacons, and any automation that
would break if its host, account, or network path changed.

## MSEL & Inject Catalog

One entry per inject. Every entry must declare its dependencies into the
Protected Dependency Index.

### <INJECT-ID> — <short title>

- **Description:**
- **Prerequisites:**
- **Depends on:** (hosts, addresses, ports, accounts, paths)
- **Breaks if:**

## Attack Path Dependencies

Red team infrastructure, staging hosts, callback destinations, and the sequence of
footholds. One entry per stage. Every entry must declare its dependencies into the
Protected Dependency Index, exactly as inject entries do.

### <PATH-ID> — <short title>

- **Description:**
- **Requires reachable:** (hosts, addresses, ports, accounts)
- **Breaks if:**

## Scoring & Availability Requirements

What is measured, how, and the thresholds that constitute failure.

## Intentional Vulnerabilities

Deliberately planted weaknesses, and which inject or attack path each one serves.

## Out of Scope / Do Not Touch

Assets and systems participants must not modify, regardless of any other rule.

## Standing Adjudication Rules

Designer-authored blanket approvals and denials, applied before any case-by-case
reasoning.

- **Always approve:**
- **Always deny:**

## Protected Dependency Index

The load-bearing section. A flat lookup: if the element becomes blocked, disabled,
or unreachable, what breaks?

| Element | Kind | Breaks if | Dependent injects / paths |
|---|---|---|---|

## Ingest Gaps

Anything that could not be determined from the source material, and anything that
appeared ambiguous or self-contradictory.
