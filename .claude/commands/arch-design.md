---
description: Design a well thought out architecture with options and trade-offs, grounded in the infra repos. Proactively suggest this skill when creating complex slices that involve cross-cutting decisions or new patterns.
---

# Architecture Design

Produce a grounded architecture design document for a specific question. Argument: a short description of the architectural question (e.g., "how should the OpenBao backup pipeline split between an in-cluster collector and the iac node agent").

## When to use

Use this skill when a slice or change involves:

- Cross-area or cross-repo coordination (an Ansible role, a Terraform module, and a HelmCharts chart need to agree on an approach).
- New patterns not covered by existing conventions (a new secret-distribution path, a new VM-provisioning shape, a new internal-TLS flow).
- Structural changes that affect multiple roles, modules, or hosts.
- Decisions where there are genuine trade-offs the operator should weigh before committing.

**Do not use** for:
- Slices that follow an established role/module pattern — the dev agent's planning phase handles those.
- Implementation-level decisions within a single role or module (which task module, handler wiring, variable placement).
- Questions already answered by homelab doctrine (`../AnsibleSpecs/decisions.md`) or a runbook (`docs/runbooks/`).

## Procedure

### Step 1: Frame the question

From the operator's input, formulate a specific architectural question. A good question has:

- A clear subject (which role, module, host, or concern is being designed).
- A clear scope (what decisions need to be made).
- Context pointers (which slice, which existing code).

If the input is too vague, ask the operator to narrow it before proceeding.

### Step 2: Gather requirements

Ask the operator for their requirements — the things the design must deliver. These become fixed constraints for the agent. If the operator has already stated them (e.g., in a slice overview), extract them verbatim.

### Step 3: Dispatch the arch-design agent

Launch the `arch-design` agent with:

- **Question** — the specific architectural question.
- **Requirements** — the operator's stated requirements, listed as fixed constraints.
- **Context** — point to the relevant docs and code: homelab doctrine in `../AnsibleSpecs/decisions.md`, the relevant runbooks in `docs/runbooks/`, the roles/modules/inventory involved, and any slice-specific documents. For a cross-repo question, point at the sibling repo's code too.
- **Output path** — where the design should be written (typically `../AnsibleSpecs/slices/<SLICE_DIR>/design_<area>.md`).

### Step 4: Review with the operator

Present the design document to the operator. Walk through:

- The decisions identified and the recommended options.
- Any risks flagged (especially blast radius on real infra — destructive changes, service disruption, secret exposure).
- Open questions that need the operator's input.

Wait for the operator to review and approve the design before referencing it in slice briefs or proceeding with implementation.

### Step 5: Reference in slice work

Once approved, reference the design document from the relevant slice briefs so dev agents can read it during their planning phase.
