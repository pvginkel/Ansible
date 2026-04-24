# Phase 1 — Bootstrap + baseline + scratch VM

**Status**: 🚧 In progress

## Goal

Every Ubuntu VM we manage needs to reach the same initial state: two user accounts (`pvginkel` for the operator, `ansible` for automation) with the right keys and sudo, plus an OS baseline (timezone, upgrades, qemu-guest-agent, SSH hardening, unattended upgrades).

Phase 1 builds those two roles and exercises them end-to-end against a disposable scratch VM provisioned by Terraform. When it's done, the "create a fresh VM" workflow is one `terraform apply` + one `ansible-playbook` run.

## Prerequisites

- Phase 0 complete.
- SSH access from `wrkdev` to Proxmox hosts as `pvginkel` (passwordless sudo may not exist yet on Proxmox — bootstrap will establish it).
- DNS resolution working for all managed hosts (confirmed).
- `pvginkel@wrkdev` has a usable SSH key (the RSA key from `/work/Obsidian/Linux.md`).

## Scope

### In scope

- Terraform module for a scratch VM on the Proxmox cluster with cloud-init bringing up `pvginkel` + SSH key.
- `bootstrap` role: establishes `pvginkel` + `ansible` users with SSH keys and passwordless sudo. Safe to re-run.
- `baseline` role: ports `/work/Obsidian/Linux.md` minus Samba/winbind/NetBIOS.
- `site.yml` top-level playbook composing the two.
- Runbook: scratch-VM lifecycle (create, bootstrap, baseline, destroy).

### Not in scope

- Adopting any existing real VM into Ansible management. That's Phase 3.
- Touching Proxmox hosts themselves (separate role in Phase 2).
- OpenBao / secrets integration — keys are manually placed on the operator machine and in Bitwarden for now.

## Deliverables

1. `terraform/scratch/` — module + instance definition for a single disposable VM.
2. `ansible/roles/bootstrap/` — idempotent role.
3. `ansible/roles/baseline/` — idempotent role, ported from Linux.md.
4. `ansible/playbooks/site.yml` — top-level playbook.
5. `docs/runbooks/scratch-vm.md` — how to spin one up, test against it, tear it down.

## Open questions

1. **`ansible` user SSH key**: where does the private key live? Bitwarden now, OpenBao once Phase 6 lands? The public key goes in the role.
2. **Which Proxmox node hosts the scratch VM?** Any of them, configurable in Terraform variables. Default TBD.
3. **UFW baseline posture**: allow only inbound SSH by default; each app role opens its own ports? Confirm.
4. **Common tools in baseline**: do we include `htop`, `tmux`, `rsync`, `jq`, or keep baseline strictly minimal?
5. **NetBIOS replacement**: Linux.md uses Samba+winbind for name resolution. We're dropping that. Confirm no lingering consumers rely on NetBIOS lookups.

## Done when

- A fresh VM can be created, bootstrapped, and baselined with:
  ```sh
  cd terraform/scratch && terraform apply
  cd ../../ansible && poetry run ansible-playbook playbooks/site.yml --limit scratch
  ```
- Re-running the same `ansible-playbook` command on the same VM reports **zero changed tasks** (true idempotency).
- The playbook runs with `--check --diff` against at least one existing host (`wrkdev`, say) without proposing any destructive changes — gives confidence the baseline role is safe to adopt in Phase 2/3.
- Runbook reviewed and committed.

## Notes for the next conversation

When Phase 1 starts:
1. Skim `/work/Obsidian/Linux.md` — primary source material for baseline.
2. Skim `/work/Obsidian/Proxmox.md` only as needed — just enough to get a scratch VM off the ground without reaching into Phase 2 territory.
3. Confirm the `pvginkel` public key to embed in the `bootstrap` role (user will paste it or point at a file).
4. Pick a Proxmox node for scratch VMs and an IP/DNS scheme for them.
