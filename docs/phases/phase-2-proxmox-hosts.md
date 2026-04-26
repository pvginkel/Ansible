# Phase 2 — Proxmox host management

**Status**: ⏳ Planned

## Goal

Bring the three Proxmox cluster nodes (`pve`, `pve1`, `pve2`) under Ansible management. Two deliverables make this possible:

1. **`adopt.yml`** — generic playbook for onboarding a non-cloud-init'd host (creates the `ansible` user, drops the key, captures the host's SSH host key into the repo). Usable for any existing host, not just PVE.
2. **`proxmox_host` role** — Proxmox-specific tunables ported from `/work/Obsidian/Proxmox.md`, plus CPU affinity reconciliation per the model recorded in `docs/decisions.md` "Proxmox VM CPU affinity".

After this phase, a `--check --diff` run of `site.yml` against the prd inventory should produce no destructive changes against the live PVE cluster.

## Prerequisites

- Phase 1 done.
- SSH access from `wrkdev` to `pve`, `pve1`, `pve2` as `pvginkel` with sudo (password-prompted).
- Skim `/work/Obsidian/Proxmox.md` for the host-config bits (sysctl tuning at the bottom). The earlier sections about VM creation defaults belong in Phase 3's Terraform modules, not here.
- Read `docs/decisions.md` "Proxmox VM CPU affinity" — the affinity model is already settled.

## Scope

### In scope

- `adopt.yml` — runs as the operator (`-u pvginkel -K`), creates `ansible` user + sudoers + authorized_keys, captures the host's existing SSH host key into `ansible/files/known_hosts.d/<group>`. After it runs, the host is reachable via the normal `site.yml` flow.
- `proxmox_host` role:
  - sysctl tuning per `Proxmox.md` (vm.swappiness=1, vm.dirty_bytes, vm.dirty_background_bytes — concretely targets the swap/backup-throughput problem documented there).
  - CPU affinity reconciliation: reads a per-host `proxmox_vm_affinity: { <vmid>: "<core-range>" }` map from inventory, runs `qm set <vmid> --affinity <range>` idempotently. Only `pve` carries this map; `pve1`/`pve2` aren't core-zoned.
- Make `baseline` PVE-compatible: PVE is Debian, not Ubuntu, and is a physical host (no qemu-guest-agent). Decision needed (open question 1) on whether to extend baseline with conditionals or factor the shared bits into a separate role.
- Update inventory: `inventories/prd/host_vars/pve.yml` carries the affinity map, plus per-host overrides for any baseline tasks that don't apply.
- Adopt `pve`, `pve1`, `pve2` end-to-end. `--check --diff` against the live cluster must come back clean before a real apply.
- **Bonus**: adopt `wrkdev` with `adopt.yml` first as a low-risk smoke test (closes the Phase 1 deferred item).

### Not in scope

- VM creation defaults (BIOS/UEFI/SCSI/VirtIO recommendations from `Proxmox.md`) — those live in Terraform modules, Phase 3.
- Codifying PVE backup jobs in Ansible — existing snapshots + cloud sync stay as-is per `docs/decisions.md` "Existing backup context".
- PVE repository management (no-subscription vs. enterprise repo) — defer unless we hit concrete pain.
- Cluster firewall, datacenter SDN, cluster-wide options — defer.
- Proxmox upgrade orchestration — Phase 4 + Phase 5 establish the upgrade pattern for k8s/Ceph; PVE upgrades follow once that pattern is proven.

## Deliverables

1. `ansible/playbooks/adopt.yml` — adoption playbook.
2. `ansible/roles/proxmox_host/` — sysctl + CPU affinity reconciliation + any other PVE-specific config that earns its place.
3. `ansible/inventories/prd/host_vars/pve.yml` — affinity map for VMs running on `pve`.
4. `baseline` role: tweaks to coexist with Debian (PVE) hosts.
5. `docs/runbooks/adoption.md` — how to onboard a non-cloud-init'd host using `adopt.yml`.
6. `wrkdev` adopted (Phase 1 follow-up).
7. `pve`, `pve1`, `pve2` adopted, with `site.yml --check --diff` showing no destructive changes.

## Open questions

1. **`baseline` on Debian/PVE**: extend with conditionals (`when: ansible_distribution == 'Ubuntu'` on Ubuntu-only tasks) or factor the truly-shared bits (timezone, vimrc, motd) into a `common` role and have `baseline`/`proxmox_host` both consume it? Conditionals are quicker; factoring is cleaner long-term. Lean: conditionals first, factor only if a third consumer appears.
2. **VM affinity map**: what's the actual `vmid → core-range` mapping for VMs on `pve`? Need the list before designing the inventory layout. Operator will provide.
3. **`adopt.yml` output**: should it edit the inventory automatically, or print a fragment the operator pastes in? Lean: print a fragment — keeps Ansible from owning a file the operator also edits.
4. **Adoption order**: `wrkdev` (lowest blast radius) → `pve2` → `pve1` → `pve` (cluster master last)? Confirm.
5. **Parallel adoption**: run `adopt.yml` against all three PVE nodes simultaneously, or one at a time? One at a time is safer; the operator gets a full review per host.
6. **Host-key capture**: does `adopt.yml` capture all key types the host serves, or pin to ed25519 only? Ed25519-only matches the current `ansible.cfg` `HostKeyAlgorithms` setting, but adopting a host with no ed25519 key (older Debian default may differ) needs handling.

## Done when

- `adopt.yml` runs cleanly against `wrkdev` and produces a diff that is committed (new entry in `known_hosts.d/`, optional `host_vars/wrkdev.yml`).
- Same for `pve`, `pve1`, `pve2`.
- `proxmox_host` role applies cleanly against all three PVE nodes; second run reports `changed=0`.
- `qm config <vmid>` on `pve` shows the affinity values that match inventory.
- `poetry run ansible-playbook playbooks/site.yml -i inventories/prd --check --diff` returns zero destructive proposals across the full prd inventory.
- Adoption runbook reviewed and committed.

## Notes for the next conversation

When Phase 2 starts:
1. Skim `/work/Obsidian/Proxmox.md` (the bottom sections about RAM/swap tuning — the upper sections are VM-creation notes for Phase 3).
2. Get the actual `vmid → core-range` affinity map from the operator.
3. Confirm adoption order (default proposal: `wrkdev` first, then PVE nodes one at a time, master last).
4. Decide question 1 (Debian conditionals in baseline vs. extracting `common`) before writing role code.
