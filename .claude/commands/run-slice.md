# Run Slice

Run the implementation workflow for a slice. Argument: the slice number (e.g., `001`).

## What this skill does

You are the orchestrator. You drive the slice's code change by dispatching the dev agents **inline
as Task subagents** from this same session — there is no separate session manager. For each area the
slice touches (`ansible/`, `terraform/`, or a sibling repo), you choreograph the change workflow
yourself: plan and review for a major change, then implement and review; implement and review
directly for a minor one. You answer the agents' questions, maintain the verification log, and at the
end hand the operator the exact `terraform`/`ansible` command that verifies the change on real infra.

**The operator runs all `terraform apply`/`destroy` and `ansible-playbook` against real
infrastructure — you and the dev agents never apply** (see `CLAUDE.md`). The dev agents write and
lint code; the *live* verification at the end is the operator running the command and reporting back.
There is no automated test suite here — lint, idempotency reasoning, and the operator's
`--check`/`apply` output are the verification.

**Push notifications:** Use `python3 tools/ai_workflow/send_message.py --title "Slice <NUMBER>" "<message>"` to notify the operator. Send a notification when:
- The slice's code change is implemented and reviewed and is ready for the operator to apply.
- The slice is blocked and needs operator attention (agent failure, an interface gap, a decision you can't make).
- The slice is fully complete (after the operator confirms a clean apply).

Do **not** notify for routine progress.

**Normative keywords.** MUST / MUST NOT / SHOULD / SHOULD NOT / MAY in a slice's acceptance
criteria, briefs, and overview carry their RFC 2119 / BCP 14 meaning — read them as binding when
answering agent questions and seeding the verification log.

## Slice file formats

Slices are authored by `/write-slice`. The layout:

```
slices/<SLICE_DIR>/
  overview.md, acceptance_criteria.json, api_contract.json,
  grounding_check.md, verification.json   ← orchestrator-owned
  <area>/brief.md                         ← dev-agent-owned folders
  (one folder per area that has work in this slice)
```

The files the runner reads:

- **`acceptance_criteria.json`** — testable conditions with `id` (prefixed by area — `ANS-`/`TF-`/`HELM-`/`DOCK-`/`RE-`), `area`, and `description`. The criteria definition is immutable here; verdicts live exclusively in `verification.json`.
- **`api_contract.json`** — interface spec (`interfaces` with inputs/outputs and a `verified` flag, `removals`); usually "no changes" for infra.
- **`<area>/brief.md`** — scoped task descriptions for each dev agent. Determine which agents need to run based on which of these files exist.
- **`overview.md`** — what the slice delivers, dependencies, scope, any doc-first checkpoint.
- **`grounding_check.md`** — per-brief record of verified `file:line` citations from `/write-slice`'s grounding pass.

When prompting agents, reference the relevant acceptance criteria and interface IDs so the agent knows exactly which conditions its work must satisfy.

## Procedure

### Step 0: Identify the slice and pre-flight the working tree

Resolve the argument to the slice directory under `../AnsibleSpecs/slices/`. For example, `001`
resolves to `../AnsibleSpecs/slices/001_<name>/`.

Read all documents in the slice directory. Determine which agents need to run based on which
`<area>/brief.md` files exist.

**Pre-flight: clean tree + lint baseline.** Before starting any agent, confirm each area the slice
touches is in a clean, lintable state — agents inherit whatever you hand them, and uncommitted
changes from an aborted prior run would pollute the slice's commit range.

- `git status` is clean in each area's repo (`/work/Ansible`, and any sibling repo the slice touches). Uncommitted changes → stop and resolve with the operator.
- Lint passes on the current tree for the touched area:
  - Ansible/YAML: `cd ansible && poetry run yamllint <paths> && poetry run ansible-lint <paths>`
  - Terraform: `cd terraform/<env> && terraform fmt -check && terraform validate`
  - A sibling repo: that repo's own lint (see its `CLAUDE.md`).

**If the baseline lint is already failing**, do not start an agent on top of it — fix the root cause or flag it to the operator first. A dirty or red baseline makes the slice's own changes impossible to attribute.

### Step 0b: Pre-flight review with the operator

After reading all slice documents and passing the baseline checks, present a pre-flight summary before starting any agent work.

1. **Work rundown.** Summarize which agents will run based on which briefs exist, with a brief description of what each will deliver (1–3 sentences per agent). Reference the slice's card on the Kanban board (in **To Do**) — `/write-slice` created it, titled `[NNN]`.
2. **High-impact decisions.** Flag decisions with significant blast radius (a new persistent host/VM, a destructive Terraform change, a service restart/drain/reboot across a host group, a new OpenBao path, a cross-repo change). Skip this section if the slice is low-impact.
3. **Clarifications.** If anything is ambiguous or could go multiple ways, ask the operator now — before any agent starts.
4. **Notify and wait.** Send a push notification and wait for the operator to respond before proceeding. Do not start Step 0c until the operator confirms (e.g., "go", "proceed").

### Step 0c: Seed the verification log

Once the operator confirms the pre-flight, move the slice's Kanban card **To Do → In Progress**, then create `../AnsibleSpecs/slices/<SLICE_DIR>/verification.json` and seed it from `acceptance_criteria.json`. The verification log is the single source of truth for what the `slice-verifier` checks at Step 4 — items only get verified if they're in the log.

Schema (one entry per item):

```json
{
  "items": [
    {
      "id": "V01",
      "source": "ac",
      "area": "ansible",
      "description": "ANS-1: <verbatim AC description>",
      "verdict": null,
      "rationale": "",
      "evidence": []
    }
  ]
}
```

- `id` — sequential `V01`, `V02`, … in entry order.
- `source` — `ac` (seeded from acceptance criteria) or `qa_correction` (added in Step 1+ when you override an agent's stated direction).
- `area` — the area a failure routes back to. For AC entries, copy the AC's `area`.
- `description` — what must be true in the implementation. For AC entries, prefix with the AC id (e.g., `ANS-1: …`). State the *what*, not the *why*.
- `verdict`, `rationale`, `evidence` — left empty; the verifier fills these in.

Seed one item per AC, in order. Commit `verification.json` to the specs repo before starting Step 1.

### Step 1+: Run each area's dev agent (inline)

For each area with a `<area>/brief.md`, choreograph the change workflow yourself by dispatching the dev agents as Task subagents. If one area defines an interface another consumes (a Terraform module a sibling repo wires up; a role another playbook calls), run that **leading** area first and let its interface settle before the consumer. Most slices have a single area; then order doesn't matter.

A dev agent dispatched as a Task subagent runs in the Ansible session's context but works on files in the area's repo. For a sibling-repo area, its working scope is that repo (`/work/HelmCharts`, `/work/DockerImages`, `/work/IaCAgent`) and it commits there per that repo's commit-as-you-go rule.

#### 1a. Pick the workflow for this area

Based on the brief plus your read of the change:

- **Minor** — pattern-following work with existing precedent, no new design decisions, narrow diff (≤ ~200 lines / ≤ ~5 files), executable without a written plan. Examples: a verbatim mirror of a sibling change, a bug fix with a clear reproduction, a config/value tweak, adding a role variable that follows an established pattern, routine version-pin bumps. → **code-writer → code-reviewer.**
- **Major** — anything that introduces new patterns, crosses module/role boundaries, or involves design decisions worth capturing in a written plan. Default to major when in doubt. → **plan-writer → plan-reviewer → code-writer → code-reviewer.**

Asymmetry across areas is expected — e.g. a Terraform module change major, a HelmCharts values mirror minor.

#### 1b. Question round (both workflows)

Dispatch the first agent (plan-writer for major, code-writer for minor) and instruct it, in the prompt, to **read the brief and the code it cites and return informed questions ONLY — do not implement yet.** Inline Task subagents can't call `AskUserQuestion`; tell the agent to put every question in its reply.

Answer the questions yourself from your knowledge of the project and the code. Then re-dispatch the agent (it reads its prior artifact and the brief from the slice folder) with your answers plus the instruction to proceed.

When answering:

- **Don't prescribe implementation details.** Describe **what** and **why**, not **how** — no task YAML, no HCL, no jinja. The agent reads the code and designs the implementation.
- **Pick one value when the agent surfaces a tunable.** A timer, a retry count, a swap size, a host-group target — give a single value, not a range. If the agent disagrees it must argue back. If you overrode a value the agent proposed, log it as a `qa_correction`; if the agent simply asked, your answer is binding and needs no log entry.
- **Ground every claim about the codebase in a verified `file:line` citation.** When an answer depends on how the code behaves today — which hosts a play targets, what a handler notifies, whether a task is already idempotent — read the file or grep before committing the answer, and cite `file_path:line_number`. Do not assert behavior from memory.
- **Trace agent-narrated boundaries against the brief, not the agent's framing.** When the agent narrates a boundary — "this only runs on first boot," "the drain already covers that," "check-mode is a no-op here" — walk what the operator actually observes on the new path under that boundary and compare to what the brief requires. Plausible framing is not requirement satisfaction. If the stated boundary leaves a relevant AC dangling, that is a `qa_correction`.

**Log the Q&A exchange** to `../AnsibleSpecs/slices/<SLICE_DIR>/qa_log.md`:

```markdown
## <Area> — Round N

Q: <agent's question>
A: <your answer>
```

Pair each question with its answer directly. Create the file on the first write.

**Log corrections to the verification log.** When your answer overrides the agent's stated direction — the agent proposed A and you said no, do B — append an entry to `verification.json` with `source: qa_correction`, the next sequential `V##` id, and the area of the agent. The `description` states what must be true in the implementation. The bar is *direction change*: clarifications, picking a tunable the agent simply asked about, and "yes that's right" confirmations do **not** go in the log.

**Log deferred items.** If any exchange surfaces work out of scope for this slice but needing future attention, create a card in the Triage **Inbox** (tagged `Ansible`) immediately.

**Decide whether to allow follow-up questions.** If the questions show good understanding, go straight to execution. If they reveal significant gaps, allow one more question round before letting the agent proceed.

#### 1c. Major path: plan, then review

For a major change, after the question round the **plan-writer** produces `plan.md` (plus `requirements.json`, `file_map.json`, `verification_plan.json`) under `<area>/`. Then dispatch the **plan-reviewer** over that plan. Read its decision block:

- `NO-GO` / `GO-WITH-CONDITIONS` → re-dispatch the plan-writer to resolve the blockers/majors (it revises `plan.md`), then re-review. Repeat until `GO`.
- `GO` → proceed to implementation.

#### 1d. Implement, then review (both workflows)

Dispatch the **code-writer** with the path to the plan (major) or the brief (minor). Its working directory is the area's repo; it implements, runs the area's lint clean, and commits its work **including the dev-agent artifacts** under the area slice folder. Remind it in the prompt: *"Store all your artifacts (change brief, plan files, code reviews) under `../AnsibleSpecs/slices/<SLICE_DIR>/<area>/` — that subfolder is yours. Do not create, edit, or delete files at the slice root or in a sibling area's folder. Commit ALL your work, and run `git status` before your final commit so nothing is left uncommitted. Do NOT run `terraform apply`/`destroy` or `ansible-playbook` against real infra — that is the operator's; lint and reason about check-mode only."*

Then dispatch the **code-reviewer** over the change (the unstaged diff, or — if already committed — the commit hashes) against the brief + acceptance criteria; it writes `code_review.md`. Read its decision block:

- `NO-GO` / `GO-WITH-CONDITIONS` → re-dispatch the code-writer to resolve every finding, then re-review. Repeat until `GO`.
- `GO` → the area is implemented.

**Confirm the workflow artifacts landed** before accepting the area's work: `change_brief.md` for a minor change; `plan.md` + `plan_review.md` + `code_review.md` for a major one. The adversarial review is the whole point — absent artifacts mean a gate was skipped; remediate by dispatching the missing agent over the committed diff before moving on.

### Step 2: Review the interface contract (if applicable)

If `api_contract.json` lists interfaces, read the implemented role variables / module inputs-outputs / inventory groups / OpenBao path / endpoint and compare. For each entry:

1. Verify the interface exists with the named inputs/outputs.
2. Confirm consumers (other roles, the sibling repo, the inventory) reference it as recorded.
3. Update the `verified` field to `true` or `false`.

For each `removals` entry, confirm the named interface is gone. If any entry is `verified: false` and it's a significant gap (missing interface, wrong shape), notify the operator and stop. Minor differences (naming) are fine. Write the updated `api_contract.json` back.

**Log any gaps or workarounds** as cards in the Triage **Inbox** (tagged `Ansible`).

### Step 3: Lint gate over the slice's changes

After all areas are implemented, run the full lint over the slice's changed files to confirm everything is green together:

- Ansible/YAML: `cd ansible && poetry run yamllint <changed-paths> && poetry run ansible-lint <changed-paths>`
- Terraform: `cd terraform/<env> && terraform fmt -check && terraform validate`
- Sibling repo: its own lint.

Any failure routes back to the owning area's code-writer with the output; re-run lint after the fix. **Maximum 3 fix rounds per area** — if an area can't get lint green, notify the operator and stop.

### Step 4: Independent verification (code level)

Verification runs in fresh context via the `slice-verifier` sub-agent walking the verification log against the committed code.

1. **Determine the slice's commit range** — the unpushed commits on the current branch, or the commits added since this slice started. Capture as a hash range or list. (Include the sibling repo's range if the slice touched one.)
2. **Dispatch the `slice-verifier` sub-agent** with paths only:

   ```
   Slice directory: ../AnsibleSpecs/slices/<SLICE_DIR>/
   Commit range: <hash>..HEAD  (or specific hashes)
   ```

   **Do not** include framing — no opinions about quality, no hints about which entries you expect to pass.
3. **Read the updated `verification.json`.** The verifier has filled in `verdict`, `rationale`, and `evidence` per entry.
4. **Route the result:**
   - Any entry `failed` or `uncertain` → back to the owning area (use the entry's `area`) with the verifier's evidence. Do not re-derive the verdict yourself.
   - A rubber-stamp rationale → send back to the verifier for a sharper reading.
   - All `passed` → proceed to Step 5.

The verifier checks what is provable from the committed code (a role does X, a module input is wired, a template renders the right shape). **Criteria that can only be confirmed by a live run — idempotence, a service actually coming up, a successful apply — are confirmed at Step 5 by the operator's output**, not by the code reviewer or verifier. Where a `verification.json` entry is inherently runtime-only, the verifier marks it `uncertain` and notes "needs operator apply"; you close it in Step 5 from the operator's reported output.

### Step 5: Operator verification on real infra

The slice's code is implemented, lint-green, and code-verified — now it must be proven against real infrastructure, which only the operator runs. Hand over the exact command(s), check-mode first (per `CLAUDE.md`'s "Check-mode first" and "Canonical command shape"):

- **Ansible:** one line, repo-relative, with `--check` appended at the **very end** so the operator converts it to an apply by deleting the trailing flag:
  `cd ansible && poetry run ansible-playbook playbooks/<play>.yml --limit <host> --check`
  Pass `-i inventories/scratch` only for scratch-fleet runs. Never pass `--diff` (`ansible.cfg` sets `diff_always`) or `--ask-vault-pass` (the operator's shell has `ANSIBLE_VAULT_PASSWORD_FILE`).
- **Terraform:** `cd terraform/<env> && terraform apply` — `apply` already shows the plan and waits for confirmation; don't propose a separate `plan`.
- **Idempotence:** for an Ansible change, ask the operator to run the apply a **second** time and confirm `changed=0` (the homelab's re-runnability rule).

Send a push notification that the slice is ready to apply, then **wait for the operator to run it and paste back the full output** — not "looks good." Parse it:

- **Clean apply + idempotent re-run + the live behavior the criteria require** → close the runtime-only `verification.json` entries from this output (cite the relevant lines as evidence) and proceed to Step 6.
- **Apply error, drift on re-run, or wrong live behavior** → route back to the owning area's code-writer with the operator's output and your diagnosis (e.g. "not idempotent — second run still reports the swap task changed"), fix, re-lint, and hand the operator the command again. Repeat until clean.

This operator-apply gate is the slice's real test. Do not move the slice to completed until the operator has confirmed a clean, idempotent apply.

### Step 6: Review the QA log for issue-log items

Review `../AnsibleSpecs/slices/<SLICE_DIR>/qa_log.md` end-to-end. Look for deferred work, known limitations, contract/spec drift, and design decisions with future implications. For each, create a card in the Triage **Inbox** (tagged `Ansible`). Don't duplicate items already logged inline during Q&A.

### Step 7: Reconcile homelab doctrine (if the slice changed it)

If the slice changed homelab doctrine (tool split, secrets, networking, MAC scheme, OS update policy, or another standing convention), confirm `../AnsibleSpecs/decisions.md` reflects what was actually built — the slice author lodged the decision at authoring time; this step confirms reality matches, fixing the entry where implementation diverged from intent (watch the `qa_log.md` drift items from Step 6). Most slices change no doctrine and skip this step. A broader documentation sweep is `/update-docs`'s job. Commit any doctrine change with the rest of the slice's specs artifacts.

### Step 8: Report and close out

Summarize what happened:
- Which agents ran (which areas) and whether they succeeded.
- The interface-contract review result (how many verified/failed), if applicable.
- Lint result and any fix rounds.
- Acceptance-criteria results — count `source: ac` entries in `verification.json` by `verdict`; all should be `passed`.
- The operator-apply result (clean / idempotent).
- Any items blocked on identified gaps (link to Triage cards).

Move the slice to completed (only when it is fully complete — operator-apply confirmed):

- **In `../AnsibleSpecs/slices/README.md`**, move the slice's entry from **Pending** to **Completed**, kept in slice order. It stays the same single line. Do **not** copy the run report into the README — that detail lives in `overview.md` and git history.
- **On disk**, `git mv ../AnsibleSpecs/slices/<SLICE_DIR> ../AnsibleSpecs/slices/completed/<SLICE_DIR>` so the active `slices/` view shows only in-flight work.
- **On the Kanban board**, move the slice's card **In Progress → Done**.

Commit the move with the rest of the slice's specs artifacts. (A slice the operator defers or cancels goes to `slices/deferred/` or `slices/cancelled/` instead.)

Notify the operator that the slice is complete (or partially complete if items are outstanding).

## Important notes

- **The operator applies — you never do.** Every `terraform apply`/`destroy` and `ansible-playbook` against real infra is the operator's keystroke. You and the dev agents prepare and lint the change and hand over the exact command. Read-only state inspection (`qm config`, `lsblk`, file reads via the SSH keys in `/work/Obsidian/Attachments/`) is fine for diagnosis.
- **Lint is green before every slice.** If lint fails after a slice run, the slice's changes caused it. Don't dismiss failures as pre-existing — the baseline was green at Step 0.
- **No backwards compatibility.** When answering agent questions, prefer clean breaking changes over compat shims, fallback branches, or silent defaults.
- **Answer questions yourself.** You have full access to the project docs and code. Don't punt the agent's questions to the operator.
- **No code in briefs or answers.** Describe *what* and *why*, not *how*.
- **Run areas sequentially**, not in parallel — an area that defines an interface must settle before its consumer.
- **Agents must use the change workflow.** Gate every first dispatch to questions-only, forbid `AskUserQuestion`, and confirm the workflow artifacts (`change_brief.md` / `plan.md` + reviews) landed before accepting the work. A missing artifact means a gate was bypassed — remediate by dispatching the missing agent over the committed diff. If an agent genuinely can't make progress within the workflow, the slice is too large — report to the operator to discuss splitting.

## Issue log

Whenever you encounter something that needs future attention — an interface gap, a deferred change, a workaround, a known limitation — log it as a card on the Triage **Inbox** (tagged `Ansible`). See `CLAUDE.md` for the two-board model.

**Card lifecycle during a slice run:**
- When a slice **starts**, find its Kanban card (in **To Do**) and move it to **In Progress** (Step 0c).
- When the slice is **implemented, verified, and the operator has confirmed a clean apply**, move its Kanban card **In Progress → Done** (Step 8).
- New issues **discovered** during the slice go to the Triage **Inbox** (tagged `Ansible`) — fresh intake for a future triage, not part of this slice's card.
