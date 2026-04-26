# `disk_resize` role

Reconciles guest filesystems against the PVE-side disk size declared in `qm config`. Idempotent — `growpart` and `resize2fs` run only on drift.

## Mental model

Terraform owns the PVE-side disk geometry. Ansible's job is to make the **guest** match what PVE says: read `qm config <vmid>` for the relevant `scsiN` slot, then grow the in-guest partition + filesystem if the guest is lagging.

Operationally:

1. Edit `size_gb` in the VM's Terraform config.
2. `terraform apply` — PVE resizes the underlying volume. No reboot needed for SCSI.
3. `ansible-playbook playbooks/grow-disks.yml --limit <host>` — guest catches up.

## Inputs

Per host:

- `vm_id` — Proxmox VMID. Already required for affinity reconciliation in `proxmox_host`; see `docs/decisions.md`.
- `pve_node` — which physical PVE node hosts the VM (`pve` by default via `group_vars/pve_vms.yml`; `pve1` / `pve2` overridden in host_vars).
- `disk_resize_filesystems` (defaults to root only) — list of `{mountpoint, scsi_index}` pairs declaring which filesystems to keep aligned with which PVE slots.

Example override for a VM with a separate data disk on `scsi1`:

```yaml
# inventories/prd/host_vars/srvk8sl1.yml
disk_resize_filesystems:
  - mountpoint: /
    scsi_index: 0
  - mountpoint: /var/snap/microk8s/common
    scsi_index: 1
```

## What it does (per filesystem)

1. Reads the declared size from `qm config <vmid>` on the VM's `pve_node` via `delegate_to`.
2. Resolves the guest device backing the mount (`findmnt` → partition → parent disk via `lsblk`).
3. Triggers a SCSI rescan so the guest picks up any pending capacity bump.
4. Asserts the guest now sees a disk at least as large as PVE declares (with 1 MiB tolerance for GPT secondary header overhead). If not, fails — typically means the rescan didn't take and a reboot is needed.
5. Runs `growpart` on the partition; `failed_when` accepts the `NOCHANGE` no-op path.
6. Runs `resize2fs` on the partition; `changed_when` keeps "Nothing to do!" green.

## Constraints

- **Partitioned disks only.** The role assumes a partition under the disk, not a filesystem on a raw block device. All current managed VMs and the cloud-init Ubuntu scratch image satisfy this.
- **ext4 only at the resize step.** `resize2fs` is the only filesystem grow tool wired up. xfs/btrfs would need a parallel branch; not in scope for Phase 3.
- **No LVM / mdraid / dm-crypt walking.** `lsblk PKNAME` is consulted once; the partition under the mount must be the direct child of the disk we resize. If a managed VM later layers LVM on top, the role needs an extension.
- **Passthrough disks are out of scope.** Operators do not list them in `disk_resize_filesystems`. Ceph OSD volumes and ZFS-passthrough drives are managed by their respective stacks.

## Failure modes

- `qm config` for the VMID has no matching `scsiN:` line — the slot was renamed or removed; fix inventory.
- Guest sees a smaller disk than PVE after rescan — typically a kernel that needs a reboot to re-read the SCSI capacity. The role fails fast rather than silently leaving the filesystem at the old size.
