# CLAUDE.md

Context for Claude Code working in this repo.

## What this repo is

Ansible + Terraform managing the homelab infrastructure: Proxmox hosts, k8s VMs and cluster, Ceph VMs and cluster, and the Linux dev box baseline. Helm owns Kubernetes workloads (in `/work/HelmCharts`); Jenkins runs deploys.

**Read [`docs/decisions.md`](docs/decisions.md) and [`docs/plan.md`](docs/plan.md) before proposing changes.** The decisions file is the authoritative record of tool split, secrets strategy, scope, and backup/DR. The plan file tracks where we are across phases and points at the current phase document in `docs/phases/`. If a decision changes, update `docs/decisions.md` — do not leave stale notes elsewhere.

## Commit early and often

Small, focused commits with clear messages. Do not batch unrelated changes into one commit. When you finish a coherent chunk of work — a role, a runbook update, a decision-record change, a new playbook — commit it before starting the next. When in doubt, commit.

Commit messages follow the style in existing history: short imperative subject, body explains the why. Always include the `Co-Authored-By` trailer for Claude.

## Explanatory notes decay

Files gain scaffolding while they're being built — TODO markers, inline justifications, walk-through comments, README-style context embedded in role defaults. Once we've moved past a file, strip the sprinkles. Keep only comments that carry a non-obvious *why*.

Rule of thumb: if the comment exists because we were *building* the file together, delete it when we move on. If it would help a reader who opens the file in a year knowing nothing of its history, keep it.

This applies to phase documents too. Once a phase is done, compress its document down to what remains operationally useful.

## Tooling

- **Poetry** for Python deps. `poetry install` once; `poetry run <cmd>` or activate `.venv/` for ad-hoc commands.
- **Ansible** runs from the `ansible/` directory (where `ansible.cfg` lives). Default inventory is `inventories/prd` (every production-grade host). The `inventories/scratch` inventory holds the disposable scratch fleet (today: two Phase 4 microk8s scratch nodes); pass `-i inventories/scratch` for scratch-VM runs.
- **Terraform** runs from the `terraform/` directory. Provider is `bpg/proxmox`.
- **Pre-commit** runs yamllint + ansible-lint on every commit.

## Operator runs Terraform and Ansible — not Claude

The user runs all `terraform apply`, `terraform destroy`, and `ansible-playbook` invocations against the real environment themselves. This includes anything targeting the scratch fleet — it lives on the production PVE cluster, even though the VMs are disposable.

Claude prepares the change (edits the role / module / inventory), proposes the exact command to run, and waits for the user to run it and report the result. Hand back full output for parsing, not "looks good."

Claude **may** use the SSH keys in `/work/Obsidian/Attachments/` to read state for investigation — `qm config <vmid>`, `lsblk`, file inspection, anything strictly read-only. Anything that would cause `changed=N>0` or a `terraform` state mutation is the operator's keystroke.

Read-only Ansible is fine when it's clearly read-only: `ansible -m setup`, `ansible-playbook --check --diff` against a host where the role itself has no side effects (e.g. fact gathering). When in doubt, hand the command to the operator.

## Related repos on this machine

- `/work/HelmCharts` — Helm charts + per-environment configs. Jenkins-driven deploys.
- `/work/DockerImages` — Jenkins-built container images.
- `/work/Obsidian` — the user's procedural runbook (Proxmox, Kubernetes, Ceph, Linux, network, Keycloak). Primary source material when building roles.

## Conventions

- **Hostnames, not IPs.** All managed hosts resolve under the `.home` search domain. Use short hostnames in inventory and task arguments. Don't hard-code IPs.
- **Idempotent tasks.** Every task must be safely re-runnable. Prefer modules over `command`/`shell`; if you must shell out, add `creates:` / `removes:` or a `changed_when:`.
- **Roles own their concern end-to-end.** Role defaults in `defaults/main.yml`. Host-specific settings in `host_vars/`. Environment-level in `group_vars/`.
- **Check-mode first.** For any change against real infrastructure, run with `--check --diff` before applying. The user wants to see diffs before things happen.

## When in doubt

The user prefers clarifying questions over silent assumptions. If a decision has downstream consequences for prod, ask before acting.
