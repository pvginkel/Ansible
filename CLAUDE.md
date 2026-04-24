# CLAUDE.md

Context for Claude Code working in this repo.

## What this repo is

Ansible + Terraform managing the homelab infrastructure: Proxmox hosts, k8s VMs and cluster, Ceph VMs and cluster, and the Linux dev box baseline. Helm owns Kubernetes workloads (in `/work/HelmCharts`); Jenkins runs deploys.

**Read [`docs/decisions.md`](docs/decisions.md) before proposing changes.** It is the authoritative record of tool split, secrets strategy, scope, and backup/DR. If a decision changes, update that file — do not leave stale notes elsewhere.

## Commit early and often

Small, focused commits with clear messages. Do not batch unrelated changes into one commit. When you finish a coherent chunk of work — a role, a runbook update, a decision-record change, a new playbook — commit it before starting the next. When in doubt, commit.

Commit messages follow the style in existing history: short imperative subject, body explains the why. Always include the `Co-Authored-By` trailer for Claude.

## Tooling

- **Poetry** for Python deps. `poetry install` once; `poetry run <cmd>` or activate `.venv/` for ad-hoc commands.
- **Ansible** runs from the `ansible/` directory (where `ansible.cfg` lives). Default inventory is `inventories/prd`; dev work requires explicit `-i inventories/dev`.
- **Terraform** runs from the `terraform/` directory. Provider is `bpg/proxmox`.
- **Pre-commit** runs yamllint + ansible-lint on every commit.

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
