# Slice doc plan

What a shipped slice must bring up to date. The run loop's doc phase is "read this doc and execute
it" — one `doc-writer` pass, diff-based over the whole slice.

Work from the slice's full diff across every repo it touched. Update only what the diff makes
untrue or incomplete; this is reconciliation, not a rewrite.

## The surfaces, in order

**1. `/work/AnsibleSpecs/decisions.md` — homelab doctrine.**
The authority for tool split, secrets, networking, the MAC scheme and OS update policy. Update it
when the slice **changed a standing rule**, not merely because it touched a matching area. A slice
that adds a host under the existing conventions changes no doctrine; a slice that moves namespaces
from the Terraform tier to the chart tier changes doctrine and must say so, including that it
supersedes what was there.

Keep it thin. If a decision changes, edit the existing entry rather than appending a second one —
two entries on one subject is how the next reader picks the wrong one.

**2. `/work/AnsibleSpecs/argo-cd/` — the Argo CD migration's document set.**
`brief.md`, `decisions.md`, `design.md`, `phases.md` and `history.md`: the goal posts, the
migration's own decision register, the target model, the sequencing, and how positions moved. It
is authoritative for the migration until the migration completes, and the slices cut from
`phases.md` plan against it — a planner reads the set, not the previous slice's `plan.md`. **Its
`decisions.md` is not surface 1.** That one is homelab doctrine; this one is the migration's
register, and a slice can owe an edit to either, both or neither.

Update it when the slice shipped something the set describes differently — a contract that gained
an argument, a mechanism that changed shape, a decision made mid-slice that contradicts an entry.
Judge by what the next planner would build from the set as it stands: if that is the wrong thing,
the set owes an edit. Write the new position as if it had always been true — edit the entry rather
than appending a supersession notice. `history.md` is where a moved position gets its narrative,
and it is the only file in the set that carries one.

**3. `docs/runbooks/` — operational procedures.**
One runbook per recurring operator procedure. Update a runbook when the slice changed a step, a
command, a hostname, or a failure mode it documents. Add one only when the slice created a
procedure an operator will repeat — a one-off migration does not earn a runbook, a new recurring
convergence does.

Check the runbooks that name what the slice touched, not just the obvious one. A change to the
`microk8s` role plausibly touches `k8s-upgrade.md`, `k8s-rebuild.md` and `dqlite-watch-freeze.md`.

**4. `/work/AnsibleSpecs/README.md` — the slice catalogue.**
`close_slice.py` moves the slice's entry from `## Pending` to `## Completed` mechanically, so the
doc phase does not touch that line. What it does own: if the slice changed how the tree itself
works — a new lifecycle folder, a convention — the surrounding prose is fair game.

**5. Role and module documentation.**
A role's `defaults/main.yml` is its interface: every variable a caller sets carries a comment
saying what it does. A new or renamed variable with no comment is an incomplete change. The same
goes for a Terraform module's `variables.tf` descriptions.

**6. `CLAUDE.md` and the docs it points at.**
Only when the slice changed something an agent must know every turn — a new toolchain path, a
changed authority rule, a repo that appeared or vanished. Apply the discipline in
[design-philosophy.md](design-philosophy.md): state every fact once, demote detail to a `docs/`
topic doc rather than inlining it. Growth here is a cost every future session pays.

## The architecture model — a nudge, not an edit

`docs/architecture/ansible-architecture.yaml` is this repo's federated Architecture-as-Code
artifact. **The doc phase does not edit it.** It is maintained by the `update-architecture` agent,
which is incremental and lives in the operator's `~/.claude/agents/`.

What the doc phase owes is a **nudge in its hand-back** when the slice plausibly moved the model:
a new managed host, a new daemon or service, a removed service, a renamed external identity, a
changed interface between systems. Say what changed and why it looks model-relevant. Routine role
edits need no nudge.

## What does not belong here

- **Slice documents themselves.** `plan.md` and `verification.json` are the pipeline's records;
  the run loop stamps them. Compressing a finished slice's documents down to what stays
  operationally useful is close-out work, not the doc phase's.
- **Inventing structure.** No `docs/index.md`, no per-decision id scheme, no topic docs the repo
  does not already have — unless the slice created the thing being documented.
- **Aspirational claims.** Every statement traces to code, inventory or a spec in the diff. If the
  slice left something owed to the operator, write that it is owed rather than that it is done.
