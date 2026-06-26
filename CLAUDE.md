# CLAUDE.md

Context for Claude Code working in this repo.

## What this repo is

Ansible + Terraform managing the homelab infrastructure: Proxmox hosts, k8s VMs and cluster, Ceph VMs and cluster, and the Linux dev box baseline. Helm owns Kubernetes workloads (in `/work/HelmCharts`); Jenkins runs deploys.

**Before proposing changes, read these in order:**
1. [`/work/AnsibleSpecs/decisions.md`](../AnsibleSpecs/decisions.md) — homelab doctrine: tool split, secrets, networking, MAC scheme, OS update policy.
2. [`/work/AnsibleSpecs/slices/README.md`](../AnsibleSpecs/slices/README.md) — the slice catalogue: numbered `NNN_` slices, closed work in `completed/`/`deferred/`/`cancelled/`. **Live status is on the shared Kanban board** (see the AI-workflow section).
3. The relevant slice doc(s) under `/work/AnsibleSpecs/slices/` — working context for the conversation.

All work is tracked as **slices**, numbered `NNN_<name>/` under `/work/AnsibleSpecs/slices/`. The phased build-out is complete and retired; its history is archived (read-only) under [`/work/AnsibleSpecs/phases/`](../AnsibleSpecs/phases/) and stays linked from slices for context. Operational runbooks stay in [`docs/runbooks/`](docs/runbooks/).

When a slice is added, moved (pending ↔ completed / deferred / cancelled), or its dependencies shift, update [`/work/AnsibleSpecs/slices/README.md`](../AnsibleSpecs/slices/README.md) in the same change. The index is the entry point — a stale row there sends future readers (including future-Claude) chasing work that no longer exists or missing work that does.

If a decision changes, update `/work/AnsibleSpecs/decisions.md` — don't leave stale notes elsewhere.

## The AI workflow: your role, slices, and the boards

This repo runs the shared [AIWorkflow](/work/AIWorkflow) slice model, but with one repo-specific twist that overrides the stock orchestrator posture: **here, you do the infrastructure work yourself.**

### Your dual role

In the app repos, the orchestrator only coordinates — it refuses to touch code directly and routes everything through the slice workflow. **This repo is different.** Most of the work here *is* orchestrator work: investigation, diagnostics, troubleshooting, reading live state, editing a runbook, fixing a role, preparing an operator command. Do that work directly and frictionlessly — you are a helpful infrastructure agent, not a gatekeeper.

Before acting, make one determination:

- **Is this a managed change to one of the infrastructure repos** (an Ansible role/playbook, a Terraform module, a HelmCharts chart, a DockerImages image) **substantial enough to warrant a tracked slice?** Then behave like the other repos' orchestrators: **push back on doing it ad hoc**, and route it through triage → write-slice → run-slice. A go-ahead to author a slice is not a go-ahead to run it; running dispatches code-writing dev agents and is always a separate, explicit operator step.
- **Otherwise** — a quick fix, a diagnosis, an investigation, a one-off operator command, a doc/runbook touch — **just do it.** No slice, no ceremony. You still obey the standing rules below (the operator runs all `terraform`/`ansible` against real infra; you don't read secret values; etc.).

When unsure which side a request falls on, say which way you're leaning and why, and ask — per "When in doubt" below.

### The slice lifecycle and skills

A slice is the tracking unit for a managed infra-repo change. Each step is a deliberate, operator-gated act:

- **`/triage`** — group raw findings / requests / Triage-Inbox cards into change-request bundles under `/work/AnsibleSpecs/change_requests/`. Stops at the bundle; never auto-writes a slice.
- **`/write-slice`** — author a numbered slice from a bundle (overview + acceptance criteria + briefs where a code change is dispatched). Allocates the number via `../AnsibleSpecs/scripts/allocate-next-slice.sh`. Authoring needs the operator's go-ahead.
- **`/run-slice`** — dispatch the dev agents (plan-writer → plan-reviewer → code-writer → code-reviewer) **inline as Task subagents** to make the code change, then validate the acceptance criteria. Never start a run yourself. Verification of anything touching real infra is the operator running `terraform`/`ansible` and reporting back — there is no automated app test suite here.
- **`/arch-design`** (sparingly), **`/update-docs`** — supporting skills.

The dev agents write code in the area the slice touches (`ansible/`, `terraform/`, or a sibling repo for a coordinated change); they run lint, never `apply`. Most slices for this repo are small enough that you carry them yourself — reach for the dev agents when a change is genuinely sizeable.

### Issue log — two shared boards

Work is tracked on two boards shared across all the operator's projects; this repo's cards carry the **`Ansible`** owner tag.

- **Triage** (https://trello.com/b/ETTRJ8iW/triage) — incoming/unstructured work. Lists **Inbox → Accepted → Later → Won't Do**.
- **Kanban** (https://trello.com/b/QNGUAXri/kanban) — slices only, as `[NNN] <title>` cards. Lists **To Do → In Progress → Done**. This is the **live status** for slices (the specs-repo README is just the catalogue).

When the operator asks to add something, drop a card in Triage **Inbox** tagged `Ansible`. When they ask about outstanding work, read the `Ansible` cards on the boards. The boards are shared — act only on `Ansible`-tagged cards, and don't silently adopt untagged or other-repo cards.

**Owner tag = who leads/runs the slice, not where the code lands.** A coordinated cross-repo change, or a change whose context lives here in Ansible(Specs) (e.g. a DockerImages container change driven by the OpenBao plan), is **Ansible-led** — tag it `Ansible`, and the Ansible orchestrator runs it (dispatching dev agents into the sibling repo as needed). Tag a card for a sibling repo (`HelmCharts`, `DockerImages`, `IaCAgent`) only when the work is self-contained there and that repo's own agent should run it.

### Push notifications

Use `python3 tools/ai_workflow/send_message.py --title "<title>" "<message>"` to push to the operator's phone. Notify on completion of anything that took (or was expected to take) over ~10 minutes, and when blocked needing input. "Send me a message" / "let me know" means this.

## Commit early and often

Small, focused commits with clear messages. Do not batch unrelated changes into one commit. When you finish a coherent chunk of work — a role, a runbook update, a decision-record change, a new playbook — commit it before starting the next. When in doubt, commit.

Commit straight to the working branch (usually `main`) as you go — no topic/feature branches. This is single-person homelab territory; there's no one to open a PR against, and a branch just adds a merge step. Same rule in the sibling repos (HelmCharts, DockerImages).

## Explanatory notes decay

Files gain scaffolding while they're being built — TODO markers, inline justifications, walk-through comments, README-style context embedded in role defaults. Once we've moved past a file, strip the sprinkles. Keep only comments that carry a non-obvious *why*.

Rule of thumb: if the comment exists because we were *building* the file together, delete it when we move on. If it would help a reader who opens the file in a year knowing nothing of its history, keep it.

This applies to slice documents in `/work/AnsibleSpecs/` too. Once a slice is done, compress its document down to what remains operationally useful.

## Tooling

- **Poetry** for Python deps. `poetry install` once; `poetry run <cmd>` or activate `.venv/` for ad-hoc commands.
- **Ansible** runs from the `ansible/` directory (where `ansible.cfg` lives). Default inventory is `inventories/prd` (every production-grade host). The `inventories/scratch` inventory holds the disposable scratch fleet (today: two Phase 4 microk8s scratch nodes); pass `-i inventories/scratch` for scratch-VM runs.
- **Terraform** runs from the `terraform/` directory. Provider is `bpg/proxmox`.
- **Linting is manual.** No pre-commit hook — it was removed because it was breaking commits. Run lint yourself before proposing a commit: `poetry run yamllint <paths>` and `poetry run ansible-lint <paths>` for Ansible/YAML changes; `terraform fmt -check` and `terraform validate` for Terraform changes.

## Operator runs Terraform and Ansible — not Claude

The user runs all `terraform apply`, `terraform destroy`, and `ansible-playbook` invocations against the real environment themselves. This includes anything targeting the scratch fleet — it lives on the production PVE cluster, even though the VMs are disposable.

Claude prepares the change (edits the role / module / inventory), proposes the exact command to run, and waits for the user to run it and report the result. Hand back full output for parsing, not "looks good."

Claude **may** use the SSH keys in `/work/Obsidian/Attachments/` to read state for investigation — `qm config <vmid>`, `lsblk`, file inspection, anything strictly read-only. Anything that would cause `changed=N>0` or a `terraform` state mutation is the operator's keystroke.

Read-only Ansible is fine when it's clearly read-only: `ansible -m setup`, `ansible-playbook --check --diff` against a host where the role itself has no side effects (e.g. fact gathering). When in doubt, hand the command to the operator.

## What Claude doesn't read on its own

- **OpenBao secrets.** `bao kv get`, the underlying `kv/data/...` HTTP endpoint, and anything else that returns a credential value require explicit operator permission for *each* path. Listing (`bao kv list`) and metadata reads (`bao kv metadata get`, `bao policy read`) are fine for navigation and audit. Reading a value is a credential disclosure; ask first, scope to the specific leaf, and don't widen on your own.
- **The operator's shell history.** `~/.bash_history` / `~/.zsh_history` / equivalents on srviac / wrkdev / any managed host are off-limits regardless of file mode. They expose past credential entry and unrelated activity. If you need to know what command was run, ask.

## Writing OpenBao secrets via `bao kv put`

`bao kv put` accepts a value from stdin when the key's RHS is `-`. Prefer this over inline `key=value` whenever the value is sensitive: positional args land in the controller's terminal scrollback and shell history (`~/.bash_history`); stdin doesn't.

```
# single-key leaf — pipe the value, don't quote it on the command line
printf %s "$VALUE" | bao kv put -mount=kv iac/foo bar=-

# multi-key leaf — assemble a JSON dict and use the @file form
jq -n --arg a "$AKEY" --arg s "$SKEY" '{access_key_id:$a, secret_access_key:$s}' \
  > /tmp/kv.json
bao kv put -mount=kv shared/ceph-rgw/s3 @/tmp/kv.json
shred -u /tmp/kv.json
```

Same logic applies to `bao kv metadata put -custom-metadata=...` for non-sensitive annotations: those are fine inline since they're not secret material.

### Canonical command shape

When handing a command to the operator to run, use this exact shape:

- **Paths the operator sees.** Claude's `/work` is the operator's `~/source` — they have no `/work`. In any command you hand the operator, use repo-relative paths, or the `~/source/<repo>` form for cross-repo hops — never a `/work/...` absolute path. (Claude still reads its own files under `/work`; this rule is only about what goes into operator-run commands.)
- **One line, `cd <dir> && <command>`.** Single copy-paste runs cleanly; if the `cd` fails, the second half doesn't fire.
- **Terraform**: `cd terraform/prd && terraform apply` — no `poetry run` (terraform doesn't need the venv). Don't propose `terraform plan` as a separate step; `apply` already shows the plan and waits for confirmation.
- **Ansible**: `cd ansible && poetry run ansible-playbook playbooks/<play>.yml --limit <host>`. Inventory defaults to `inventories/prd` per `ansible.cfg`; pass `-i inventories/scratch` only for scratch-fleet runs. Don't pass `--diff` — `ansible.cfg` sets `diff_always = True`. For the check-mode preflight from "Check-mode first" above, append `--check` to the **very end** of the apply command (e.g. `… --limit <host> --check`) so the operator converts it to an apply by deleting the trailing flag — never put `--check` mid-command. Never include `--ask-vault-pass`: the operator's shell has `ANSIBLE_VAULT_PASSWORD_FILE` set, so the vault unlocks automatically.

## Related repos on this machine

Paths below are Claude's mount (`/work/...`); the operator sees the same repos under `~/source/...`. Use the operator's form in commands handed to them (see "Paths the operator sees" above).

- `/work/HelmCharts` — Helm charts + per-environment configs. Jenkins-driven deploys.
- `/work/DockerImages` — Jenkins-built container images.
- `/work/Obsidian` — the user's procedural runbook (Proxmox, Kubernetes, Ceph, Linux, network, Keycloak). Primary source material when building roles.

## Federated architecture model

We take part in a federated Architecture-as-Code model. The architecture for this repository is maintained in `docs/architecture/ansible-architecture.yaml`. Whenever a change is made in this repo that could impact an Enterprise Architecture / ArchiMate model modeling everything owned by this repo, nudge the user to spawn the `update-architecture` agent. The agent is incremental, so it's not a hard requirement that it runs on every change. Nudge a bit harder when significant changes are made (new managed host, new daemon, removed service, renamed external identity). When you are performing work unattended, feel free to invoke the agent yourself.

The agent definitions are installed in the operator's `~/.claude/agents/` — `inventory-architecture` (one-shot seed) and `update-architecture` (permanent, incremental). They are not in this repo. The producer manual at `docs/architecture/producer-manual.md` is the authoritative vocabulary reference; both agents read it on startup from the producer repo's working directory.

## Conventions

- **Hostnames, not IPs.** All managed hosts resolve under the `.home` search domain. Use short hostnames in inventory and task arguments. Don't hard-code IPs.
- **Idempotent tasks.** Every task must be safely re-runnable. Prefer modules over `command`/`shell`; if you must shell out, add `creates:` / `removes:` or a `changed_when:`.
- **Roles own their concern end-to-end.** Role defaults in `defaults/main.yml`. Host-specific settings in `host_vars/`. Environment-level in `group_vars/`.
- **Check-mode first.** For any change against real infrastructure, run with `--check --diff` before applying. The user wants to see diffs before things happen.
- **Cluster upgrades — revisit the dqlite watch-freeze watchdog.** The per-node `dqlite-watchdog.timer` (microk8s role) works around an unreleased upstream bug. On every microk8s channel bump, re-check whether the cluster now carries the fix and the watchdog can be retired. Details and the removal checklist: [`docs/runbooks/k8s-upgrade.md`](docs/runbooks/k8s-upgrade.md) and [`docs/runbooks/dqlite-watch-freeze.md`](docs/runbooks/dqlite-watch-freeze.md).

## When in doubt

The user prefers clarifying questions over silent assumptions. If a decision has downstream consequences for prod, ask before acting.
