# Phase 3a — VM fleet under Terraform state

**Status**: ⏳ Planned

Spun out of Phase 3 once the `disk_resize` role had shipped. Phase 3a delivers the second half of the original Phase 3 plan: bringing the six existing managed VMs under `tfstate` and exercising the rebuild workflow on `wrkscratch`.

## Goal

Two outcomes:

1. **Every managed VM has a Terraform module.** `srvk8sl1`, `srvk8ss1`, `srvk8ss2`, `srvceph1`, `srvceph2`, `srvceph3` live in `terraform/managed-vms/<name>/`. Each has been imported into state. `terraform plan` against each returns zero diffs. The decisions encoded along the way (backup-by-node, passthrough pinning, BIOS→UEFI normalization on `srvk8ss2`) all converge to clean plans.
2. **The rebuild workflow is documented and exercised.** `terraform apply -replace=<vm-resource> → adopt.yml → site.yml` against `wrkscratch` lands cleanly, and the procedure is captured in `docs/runbooks/vm-rebuild.md` so it's available when Phase 4/5 need to rebuild a real cluster member.

## Prerequisites

- Phase 3 done — `disk_resize` is the canonical example for any role that needs PVE-side config via `delegate_to`.
- Phase 2 inventory contracts (`vm_id`, `pve_node`, `workload_class`) intact for every managed VM.
- `terraform/scratch/` is the reference module shape — bpg/proxmox provider, cloud-init snippet upload, MAC pinning, ed25519 host key generation, `lifecycle.ignore_changes` for the cloud image and CPU affinity are all already worked out there.
- Operator's PVE-root SSH key in agent (Terraform's snippet upload uses it).

## Scope

### In scope

- **Per-VM Terraform modules** under `terraform/managed-vms/<name>/`. Per-VM directories matching the `scratch` pattern (open question 1 from original Phase 3, lean accepted).
- **Backup-by-node attribute** — `pve_node_backup_datastore` on each PVE host's `host_vars`, read by the per-VM modules to set `backup=true|false` on each managed disk. Passthrough disks always `backup=false` regardless of node. See `decisions.md` "Backup". Net effect:
  - `srvceph3`, `srvk8sl1` (on `pve`): managed disks `backup=true`, passthroughs `backup=false`. Matches today.
  - `srvceph1`/`2`, `srvk8ss1`/`2` (on `pve1`/`pve2`): all disks `backup=false`. Their managed disks are `backup=true` today by accident; the cluster vzdump job is `node: pve` only so nothing actually runs against them — the flag is misleading, not actively bad. Bringing them in line is a one-line `qm set` per VM, run by the operator.
- **Disk passthroughs codified by `/dev/disk/by-id/<serial>`** — nvme1n1 (cloud-sync ZFS) on `srvk8sl1`, the three Ceph OSD SSDs on `srvceph1`/`2`/`3`. Pin to serial; serial change on disk swap is a documented runbook procedure (open question 2 from original Phase 3, lean accepted).
- **`srvk8ss2` BIOS → UEFI normalization** — model the VM as UEFI in TF (matching its siblings); operator flips the live VM via PVE config (`bios=ovmf`, add `efidisk0`), with `grub-efi-amd64` already installed in the guest beforehand. Once the manual flip is done, `terraform plan` is clean. Rebuild is the fallback if the manual flip goes sideways, but Phase 4's drain/cordon is what protects the cluster on a rebuild — hold the rebuild for then if it comes to it.
- **`terraform import` for each VM** into state. First `terraform plan` after import lists the drift; tune `lifecycle.ignore_changes` until the plan is empty.
- **Rebuild workflow exercise** — replace `wrkscratch` end-to-end via `terraform apply -replace=... → adopt.yml → site.yml`. Verify zero residual changes from `site.yml --check --diff` afterward.
- **Runbooks** — `docs/runbooks/vm-rebuild.md`; extend `disk-resize.md` if the multi-disk overrides need additional examples once real managed VMs have them.

### Not in scope

- Adopting `k8s_prd` / `ceph_prd` VMs into Ansible management — Phases 4 + 5.
- VMs in `pve_vms_unmanaged` (Home Assistant, Windows, dev workstations).
- Provisioning the (future) Jenkins agent VM or OpenBao VM. Their `lifecycle.prevent_destroy = true` requirement from `decisions.md` lands in Phase 6 and Phase 10 respectively.
- Deleting `terraform/scratch/` — keep it as the canonical "minimal VM" reference and exercise target.
- Replacing the existing PVE VM snapshot/backup workflow — out of scope.
- The two backup follow-ups noted as deferred in `decisions.md` (Ansible-side drift assertion of the backup policy, vzdump job's `node`/`storage` derived from the same attribute) — flagged for Phase 10 / future tidy.

## Deliverables

1. `terraform/managed-vms/<name>/` — six per-VM modules, six imports, six zero-diff plans.
2. `pve_node_backup_datastore` attribute on each PVE node's `host_vars`; per-VM modules read it to compute the disk `backup` flag.
3. `wrkscratch` rebuilt end-to-end through the new workflow; resulting VM passes `site.yml --limit wrkscratch --check --diff` clean.
4. `docs/runbooks/vm-rebuild.md`. Disk-passthrough swap procedure documented (likely as a section in `vm-rebuild.md` or a sibling runbook).

## Open questions

1. **Order of VM modeling.** Lean: start with `srvceph3` — simplest layout (root + data + OSD passthrough), on `pve` (same node Terraform reaches), smallest blast radius. Move outward from there.
2. **Backup attribute first, or modules first.** The per-VM module needs the attribute to compute the `backup` flag, so the attribute lands first — at minimum a no-op definition on each PVE host's `host_vars` (`pve_node_backup_datastore: local-backup` on `pve`, empty/undefined on `pve1`/`pve2`).
3. **`srvk8ss2` UEFI flip — before or during import.** If the operator flips first, the import sees a UEFI VM matching the TF module. If the import runs first against the BIOS-as-it-is-today VM, plan will show drift and the manual flip becomes the resolution. Lean: flip first (less ambiguous state during import), but either order works.
4. **Drift on import.** Already-running VMs surface fields TF cares about that operator-edited or auto-generated. List exactly which fields go in `lifecycle.ignore_changes` once the first import is run and `terraform plan` shows the actual diff. The two patterns we already know:
   - `cpu[0].affinity` (Ansible-owned) — already pinned in the scratch module, replicate.
   - `disk[0].file_id` for the cloud image — `scratch`'s pattern. May not apply to the managed VMs since they were not provisioned from a cloud image originally; confirm at first import.

## Done when

- All six managed VMs are in `tfstate`; `terraform plan` shows zero diffs against each.
- `wrkscratch` rebuilt end-to-end via the new workflow; the resulting VM passes `site.yml --limit wrkscratch --check --diff` clean.
- `vm-rebuild.md` reviewed and committed.

## Notes for the next conversation

When Phase 3a starts:

1. The `qm config` for the six VMs was pulled at the start of Phase 3 — re-pull if the gap has been long enough that drift is plausible, otherwise reuse.
2. Confirm the operator has flipped `srvk8ss2` to UEFI (or coordinate to do it as the first step).
3. Decide order of work (open question 1) — lean is `srvceph3` first.
4. Implement `pve_node_backup_datastore` attribute as the first commit before any per-VM module lands, so each module already has the value to read.
