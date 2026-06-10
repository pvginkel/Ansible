# `zfs` role

Creates ZFS pools on a managed VM, **create-if-absent**. Declares its
work per host through `zfs_pools`; on a host with no entries the role is
a no-op. Pools only — no dataset, property, or zvol management today.

This is the *creation* counterpart to the pool **import** that
`rebuild-k8s.yml` does for passthrough pools (`zpools_to_import`). The
two are deliberately separate:

- **Create** (this role) — a virtual disk on PVE storage is reformatted
  whenever the VM is rebuilt, so its pool has to be *recreated* on a
  fresh disk. Idempotent create-if-absent covers both first boot and
  every rebuild.
- **Import** (`rebuild-k8s.yml`) — a passthrough disk (e.g. srvk8s1's
  NVMe-backed `zpool2`) survives the rebuild with its on-disk pool
  intact, so it is `zpool import -f`'d, never recreated.

## Mental model

Terraform owns the PVE-side disk (a `managed_disks` entry in
`terraform/prd/vms.tf`); this role makes the pool on the guest match
what the inventory declares. It resolves the backing disk from the
declared `scsi_index` via its stable `/dev/disk/by-id/` path
(PVE/QEMU serialises SCSI disks as `drive-scsiN`), refuses to write over
a disk that already carries any signature, and `zpool create`s an empty
pool mounted at the declared `mountpoint`.

ZFS auto-mounts the pool's root dataset and auto-imports it on reboot
via the standard `zfs-import-cache` / `zfs-mount` services — no fstab
entry, same as every other pool in the homelab.

## Inputs

Per host, in `host_vars/<name>.yml`:

```yaml
zfs_pools:
  - name: zpool1       # pool name == root dataset name
    scsi_index: 2      # PVE scsiN slot backing the pool
    mountpoint: /zpool1
```

Default `zfs_pools: []` — no-op on every host that declares nothing.

## What it does (per pool)

1. Resolves the backing disk to `/dev/disk/by-id/scsi-0QEMU_QEMU_HARDDISK_drive-scsi<index>` and asserts it is a present block device (fails loud with a "did `terraform apply` run?" hint if not).
2. Skips entirely when a pool of that name is already imported (`zpool list`).
3. Otherwise asserts the disk is blank (`blkid -p` exits non-zero, no output) before creating — never reformats a disk that carries a filesystem, partition table, or an un-imported pool.
4. `zpool create -o ashift=12 -m <mountpoint> <name> <by-id>` — an empty pool, no child datasets.

## Constraints

- **Single whole-disk vdev.** One disk per pool, no mirror/raidz, no partitioning. Matches the homelab's single-disk pools.
- **Create-only.** The role never destroys a pool, changes its properties, or creates datasets inside it. Growing, mirroring, or dataset management is out of scope and would be hand-done or a future extension.
- **Virtual `managed_disks` only.** The `drive-scsiN` by-id path is how PVE serialises VM-backed disks; passthrough pools use the import path instead.
