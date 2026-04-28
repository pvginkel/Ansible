# Phase 3a — VM fleet under Terraform state

**Status**: ✅ Done

## What shipped

- **Six per-VM Terraform modules** under `terraform/managed-vms/<name>/` (`srvceph1`, `srvceph2`, `srvceph3`, `srvk8sl1`, `srvk8ss1`, `srvk8ss2`). Each is a thin caller of the shared `terraform/modules/managed-vm/` child module. All six are imported into per-VM `tfstate` and converge to a zero-diff plan.
- **Shared `managed-vm` child module** with the resource shape, the backup-flag derivation from `pve_node_backup_datastore` in Ansible inventory, `reboot_after_update = false`, and dynamic `efi_disk` (only emitted for `bios = "ovmf"`).
- **`pve_node_backup_datastore` inventory attribute** on `pve` only — single source of truth driving each managed disk's `backup` flag. pve1 / pve2 carry no host_vars file; child module treats absence as "no backup datastore."
- **Rebuild flow exercised** end-to-end on `wrkscratch` (`terraform apply -replace` → `site.yml` → `site.yml --check --diff` zero-residual).
- **`docs/runbooks/vm-rebuild.md`** — the canonical rebuild procedure, including the disk-passthrough swap subsection.

## Constraints surfaced (now in `decisions.md`)

- **Disk passthrough on managed VMs** — PVE blocks API tokens from passing arbitrary filesystem paths. Existing passthroughs survive imports cleanly but cannot be created or modified by Terraform; rebuilds drop the passthrough block from the module and Ansible reattaches.
- **Terraform applies on cluster members never reboot directly** — `reboot_after_update = false` baked into the child module. Reboots stay owned by Ansible's drain-aware update flow.
- **Network topology** (vmbr0 + vmbr1 + vmbr0 tag=2) and **VMID convention** (legacy 100–199 vs the 900-and-up TF range) both written down for the first time.
- **Legacy MAC carry-forward** — existing BC:24:11:... MACs pinned verbatim. The deterministic `02:A7:F3:VV:VV:EE` scheme applies post-rebuild.

## Carried forward to Phase 4 / 5

The current per-VM modules are the **adoption shape**. To rebuild a cluster member, the module needs the from-scratch shape (cloud-init snippet, `tls_private_key`, `local_file` for `known_hosts.d/`, deterministic MAC, optional VMID rotation into the 900-range, passthrough block removed). That commit lands per-VM as part of Phase 4 (k8s) and Phase 5 (Ceph). `vm-rebuild.md` outlines the procedure; the playbook hooks (cordon/drain, OSD reattach) are owned by those phases.

`srvk8ss2` is currently `bios = "seabios"` matching reality — the BIOS→UEFI flip is part of its Phase 4 rebuild commit (memory: `project_srvk8ss2_uefi.md`).
