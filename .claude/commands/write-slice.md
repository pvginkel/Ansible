# Write Slice

Author an implementation slice. **Required input: a change-request bundle** produced by `/triage`,
under `../AnsibleSpecs/change_requests/<slug>/`. Argument: the path to that bundle (or its slug).

A slice is never authored from a bare request — it is authored from a bundle. If you were handed a
raw request with no bundle, stop and run `/triage` first (the only exception is the narrow
interactive minimal-change path described in `/triage`, which still produces the bundle).

**Normative keywords.** MUST / MUST NOT / SHOULD / SHOULD NOT / MAY in the slice's acceptance
criteria, briefs, and overview carry their RFC 2119 / BCP 14 meaning. Use them deliberately when
stating requirements.

## What you produce

A complete slice directory under `../AnsibleSpecs/slices/<NUMBER>_<snake_case_name>/` with the
following layout:

```
<NUMBER>_<snake_case_name>/
  overview.md                 — summary of what the slice delivers, why, dependencies
  acceptance_criteria.json    — testable conditions confirming the slice is done
  api_contract.json           — interface spec (usually "no changes" for infra; see Step 6)
  authoring_notes.md          — authoring decision log + open questions (kept as a record)
  grounding_check.md          — per-brief record of verified file:line citations
                                (always required when any brief is produced)
  <area>/brief.md             — scoped brief for that area's dev agent
  (one folder per area the slice actually touches)
```

**Areas** are where the slice's code change lands: `ansible/` (roles/playbooks/inventory),
`terraform/`, or a sibling repo (`helmcharts/`, `dockerimages/`, `iacagent/`) for a coordinated
cross-repo change. Only create the area folder for surfaces the slice actually touches — an
Ansible-only slice has only an `ansible/` folder. Orchestrator-owned files (everything listed
above the area folders) stay at the slice root; the `<area>/` subfolders are the dev agent's own
working directory and hold both the brief and the dev-agent artifacts (plan, change brief, code
reviews, etc.) produced during `/run-slice`.

Add the slice to the **Pending** section of `../AnsibleSpecs/slices/README.md` as a **single
line** matching the existing entries — `- **[NNN](NNN_<name>/overview.md)** — <short title>:
<one-clause summary>`. No status blob: the slice's `overview.md` is where the detail lives. The
slice index is a lean catalogue (mirroring the thin `decisions.md`), not a narrative.

## Procedure

### Step 1: Read the change-request bundle

Read every file in the bundle — `change_request.md` and any attachments. The bundle is your source
of truth: triage has already absorbed the findings, the issue-tracker cards, the Q&A, and any prior
design work into it. You need to understand:

- **What problem** is being solved or what capability is being added.
- **Which areas** are likely affected (`ansible/`, `terraform/`, a sibling repo, or a combination).
- **What the operator expects** to see when the slice is done.

**Capture every explicit request.** If the bundle (or the operator) says "I want X," X MUST become an acceptance criterion — not a suggestion, not a nice-to-have, not something softened into a different approach because it seems easier. If you think X is problematic or infeasible, say so and discuss it. Do not silently substitute a different approach.

Check the issue tracker (the `Ansible` cards the bundle references, now in the Triage board's **Accepted** list) for any context the bundle didn't capture.

### Step 1b: Reconcile with the bundle and challenge — return to the operator only on a delta

The bundle is the **authoritative statement of intent** — triage already clarified it with the operator and absorbed that Q&A. Do **not** re-interview the operator or re-derive what the bundle already settles; that just spends the same touchpoint twice. Instead:

1. **Take the bundle's understanding as your baseline.** Read it as authoritative and record your reading in `authoring_notes.md`. You do not need a confirm-back round for anything the bundle already answers.
2. **Challenge when the request cuts against an established pattern** — project or general (homelab doctrine, role/module conventions, networking, secrets, security; `CLAUDE.md`; `../AnsibleSpecs/decisions.md`). This is what deeper grounding newly surfaces, so it is the part worth raising. The sole purpose is to ensure you do not deviate from the request without the operator being aware. Likely outcomes:
   - The operator changes their mind → the request improves.
   - The operator overrules your objection → you MUST capture *why*, so the direction is explicit.
   - You learn context you were missing → it is critical to a correct end product.
3. **Return to the operator only on a genuine delta** — a pattern conflict, an ambiguity the bundle did not resolve, or something your grounding revealed that changes the shape. If the bundle is clear and grounding surfaces nothing to raise, proceed without a round; the operator still reviews everything at Step 9.
4. **Record the outcome in the slice** (the working document below, and the overview's Constraints/Decisions). Writing it down is what stops a later agent — which will share your instinct — from accidentally re-deviating.

### Step 1c: Keep an authoring working document (`authoring_notes.md`)

While you write the slice, maintain `authoring_notes.md` in the slice folder. It carries two logs and stays in the slice as a permanent record — distinct from `qa_log.md`, which `/run-slice` keeps for the dev-agent Q&A.

- **Decision Log — genuine A/B decisions only.** Log a point **only** when you weighed real alternatives and the choice could reasonably have gone the other way (e.g. *which collection/role to use*, *check-mode vs. a guarded command*, *whether to split the bundle*, *which host group to target*). Do **not** log requirements, restatements of the brief, natural or forced outcomes, or administrative bookkeeping — these are not decisions and they bury the ones that matter. Things that do **not** belong: "role stays idempotent" (a requirement); "no contract/inventory impact" (a natural outcome); "design doc placed at …" (administrative); "called out the edge cases" (a requirement). If an item has no plausible alternative, leave it out. Aim for a short log of real decisions, not a diary. Use this **exact format**, one entry per decision (headings and bullets, **no tables**):

  ```
  - <short description of the decision>
    - <the options / alternatives that were on the table>
    - <the choice> — <the reasoning, citing a decisions.md entry or a rule where one applies>
  ```

- **Open Questions.** Questions for the operator that the bundle did not settle. Use this format, with the answer slot written as `_Unanswered_` until the operator fills it in:

  ```
  - <the question>
    - _Unanswered_
  ```

  When a question is answered, replace `_Unanswered_` with the answer; if it was a real A/B, also fold the resolved choice into the Decision Log.

**Write the slice iteratively** when it is non-trivial: make some progress, log your decisions and questions, ask the operator to answer the open questions, then continue. Repeat until the slice is done. When the operator reviews the decisions and asks you to change one, treat it exactly as if they had answered an open question — go back into the loop and revise. That is a *good* outcome: without it the slice would likely have shipped something the operator didn't want.

### Step 2: Research the codebase

Before writing anything, understand the current state:

- Read the relevant homelab doctrine in `../AnsibleSpecs/decisions.md` and any related runbook under `docs/runbooks/`.
- Read the code areas that will be affected — roles (`tasks/`, `defaults/`, `templates/`, `handlers/`), playbooks, inventory (`host_vars/`, `group_vars/`), Terraform modules, or the sibling repo's charts/images.
- Check recent slices in the same area for patterns and context (`../AnsibleSpecs/slices/` and `slices/completed/`).
- Identify dependencies on other slices.

Do not write briefs based on assumptions about what the code looks like. Read it.

**Adjust research to fit the request.** A new role or Terraform module needs you to understand the existing roles, the inventory layout, and established conventions. A mechanical change like "normalize every collection version pin" does not — it needs a clear rule and broad scope. Match the depth of your research to what the operator actually asked for, and carry that through to the briefs: if the request is rule-based, the brief should state the rule and let the agent apply it, not enumerate every individual change (which agents misread as a closed set).

### Step 2b: Decide whether to split the bundle

One bundle usually becomes one slice. Dev agents do significant work in one sitting, and bundling keeps cycle time down — so the default is a single slice. Split the bundle into multiple slices only when there is a **clear** need (a genuine blocking dependency between parts, or work too large to stay coherent in one slice). When you split, record the split and its reason in `authoring_notes.md`. Prefer one slice; do not split for tidiness.

### Step 3: Assign a slice number

Slice numbers come from a **shared lock-guarded counter** so concurrent `/write-slice` sessions never
collide. Allocate with the helper script and use what it prints:

```bash
N=$(../AnsibleSpecs/scripts/allocate-next-slice.sh)   # prints e.g. 006
```

The script `flock`-serializes concurrent callers and persists the reservation **before** your slice
folder is created, so a parallel session sees the bump immediately. A burned number (allocate, then
abandon the slice) leaves a harmless gap — the accepted cost of collision-safety. The README slice
index is not the number oracle; you still add the slice to it (Step 10), but the counter decides the
number.

**Follow-up work** to an existing slice does **not** use the allocator — pick a letter suffix tied to
that slice (e.g. `004b`), since the number deliberately follows that slice rather than being freshly
sequenced.

### Step 4: Write the overview

The overview is for the orchestrator and reviewers. It explains **what** and **why** — not implementation details.

Structure:

1. **What this slice delivers** — 1–3 sentences describing the outcome.
2. **Why** — the problem being solved or capability being added.
3. **Requirements** — numbered list of concrete requirements (R1, R2, ...).
4. **Current state** — what exists today (if relevant).
5. **Dependencies** — which prior slices must be complete.
6. **Scope** — which areas/hosts are affected; explicitly note what's out of scope.

**Keep the overview at summary level.** The overview orients a reader — it is not where the working detail lives. The per-area **briefs carry the detail** (current-state `file:line` citations, task specifics, edge cases); the overview summarizes. Do not restate a brief's contents in the overview — state the outcome and the requirements at a glance and let the brief hold the rest. A reader should grasp the slice from the overview and reach for a brief when they need one area's specifics.

### Step 5: Write acceptance criteria

**This is the most important file in the slice.** The acceptance criteria are the contract between the operator and the implementation. Everything else — briefs, interface contracts, overviews — serves the criteria. If a requirement isn't in the acceptance criteria, it won't be verified, and if it's not verified, it may not be delivered.

Write `acceptance_criteria.json` with specific, testable conditions. Each criterion should be verifiable by code review, spec inspection, or the operator's `--check`/`apply` output (there is no automated test suite — see Step 7's verification requirements).

```json
{
  "criteria": [
    {
      "id": "ANS-01",
      "area": "ansible",
      "description": "One specific, testable outcome"
    }
  ]
}
```

**ID prefixes:** use area-specific prefixes for clarity — `ANS-` (Ansible), `TF-` (Terraform), `HELM-` (HelmCharts), `DOCK-` (DockerImages), `RE-` (regression).

`acceptance_criteria.json` carries the criteria definition only. Verdicts live in `verification.json` (created and maintained by `/run-slice`) — do not add a `status` field here.

**Good criteria:** "The `managed-vm` module derives the MAC from the VM id; re-running `terraform plan` reports no MAC drift for an unchanged VM."
**Bad criteria:** "MAC derivation works correctly."

**The completeness rule:** Go back through the operator's request, the issue-log cards, and the overview requirements. For every explicit ask, there must be a matching acceptance criterion. If you can't write a criterion that matches the request, that's a signal to discuss feasibility, not to quietly substitute.

### Step 6: Write the interface contract

`api_contract.json` records changes to an **interface other code or operators consume** — a role's input variables, a Terraform module's variables/outputs, an inventory group, an OpenBao path schema, a service endpoint a sibling repo calls. Most infra slices change no such contract; for those, use:

```json
{
  "changes": [],
  "notes": "No interface changes. <context>."
}
```

For slices that do change an interface, capture the shape — e.g. for a Terraform module or role:

```json
{
  "interfaces": [
    {
      "id": "IF-01",
      "kind": "terraform_module",
      "name": "managed-vm",
      "description": "What this interface provides",
      "inputs": ["vm_id", "node_name"],
      "outputs": ["mac_address"],
      "verified": null
    }
  ],
  "removals": []
}
```

Use the `kind` that fits (`terraform_module`, `role_vars`, `inventory_group`, `openbao_path`, `service_endpoint`, …). The point is that a consumer-facing shape is recorded and can be checked in `/run-slice`.

### Step 7: Write the briefs

Write one brief per agent that will work on the slice, placed at `../AnsibleSpecs/slices/<SLICE_DIR>/<area>/brief.md` (e.g., `ansible/brief.md`). Briefs are the most important part — they're what the dev agent reads to understand its task.

#### The cardinal rule: describe outcomes, not implementations

Briefs describe **what** needs to change and **why**. They do NOT prescribe **how**. The dev agent reads the code and writes the implementation; it knows the context the orchestrator doesn't.

**Good:** "The role must ensure the swap file exists and is enabled, and must report no change on a second run."
**Bad:** "Add a `community.general.filesystem` task, then an `ansible.posix.mount` task with `state: present`, then a `command: swapon` with `creates:`."

**Good:** "The pre-drain check must treat a node as Ready only when every node condition the drain depends on is satisfied."
**Bad:** "Modify `roles/k8s-drain/tasks/main.yml:42` to add `when: node.status.conditions ...`."

#### Forbidden patterns

If a draft line matches any of these, rewrite it — don't soften, don't caveat.

1. **Code or pseudocode**, even one-liners or "shape" hints. No task YAML, no `when:` expressions, no HCL fragments, no jinja templates.

2. **Algorithm or step lists.** "First do the filesystem task, then mount, then enable" is procedure; describe the outcome and let the agent derive it. This includes task decompositions like "Task 1: add the var. Task 2: template the file. Task 3: add the handler" — that's an algorithm wearing a task list. A task is a unit of outcome, not an implementation step.

3. **Named symbols to create.** Don't name the variables, handlers, task files, roles, modules, or templates the agent should produce.
   - **Bad:** "Add a handler `restart microk8s` and a defaults var `swap_size_mb`."
   - **Good:** "The role must let the swap size be configured, and must restart the affected service when its config changes."

4. **Target-state `file:line` citations.** Citations describe what the code is today, never what it should become. "Today, the drain check lives in `roles/k8s-drain/tasks/main.yml:42`" is fine; "Modify `roles/k8s-drain/tasks/main.yml:42` to do Z" is not — the agent picks the location.

5. **Exact template / config strings.** Prescribing the literal lines to write into a `.j2` template or a config file is still prescription. Describe the required end state in prose and point at a precedent file to match for style.

6. **Forbiddances without a stated requirement.** If you have to forbid a path, you've imagined the implementation. State the positive requirement instead.
   - **Bad:** "Do not use `shell:` for this."
   - **Good:** "The task must be idempotent and report no change on a re-run." (The agent figures out the module.)

Precedent references are the one allowed form of pointing at code: "follow the pattern in `roles/<other-role>`" — no line numbers, no symbol names.

#### Final pass: classify every line

Before freezing, re-read the brief. Every non-trivial line is one of:

- **(a)** Fact about current state with a `file:line` citation — keep.
- **(b)** Outcome, requirement, constraint, or behavioral rule about target behavior — keep.
- **(c)** Prescription about how to get from (a) to (b) — move it: into `acceptance_criteria.json` if it's a requirement in disguise, into the overview's Constraints section if the operator explicitly demanded the implementation choice, otherwise delete it.

#### Length ceilings

Past the ceiling, the brief is doing a plan's job and the work belongs in the major workflow:

- **Routine maintenance** (rule-based, version-pin bumps, sweeps): ≤ 400 words.
- **Pattern-following / bug fix with reproduction**: ≤ 600 words.
- **Any minor brief**: ≤ 1,000 words hard ceiling.
- **Major-workflow briefs**: no ceiling — they go through plan-writer + plan-reviewer.

#### Rule-based briefs (routine maintenance)

When the request is a rule applied broadly (collection/provider version bumps, bulk renames, config normalization, lint sweeps, dead-code removal, doc fixes), the brief should describe the **rule** and its scope, not enumerate every individual change. Include:

1. The rule (e.g., "pin every Ansible collection to its latest published version").
2. How to determine inputs (e.g., "run `ansible-galaxy collection list` and check Galaxy for the latest").
3. A few illustrative examples.
4. Explicit scope — "every collection in `requirements.yml`" vs. "only these specific ones."

Exhaustive tables get misread as a closed set.

**Routine briefs go to the minor workflow regardless of file count.** Each touch is mechanical and the dev coordinator does not need a written plan. Note this in the overview's Scope section ("Routine maintenance — minor workflow expected") so `/run-slice`'s brief-shape check exempts it from the plan-shaped-brief warning.

If a routine brief grows past 400 words, the work is probably no longer routine — design decisions are hidden inside the rule. Surface them to the operator before freezing.

#### Brief structure

Each brief should include:

1. **Context** — 1–2 sentences on what the agent is building (point to the overview for background).
2. **Tasks** — numbered, scoped units of work. Each task describes:
   - What needs to change (a new role, a module variable, a playbook wiring, a chart value).
   - Why it needs to change (the problem or requirement it addresses).
   - Constraints and edge cases (idempotency, check-mode safety, which hosts, secret handling, failure modes).
   - Which acceptance criteria it covers (reference the IDs).
3. **Verification requirements** — what proves the change is correct: which lint runs clean (`poetry run yamllint` + `poetry run ansible-lint`, or `terraform fmt -check` + `terraform validate`), what a `--check`/`plan` diff should show, that a second run reports no change (idempotence), and the operator `apply`/`--check` command that confirms it on real infra. **The dev agent never applies against real infra** — it lints and reasons about check-mode; the operator runs the apply (see `CLAUDE.md`).
4. **Code quality** — pointer to the root `CLAUDE.md` Tooling section for lint commands (or, for a sibling-repo area, that repo's own `CLAUDE.md`).

#### Allowed content

The forbidden patterns say what to leave out. Positively, briefs carry:

- **Interface details** — variable names, types, defaults (required/optional), module inputs/outputs, inventory group names. Facts about the contract, not implementation.
- **Behavioral rules** — "if X, the role must Y." Logic as requirements.
- **Failure conditions** — what can go wrong (unreachable host, missing secret, first-boot vs. re-run), and the required behavior.
- **Constraints** — "must be idempotent," "must be check-mode safe," "must target only the `k8s_prd` group," "must read the secret from OpenBao, never inline it."
- **Precedent references** — "follow the pattern in `roles/<role>`." Point at the file; no line numbers, no symbol names.
- **Acceptance criterion IDs** — every task references the criteria it satisfies.

#### External dependency updates — verify the bump landed

If the slice depends on a new version of an external dependency (an Ansible collection or role, a Terraform provider pin, a base-image tag), the brief must require the dev agent to verify the pin/lock is on the new version before relying on the new behavior.

#### Doc-first slices — require a checkpoint between Task 1 and Task 2

When a slice is structured as "Task 1: write a contract/design document; Task 2: implement the change whose direction depends on Task 1's contract" (e.g. a design doc that determines which repo owns a follow-up fix), the brief must explicitly require the agent to stop after Task 1, commit the doc, and wait for operator review before starting Task 2.

**Why:** The doc itself is the decision the operator wants to vet before code lands.

**How to apply:**
- In the brief's Task 1, include a terminal instruction: *"After committing Task 1, stop and wait for the orchestrator to resume you. Do not start Task 2."*
- In Task 2, note that the task is gated on operator review of Task 1.
- Flag the checkpoint in the overview so `/run-slice` knows to pause and hand off to the operator between tasks.

### Step 7b: Grounding pass

**Mandatory — do not skip, do not soften to "consider".** Before any brief in this slice is considered frozen, you must re-ground every codebase claim it contains against the current code. Briefs written from your short-term mental model rather than from a fresh read of the files are the leading cause of Round 1 Q&A corrections. This step exists to catch those misses before the brief is handed to a dev agent.

For every brief produced in this slice (one `<area>/brief.md` per area the slice touches), you must:

- **(a) Open every `file:line` citation** in the brief and confirm the cited code matches the claim the brief is making about it. A stale line number, a renamed role/var, a moved task block — any mismatch gets corrected in the brief before the brief is frozen.
- **(b) Re-grep or re-read the code behind every "the system does X today" / "the current behavior is Y" / "there is no Z today" assertion.** Do not assert current behavior from memory. If the claim is "role R does not exist yet," grep for it; if it is "the playbook drains the node before reboot today," open the play.
- **(c) Check every "add Y" / "introduce Y" / "create Y" task against the current codebase** to confirm Y is not already present. Partial implementations count — if a half-built version of Y exists, record what is present so the brief directs the agent to complete rather than duplicate.

You must write a sibling grounding self-check artifact at `../AnsibleSpecs/slices/<SLICE_DIR>/grounding_check.md`. This file is a dedicated artifact, not inlined into each brief. Its minimum contents:

- One section per brief (`## <area>/brief.md` for each area the slice touches).
- Under each section, a bulleted list of every claim you checked, each with a `file_path:line_number` citation where relevant and a verdict of **confirmed**, **corrected** (with a short note on what was changed in the brief), or **not applicable** (with a reason).
- A final "Summary" bullet per section stating "all file:line citations verified" — or, if any corrections were applied, listing them.

A brief without a matching `grounding_check.md` section is not frozen.

### Step 8: Consider architecture design

Most slices follow an existing pattern and do not need a separate `/arch-design` run. The dev agent's own planning phase during `/run-slice` surfaces the same implementation subtleties an upfront arch-design would — running both is redundant.

**Reserve `/arch-design` for slices where:**
- The decision spans multiple areas/repos and affects how they coordinate.
- The decision changes the slice structure (splitting into sub-slices, introducing blocking dependencies).
- There are genuinely competing approaches and the operator needs to choose before implementation starts.
- A new cross-cutting pattern is being introduced that future slices will follow.

For "follow the existing pattern" slices, the brief plus the dev agent's own planning is sufficient. Do not default to running arch-design as a safety net.

### Step 9: Present to the operator

Show the operator a summary of what you've written:

- Which agents will run (which areas).
- Key requirements and acceptance criteria.
- The **Decision Log and any Open Questions** in `authoring_notes.md` — present the working document alongside the slice so the operator can review the decisions and push back.
- Any design decisions or trade-offs you made, and any ambiguities still open.

Wait for the operator to review and approve. If they challenge a decision or answer an open question, go back into the authoring loop (Step 1c) and revise before considering the slice complete.

### Step 10: Absorb the bundle and update the tracker

Once the slice is approved:

1. **Absorb all source material into the slice.** Everything in the bundle — the `change_request.md` content, the Q&A, the referenced issue-tracker items, and any attachment — MUST be reflected in the slice (overview, acceptance criteria, briefs, `authoring_notes.md`). Only a *substantial* attachment worth keeping verbatim (a long prior design doc, e.g. an arch-design) lives **inside the slice directory** and is linked from the overview; everything else is absorbed in place. **Slice-owned documents — including any design doc — stay with the slice; never park them in `handovers/`** (that folder is for transient cross-session handoffs). For a design that spans a multi-slice program, keep it in the **keystone slice's** directory and reference it from the program's other slices.
2. **Delete the bundle.** When the slice is complete and self-contained, delete the `change_requests/<slug>/` folder — the operator should be able to delete it with nothing lost. The issue-tracker items are **kept** (they track the work; their content lives in the slice now).
3. **Replace the source cards with a slice card.** The source cards (`Ansible`-tagged, in the Triage board's **Accepted** list) have no standalone value now that the slice exists. Create **one new card on the Kanban board** (https://trello.com/b/QNGUAXri/kanban) in the **To Do** list that represents the slice — title = `[NNN] <slice title>` (the slice number in brackets); the `Ansible` owner tag and no other labels; a short description that gives the highlights (not a restatement of the slice), points to the slice folder, and **lists the source-card ids it subsumes** so the thread from raw idea to slice survives the archive. Then **archive the source cards** that fed this slice. If you split the bundle into multiple slices, create one Kanban card per slice — each listing the source ids it subsumes — and archive the source cards across them. From here that single slice card is what flows **To Do → In Progress → Done**.

   **Owner tag = who leads/runs the slice, not where the code lands** (per `CLAUDE.md`). A coordinated cross-repo change, or one whose context lives here in Ansible(Specs), is Ansible-led — tag it `Ansible`.

### Step 11: Lodge any doctrine change

If the slice establishes or changes **homelab doctrine** (tool split, secrets, networking, MAC scheme, OS update policy, or another standing convention), update `../AnsibleSpecs/decisions.md` — the repo's existing decision record — so the doctrine and the slice agree. You already recorded the reasoning in `authoring_notes.md` and the overview; this gives the standing decision its durable home. State it as the design, not as a dated log entry, and match the voice of the surrounding decisions.

Most slices change no doctrine — they apply it. Only touch `decisions.md` when the doctrine itself moves. Leave implementation-detail documentation (what the finished role/module does) to `/run-slice`'s close-out or a later `/update-docs` sweep.

Commit the doctrine change with the rest of the slice's specs artifacts.

## Your role

You are a **work coordinator and validator**, not a technical architect. Your value is in:

1. **Faithfully capturing requirements** — every operator request becomes a tracked criterion.
2. **Ensuring completeness** — nothing falls through the cracks between overview, criteria, and briefs.
3. **Pushing back** — raising feasibility concerns before work starts, not silently substituting.
4. **Validating delivery** — verifying at the end that what was asked for is what was built.

You are NOT responsible for designing the implementation. The dev agents read the code, write plans, and make technical decisions.

## Quality checklist

Before presenting the slice to the operator, verify:

- [ ] Overview explains *what* and *why*, not *how*.
- [ ] **Every explicit operator request** has a matching acceptance criterion.
- [ ] **Every issue-log card** scoped into this slice has matching acceptance criteria.
- [ ] No request was silently substituted with a different approach.
- [ ] Every acceptance criterion is specific and testable (by review, inspection, or operator `--check`/`apply` output).
- [ ] Briefs contain zero code/pseudocode (task YAML, HCL, jinja, `when:` expressions).
- [ ] Briefs describe outcomes and constraints, not implementation steps.
- [ ] Interface contract lists all new/changed/removed consumer-facing interfaces (or states "No interface changes").
- [ ] Failure conditions and edge cases are documented as requirements.
- [ ] Idempotency and check-mode safety are stated where they apply.
- [ ] Dependencies on other slices are listed.
- [ ] Scope is clear — "out of scope" and which hosts/areas are stated where relevant.
- [ ] Each brief references which acceptance criteria IDs it covers.
- [ ] Briefs live under `<SLICE_DIR>/<area>/brief.md`, not at the slice root.
- [ ] Grounding pass has been run and `grounding_check.md` exists with every `file:line` citation verified.
- [ ] Overview is summary-level — it does not restate brief detail.
- [ ] `authoring_notes.md` exists with the Decision Log (options + grounds) and any Open Questions.
- [ ] The change-request bundle's material is fully absorbed, and the bundle is deleted once the slice is complete.
- [ ] Source cards are archived and replaced by a single Kanban **To Do** card per slice (title prefixed `[NNN]`; `Ansible` tag; a short highlights summary; a pointer to the slice; the subsumed source-card ids).
- [ ] Slice is added to the **Pending** section of `../AnsibleSpecs/slices/README.md` as a single one-line entry.
- [ ] If the slice changes homelab doctrine, `../AnsibleSpecs/decisions.md` is updated to match.
