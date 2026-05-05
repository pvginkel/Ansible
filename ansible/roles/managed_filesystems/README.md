# `managed_filesystems` role

Reconciles guest filesystems on managed Proxmox VMs end-to-end. On a fresh disk: partition + format + mount + persist fstab. On a converged disk: rescan + `growpart` + `resize2fs` only on drift. Idempotent in both directions — every step reports `ok` on a steady-state host.

Replaces the earlier `disk_resize` role; the same `(scsi_index, mountpoint, fstype)` schema now drives both create-time and grow-time work.

## Mental model

Terraform owns the PVE-side disk geometry; Ansible's job is to make the **guest** match what PVE says. The role reads `qm config <vmid>` for each declared `scsiN` slot, resolves the corresponding guest disk via HCTL (`host:channel:target:lun`), and reconciles partition + filesystem + mount in one pass.

Operationally this covers two flows:

1. **First-boot of a fresh VM** — the role partitions the data disk, formats it ext4, and mounts it before workload roles (e.g. `microk8s`) start populating the mountpoint.
2. **Post-grow reconciliation** — after `terraform apply` resizes a disk PVE-side, `playbooks/grow-disks.yml` runs the role to catch the guest up.

## Inputs

Per host:

- `vm_id` — Proxmox VMID. Already required for affinity reconciliation in `proxmox_host`.
- `pve_node` — which physical PVE node hosts the VM (`pve` by default via `group_vars/pve_vms.yml`; `pve1` / `pve2` overridden in host_vars).
- `managed_filesystems_volumes` — list of `{scsi_index, mountpoint, fstype}` entries declaring every filesystem the host owns.

Default `managed_filesystems_volumes: []` — the role no-ops on hosts without entries (bare-metal PVE nodes inherit nothing). VMs in the `pve_vms` group inherit a root entry from `group_vars/pve_vms.yml`. k8s_prd extends with `/var/snap` on `scsi1`.

Example override for a VM with a separate data disk on `scsi1`:

```yaml
managed_filesystems_volumes:
  - scsi_index: 0
    mountpoint: /
    fstype: ext4
  - scsi_index: 1
    mountpoint: /var/snap
    fstype: ext4
```

## What it does (per filesystem)

1. Reads the declared size from `qm config <vmid>` on the VM's `pve_node` via `delegate_to`.
2. Resolves the guest disk by HCTL (`*:*:<scsi_index>:0`) — used to validate that the inventory and the guest agree on which slot holds which disk.
3. Branches on whether the declared mountpoint is already mounted:
   - **Already mounted** (root, legacy `/var/snap`): derive the partition from `findmnt`, validate that its parent matches the HCTL-resolved disk, and assert that the running fstype matches the declared one. Skip the create path entirely (no `parted`, no `mkfs`).
   - **Not mounted** (fresh data disk): inspect the HCTL disk's children. If blank, `community.general.parted` creates a single GPT partition spanning the disk. If a partition already exists with a different fstype, fail-loud. Multi-partition unmounted layouts also fail-loud — the role doesn't try to guess which partition.
4. **Format path** (common) — `community.general.filesystem` runs `mkfs.<fstype>` only on a blank partition; idempotent against an existing matching fstype.
5. **Mount path** (common) — `ansible.posix.mount` with `state: mounted` and `src: UUID=<uuid>` (read via `blkid`) realizes the mount in the running system and writes the fstab line in one step.
6. **Grow path** (common) — SCSI rescan, then `growpart` + `resize2fs`. `growpart`'s `NOCHANGE` exit and `resize2fs`'s `Nothing to do` marker both keep `changed=0` on converged disks.

## Constraints

- **Partitioned disks only.** The role assumes a single partition under the disk. No filesystem-on-raw-block, no multiple-partitions-per-disk.
- **ext4 only.** Both `community.general.filesystem` and the `growpart` / `resize2fs` chain are wired for ext4. xfs/btrfs would need a parallel branch.
- **No LVM / mdraid / dm-crypt walking.** The partition under each declared mount must be the direct child of the disk we touch.
- **Partition on PVE side stays passthrough-free.** Ceph OSDs and ZFS-passthrough drives are managed by their own stacks; do not list them in `managed_filesystems`.

## Failure modes

- `qm config` for the VMID has no matching `scsiN:` line — the slot was renamed or removed; fix inventory.
- HCTL lookup finds no disk — the qm config declared the slot but the virtio-scsi controller did not surface it (e.g., wrong `scsihw`); investigate before re-running.
- Existing partition carries a different fstype than declared — fails fast; re-rebuild or hand-fix.
- Guest sees a smaller disk than PVE after rescan — typically a kernel that needs a reboot to re-read SCSI capacity; the role fails fast rather than silently no-op.

## First-run quirk on legacy nodes

Adoption-era nodes (`srvk8sl1`, `srvk8ss2`) carry `/dev/sdb1 → /var/snap` from out-of-band setup and may have device-name fstab entries (`/dev/sdb1 /var/snap …`). The first run of this role rewrites those entries to `UUID=<uuid> /var/snap …`. Subsequent runs are idempotent. Always preview with `--check --diff` before applying.
