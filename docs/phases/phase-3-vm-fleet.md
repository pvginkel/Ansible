# Phase 3 — VM fleet via Terraform (part 1: `disk_resize`)

**Status**: ✅ Done

The phase split mid-flight. Part 1 — the `disk_resize` role, the most common day-to-day operational mutation — landed here. Part 2 — bringing the six managed VMs under `tfstate` and exercising the rebuild workflow — moved to [Phase 3a](phase-3a-vm-fleet-import.md) so the architecture rework that prompted the split could land first.

## What landed

- [`ansible/roles/disk_resize/`](../../ansible/roles/disk_resize/) — reconciles the guest filesystem against the size PVE declares for the VM's `scsiN` slot. `growpart` + `resize2fs` only on drift. Reads the requested size from `qm config <vmid>` on the VM's `pve_node` via `delegate_to`, so Ansible stays decoupled from `tfstate`.
- [`ansible/playbooks/grow-disks.yml`](../../ansible/playbooks/grow-disks.yml) — operator-triggered standalone, not part of `site.yml`.
- [`docs/runbooks/disk-resize.md`](../runbooks/disk-resize.md) — the new "edit Terraform → apply → grow-disks" flow, replacing the manual three-step PVE UI procedure.
- `host_vars/pve.yml` — documents `/dev/sda` as an intentional uncommitted spare.
- `decisions.md` — codifies "backup follows the PVE node, not the VM"; passthrough disks are always `backup=false`. Implementation lands in Phase 3a.
- `CLAUDE.md` — operator runs `terraform apply` and `ansible-playbook` against the real environment, not Claude. Read-only investigation stays in Claude's lane.

## Operationally useful afterwards

- **Growing a disk** — `docs/runbooks/disk-resize.md`. Edit `size_gb` in TF → `terraform apply` → `ansible-playbook playbooks/grow-disks.yml --limit <host>`.
- **Multiple managed filesystems on one VM** — set `disk_resize_filesystems` in the VM's `host_vars` to extend beyond the default root-only scope. Passthrough disks (Ceph OSDs, ZFS-passthrough drives) **must not** be added to the list.
- **Reading PVE config from a playbook** — the role's `delegate_to: "{{ pve_node }}"` pattern (combined with `become: true` to escalate to root for `qm config`) is the template for any future role that needs PVE-side data per VM.
- **Inventory contract** — `disk_resize` requires `vm_id` and `pve_node` on the target host. All managed VMs already carry these; new VMs added in inventory must too. The `scratch` inventory carries `wrkscratch`'s `vm_id` / `pve_node`; `prd` keeps an affinity-only stub so `proxmox_host` can reconcile when wrkscratch is running.
