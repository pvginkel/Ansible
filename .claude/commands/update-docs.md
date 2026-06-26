---
description: Bring the repo's living docs into line with reality — homelab doctrine (decisions.md), the operational runbooks, and the slice index. Reconciles drift against what the code actually does. Argument (optional): a hint to focus on (an area, a runbook, or "absorb the decision about X").
---

# Update Docs

Get the docs current. One job: make the repo's living documentation reflect how the
infrastructure actually works and why. This repo's living docs are:

- **`../AnsibleSpecs/decisions.md`** — homelab doctrine (tool split, secrets, networking, MAC scheme, OS update policy, standing conventions).
- **`docs/runbooks/`** — perpetual operational runbooks (Proxmox, Kubernetes, Ceph, Linux, network, upgrades).
- **`../AnsibleSpecs/slices/README.md`** — the lean slice catalogue.

There are **no modes** — you reconcile whatever has drifted. An optional **hint** narrows the
focus: an area (`the microk8s role`), a runbook (`the k8s-upgrade runbook`), or a decision to
reconcile (`the OS update policy`). With no hint, the whole living-doc set is in scope.

> This repo has **not** adopted the federated topic-doc / decision-index documentation model — that
> is a deferred Triage item. Do not invent `docs/index.md`, topic docs, or `DNNN` decision ids.
> Target the three surfaces above as they exist today.

## Why this is a skill, not an agent

Sweeping the doc set means surveying it whole — best done by fanning out several **Explore
sub-agents in parallel**, keeping their conclusions and not the file dumps. A sub-agent can't spawn
sub-agents, so this runs in the **main conversation**, where it can fan out.

## Read first

1. The target's existing docs — the relevant section of `decisions.md`, the runbook(s) in scope, or the slice index.
2. The sources of truth, for the scope in question:
   - the **code** (ground every claim in it — roles, playbooks, inventory, Terraform modules, and the sibling repos where relevant);
   - `../AnsibleSpecs/slices/**` overviews + acceptance criteria (the design intent behind changes), including `slices/completed/`;
   - recent `git log` for the scope (what changed since the docs were last touched).

## Steps

1. **Scope it.** From the hint (or the whole set by default) pick what to work: a slice of `decisions.md`, one or more runbooks, the slice index, or all of it.

2. **Survey — fan out Explore agents.** Launch Explore agents in parallel (one per scope or area). Ask each to return, with `file:line` evidence:
   - the real design, conventions, and behaviours of the scope (what an operator or a future-Claude would need to know);
   - what is already documented, and where the docs have **drifted** from the code;
   - **gaps** — doctrine or operational knowledge that lives only in code or slices with no doc home.
   Conclusions, not file contents.

3. **Triage the non-obvious with the operator.** Where the call isn't clear — is this a standing doctrine change or a one-off? is this runbook step still true? — surface it and let the operator decide. Don't invent rules and don't preserve stale ones unilaterally. Routine, well-grounded updates need no checkpoint; this is maintenance, not a design review.

4. **Author / update.**
   - Fix drift: where the code and a doc disagree, the code wins — correct the doc and say so.
   - **Doctrine** goes in `decisions.md`, stated as the standing decision (not a dated log entry), in the voice of the surrounding entries. Keep it the thin doctrine record it already is.
   - **Operational how-to** goes in the owning runbook under `docs/runbooks/` — the actual procedure, current.
   - Keep the slice index (`slices/README.md`) honest: each entry one line, in the right Pending/Completed/Deferred section.

5. **Validate.** Every claim grounded in code or spec. No invented or aspirational conventions. Links resolve. The slice index matches the slices on disk.

6. **Commit.** Per the repo's commit-as-you-go rule, in the repo each file lives in: runbooks in `/work/Ansible` (operator: `~/source/Ansible`), `decisions.md` and the slice index in `../AnsibleSpecs`. Stage only the files you touched.

## Constraints

- **Ground everything.** If a "rule" isn't real in the code or specs, it doesn't go in a doc. When a source contradicts a doc, the code wins — fix the doc and say so.
- **Don't restate** `CLAUDE.md` or the code. Document the doctrine and the procedures; link the rest.
- **Keep `decisions.md` doctrine, not narrative.** Standing decisions, stated as the design — not a changelog.
- **No tombstones.** A superseded convention is a rewrite, not an appended changelog.
- **The hint focuses; it doesn't widen.** No hint means the whole living-doc set; a hint means just that slice of it.
