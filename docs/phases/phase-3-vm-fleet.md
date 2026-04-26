# Phase 3 — VM fleet via Terraform

**Status**: ⏳ Planned

## Goal

Two complementary outcomes:

1. **`disk_resize` role.** Idempotent reconciliation of guest filesystem against the disk size declared in Terraform — `growpart` + `resize2fs` only on drift, no-op otherwise. Lands first because growing a disk is the most common operational mutation we do today and it's currently a manual three-step routine.
2. **Bring the existing managed VMs under Terraform state.** Import the six currently-running managed VMs (`srvk8sl1`, `srvk8ss1`, `srvk8ss2`, `srvceph1-3`) into tfstate without disrupting them. After this phase, every VM in the `managed` group has both an inventory entry and a Terraform definition, and the workflow `terraform apply (replace) → adopt.yml → site.yml` is the documented and exercised path for replacing any managed VM.

## Prerequisites

- Phase 2 done.
- Inventory carries `vm_id`, `pve_node`, `workload_class` per VM (Phase 2 deliverable).
- `terraform/scratch/` is the reference for module shape — bpg/proxmox provider, cloud-init snippet upload, MAC pinning, ed25519 host key generation are all already worked out there.

## Scope

### In scope

- **`disk_resize` role**:
  - Reads the requested disk size from inventory (or detects from `qm config` — open question 4).
  - Compares against the guest's filesystem size.
  - Runs `growpart` (cloud-utils) and `resize2fs` only on drift.
  - Targets the root partition by default; per-host override for additional volumes.
  - Standalone playbook (`playbooks/grow-disks.yml`) — operator-triggered, not part of `site.yml`.
- **Terraform modelling for the six managed VMs**:
  - Per-VM TF config under `terraform/managed-vms/<name>/` (or single multi-VM module with map — open question 1).
  - Cloud-init user-data, MAC, ed25519 host key, CPU/memory/disks declared.
  - Disk passthroughs codified by `/dev/disk/by-id/` serial: nvme1n1 (cloud-sync ZFS) on srvk8sl1, the three Ceph OSD SSDs on srvceph1/srvceph2/srvceph3.
  - `terraform import` for each VM into state. First `terraform plan` after import returns zero diffs.
  - `lifecycle { ignore_changes = [...] }` on user-data and any other field the import surfaces as drift (open question 3).
- **Rebuild workflow exercise**: replace `wrkscratch` end-to-end via the new path (`terraform apply -replace=... → adopt.yml → site.yml`) and confirm zero residual changes. Documents the procedure for future VM rebuilds.
- Runbook: how to grow a disk, how to rebuild a managed VM.

### Not in scope

- Adopting `k8s_prd` / `ceph_prd` VMs into Ansible management — Phases 4 + 5 own that, alongside the role work that justifies bootstrapping them.
- VMs in `pve_vms_unmanaged` (Home Assistant, Windows, dev workstations) — not Ansible-managed at the OS level, no need to model in Terraform now.
- Provisioning the (future) Jenkins agent VM or OpenBao VM. Those resources are created in their phases (10 and 6 respectively); the `lifecycle { prevent_destroy = true }` requirement from `decisions.md` "Production execution model" lands there, not here. **Carried forward**: when those modules are written, they MUST include `prevent_destroy = true` and the CI plan-stage check that flags `replace`/`destroy` on either resource.
- Deleting `terraform/scratch/` — keep it as the canonical "minimal VM" reference and exercise target.
- Replacing the existing PVE VM snapshot/backup workflow — out of scope.

## Deliverables

1. `ansible/roles/disk_resize/` — role + README.
2. `ansible/playbooks/grow-disks.yml` — operator-triggered standalone.
3. `terraform/managed-vms/` (or per-VM directories) — six modules, six imports.
4. `terraform plan` against managed-vms returns zero diffs after import.
5. `wrkscratch` exercised end-to-end through the rebuild workflow.
6. `docs/runbooks/disk-resize.md` and `docs/runbooks/vm-rebuild.md`.

## Open questions

1. **Module shape** — per-VM `terraform/managed-vms/<name>/` (matches `scratch`'s pattern, easy to apply incrementally, more boilerplate) or single `terraform/managed-vms/` with a map-based loop (DRYer, harder to apply one VM at a time)? Lean: per-VM. Revisit if the boilerplate grows annoying.
2. **Disk passthroughs surviving disk replacement** — `/dev/disk/by-id/<serial>` pins to one specific drive. If a Ceph OSD SSD fails and is replaced, the new serial breaks the TF reference. Either accept the manual TF edit on disk swap (cheap, documented in a runbook) or use a less specific identifier. Lean: pin to serial, runbook the swap procedure.
3. **Drift on import** — already-running VMs have whatever `qm config` they have today; importing into TF will surface fields TF cares about that operator-edited or auto-generated. For things we don't want to manage from TF (e.g. cloud-init user-data on running VMs), `lifecycle { ignore_changes = [...] }` is the lever. List exactly which fields go in `ignore_changes` once the first import is run and `terraform plan` shows the diff.
4. **`disk_resize` size source** — read the requested size from inventory (`baseline_disk_size` style host_var) or from the VM's actual `qm config <vmid>` `scsi0` size? The former is declarative; the latter is "make the guest match what PVE thinks." Lean: from `qm config` via a fact-gathering task on the PVE node, with inventory override for cases where TF and PVE disagree.
5. **Per-VM partition layout** — most VMs are simple root + EFI. srvk8sl1 has multiple disks (root, k8s data, cloud-sync ZFS passthrough); srvceph nodes have root + Ceph OSD passthrough. The role only resizes Ansible-managed filesystems — Ceph OSD passthroughs and ZFS pools are not the role's business. Make the partition list explicit per-host.
6. **Order of work** — disk_resize first (against `wrkscratch`), then VM modelling? Or the reverse? Lean: disk_resize first. Less coupled, easy to verify, immediately useful.

## Done when

- `disk_resize` role applies cleanly to a fresh `wrkscratch` after a Terraform-driven disk grow; second run reports `changed=0`.
- All six managed VMs are in tfstate; `terraform plan` shows zero diffs against each.
- `wrkscratch` rebuilt end-to-end via the new workflow; the resulting VM passes `site.yml --limit wrkscratch --check --diff` clean.
- Both runbooks reviewed and committed.

## Notes for the next conversation

When Phase 3 starts:

1. Pull `qm config <vmid>` for the six managed VMs. The Phase 2 inspection captured this earlier; either reuse or re-run to confirm nothing changed.
2. Decide module shape (open question 1) before writing Terraform.
3. Confirm the disk passthroughs are still in their current form (the `srvk8sl1` cloud-sync NVMe, the three Ceph OSD SSDs).
4. Resolve the dead-zpool on `pve`'s `/dev/sda` from the Phase 2 inspection — either declare it in TF as part of an existing VM or remove the disk. Out of scope for the role work but a natural moment to clean up.
5. Confirm `disk_resize` first vs VM modelling first (open question 6).
