# Phase 1 — Bootstrap + baseline + scratch VM

**Status**: ✅ Done

## What this phase delivered

Two foundational Ansible roles + a disposable Terraform-provisioned VM to exercise them against.

- `ansible/roles/bootstrap/` — establishes `ansible` (UID 900, NOPASSWD sudo, ed25519 key) and `pvginkel` (UID 1000, sudo group with password, ed25519 key). Idempotent against both fresh cloud-init'd VMs and bare hosts.
- `ansible/roles/baseline/` — Ubuntu OS hygiene: timezone, qemu-guest-agent, motd cleanup, vimrc. Purges `unattended-upgrades` (Ansible owns updates — see `docs/decisions.md` "OS updates").
- `ansible/playbooks/site.yml` — composes both roles against the inventory.
- `terraform/scratch/` — disposable Ubuntu 24.04 VM on `pve` with pinned MAC + cloud-init-injected ed25519 host key. Recreate with `terraform apply -replace=proxmox_virtual_environment_vm.scratch`.

Operational docs:
- `docs/runbooks/operator-workstation.md` — workstation prereqs (Poetry, two SSH identities, DNS).
- `docs/runbooks/proxmox-api-token.md` — Terraform's API token + custom role.
- `docs/runbooks/scratch-vm.md` — full scratch VM lifecycle.

## What didn't land in this phase

The original "done when" criteria included a `--check --diff` smoke test against `wrkdev`. Deferred to Phase 2: `wrkdev` is a non-cloud-init'd host, so it needs the adoption workflow (`adopt.yml`) Phase 2 builds. Once that exists, the wrkdev smoke test happens naturally as the first adoption.
