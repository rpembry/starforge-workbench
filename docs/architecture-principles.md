# Modular architecture guidance

Workbench is a personal reference implementation. Build a useful feature first, with boundaries that let a cohesive capability be changed or reused without dragging along its UI, transport, or host setup. Modularity is a design check, not a mandate to split packages, processes, or repositories. Keep the existing [coordinator authority boundaries](adr-coordinator-core.md) and the distinction between observations and accepted work.

## Boundaries for a new capability

- Give each optional module one purpose and an explicit owner for its state, lifecycle, and security decisions. A module may live inside the current repository and process. Avoid a second scheduler, task ledger, notification coordinator, or policy authority beside the canonical owner.
- Define small, versioned contracts at a boundary used by more than one component or likely to be persisted: inputs, outputs, capabilities, failure states, and compatibility or migration behavior. Advertise capabilities rather than assuming every installation has every provider or feature. Do not invent a generic plugin API for a single use case.
- Keep reusable rules separate from presentation and transport. UI, CLI, MCP, API, provider, and host-specific code should adapt requests to a shared capability rather than each implement its own scheduling, deduplication, policy, or state transitions. Dependencies should point from adapters toward the capability's contracts and core rules; the core should not import an adapter to operate.
- Treat configuration, extra dependencies, and state migrations as opt-in parts of an optional feature. Disabled or missing features must leave ordinary Workbench startup and existing workflows usable. Define how older state is read, migrated, or rejected and how unsupported capabilities are reported; do not silently reset durable state.
- Test contracts and authority at the boundary with synthetic fixtures: an absent module, incompatible version, restart/retry, adapter parity, and denied operations where applicable. Add integration checks only for the behavior they can actually prove.
- Extract a module into another package or repository only after a real independent consumer or lifecycle need makes the cost worthwhile. Prefer a focused in-repo boundary over a distributed monolith with duplicated orchestration and cross-service coordination.

## Applying the guidance

An internet information collector could expose a bounded, versioned observation contract while one designated scheduler controls cron-style timing and a Workbench adapter handles display and storage. A later `starforge-scrape` extraction would take the source-fetching and normalization logic only if independent use justifies it; scheduling, authorization, and state ownership must remain unambiguous. Collection output is evidence, not permission to act.

RGB notifications could be an optional integration through a versioned producer contract. RGB owns routing, delivery policy, credentials, and delivery state; Workbench owns its domain event and read model. A disconnected device or absent dependency should report unavailable delivery without changing task state or preventing the normal UI from starting. Existing privacy rules and retry semantics still apply.

A custom script hook for a lottery Calendar/Sheet workflow needs a separate, explicit execution policy and bounded inputs and outputs before any implementation. A data-only theme or configuration file is not a trusted code plugin. Third-party code execution, filesystem or network access, credentials, and side effects require explicit operator authorization and isolation appropriate to that capability. UI, CLI, MCP, and background adapters must enforce the same authentication and policy; none may bypass the canonical owner by calling a hook directly. This document does not enable script execution or grant new permissions.

## Lightweight review check

For a proposed feature, state:

1. What cohesive capability and current use case does it serve? Is an existing module enough?
2. Who owns its durable state, lifecycle, scheduling, and security policy? Which adapters may call it?
3. What contract and capabilities cross the boundary, and how do version mismatch or feature absence behave?
4. Can the reusable logic be tested without UI, CLI, MCP, provider, or host setup? Are adapter behavior and policy consistent?
5. What opt-in dependencies, configuration, migration, and synthetic contract tests are needed?
6. Is extraction justified by an independent consumer or deployment lifecycle now, or is an in-repo module simpler?

Apply this check proportionally: a small local change needs a brief answer, not a new framework or ADR. See [contributing](../CONTRIBUTING.md) for change scope and verification expectations.
