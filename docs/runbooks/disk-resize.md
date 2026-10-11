# Growing a managed VM's disk

How to grow the boot (or a data) disk on a Terraform-provisioned, Ansible-managed VM. Replaces the manual three-step "grow in PVE UI → growpart → resize2fs" routine. A ZFS pool on a virtual disk grows differently in the guest: see [its section](#growing-a-zfs-pool-on-a-virtual-disk).

The contract is split across the two tools:

- **Terraform** owns the PVE-side disk geometry — bumping `size_gb` is the only place you touch.
- **Ansible** owns the guest-side filesystem — the `managed_filesystems` role reads `qm config` from the VM's PVE host and reconciles the partition + filesystem to match. (Same role also handles the first-boot create path: partition + format + mount on a fresh data disk. On a converged host every step reports `ok`.)

Disks can only grow. Shrinking requires destroy + recreate of the VM (the rebuild path).

## Prerequisites

- Both SSH identities loaded in `ssh-agent` per [`operator-workstation.md`](operator-workstation.md): `pve-root` (Terraform → PVE) and the `ansible` service key (Ansible → managed VM).
- The target VM has `vm_id` and `pve_node` in inventory. All managed VMs and every scratch host already do; check `inventories/prd/host_vars/<host>.yml` (or `inventories/scratch/host_vars/<host>.yml` for a scratch VM) if a new VM is missing them.
- The PVE host's thin pool has room: `pvesm status` on the VM's `pve_node` shows `local-lvm`'s free space. A grown disk takes thin space only as the guest writes it, but a full thin pool stalls every VM on it.

## Steps

### 1. Bump `size_gb` in Terraform

For the scratch VM, this is `vm_disk_size_gb` in `terraform/scratch/terraform.tfvars`. For production VMs, edit the matching `managed_disks[*].size` entry under the VM's key in `terraform/prd/vms.tf`.

```sh
cd terraform/scratch     # or: cd terraform/prd
terraform plan        # confirm the only change is the disk size you intended
terraform apply
```

For a production resize, narrow the apply to the affected VM with `terraform apply -target='module.vm["<host>"]'`.

PVE resizes the underlying logical volume online — no VM reboot.

### 2. Grow the in-guest filesystem

From the `ansible/` directory:

```sh
poetry run ansible-playbook playbooks/grow-disks.yml --limit <host> --check
poetry run ansible-playbook playbooks/grow-disks.yml --limit <host>
```

The check run prints what `growpart` and `resize2fs` will do without running them. The real run grows the partition and filesystem to match what PVE declared in step 1.

A second real run reports `changed=0` — that's the idempotency proof.

### 3. Verify

```sh
ssh ansible@<host> 'lsblk /dev/sda && df -h <mountpoint>'
```

Disk, partition, and filesystem should all show the new size (modulo the small overhead at the end of disk for the GPT secondary header and the BIOS/EFI/boot helper partitions on cloud-init Ubuntu).

## Multiple disks per VM

VMs in `pve_vms` inherit a root entry from `group_vars/pve_vms.yml`. To extend it to a data disk, set `managed_filesystems_volumes` at the right inventory level (`group_vars/<group>.yml` if every host in the group has it; `host_vars/<host>.yml` otherwise) — overriding replaces the list:

```yaml
# inventories/prd/group_vars/k8s_prd.yml — every k8s_prd node has scsi1 = /var/snap
managed_filesystems_volumes:
  - scsi_index: 0
    mountpoint: /
    fstype: ext4
  - scsi_index: 1
    mountpoint: /var/snap
    fstype: ext4
```

Passthrough disks (Ceph OSD volumes, ZFS-passthrough drives) are deliberately **not** in scope for this role and must not be added to the list — they're owned by their respective stacks (Ceph, ZFS) and have their own resize procedures. A ZFS pool on a virtual disk stays off the list as well: it has [its own section](#growing-a-zfs-pool-on-a-virtual-disk).

## Growing a ZFS pool on a virtual disk

Each pool in a host's `zfs_pools` (zpool1 on srvk8sdev, zpool3–5 on srvk8s2–4) fills a whole Terraform virtual disk. The `zfs` role only creates pools, and `grow-disks.yml` handles ext4 only, so you grow the pool by hand in the guest. The datasets on the pool have quotas set by their deploy repo's Terraform, and growing the pool doesn't raise any of them.

1. **Bump the disk in Terraform**, as in [step 1](#1-bump-size_gb-in-terraform). The slot is the pool's `scsi_index`.
2. **Expand the pool on the guest.** `<N>` is the pool's `scsi_index`:

   ```sh
   d=$(basename "$(readlink -f /dev/disk/by-id/scsi-0QEMU_QEMU_HARDDISK_drive-scsi<N>)")
   echo 1 | sudo tee /sys/block/$d/device/rescan   # the kernel picks up the new size
   lsblk /dev/$d                                   # the disk shows it, part1 still the old size
   sudo zpool online -e <pool> scsi-0QEMU_QEMU_HARDDISK_drive-scsi<N>
   zpool list <pool>                               # SIZE is the new size
   ```

   The pool was created on the whole disk, so ZFS owns its partition table: `online -e` grows partition 1 itself, with no `growpart`. These pools have `autoexpand` off, so the new space stays unused until you run `online -e`.

3. **Raise the quota of the dataset that needs the space.** `zfs get -r quota <pool>` lists the quotas. Each dataset is a `homelab_zfs_dataset` in the Terraform of the deploy repo that uses it. Change the quota there, and the repo's Argo CD sync applies it. For KubeCoder's env storage on zpool5, that is KubeCoderDeploy `config/<stage>/terraform.tfvars`:
   - Raise both `zfs_quota`, the real limit, and `zfs_size`, the PV's capacity, which Terraform updates in place.
   - Leave the chart's `storage.zfs.size` (the PVC's request) alone. The PVC is bound to a fixed PV, and Kubernetes won't resize such a claim.
   - prd's value reaches the cluster only through a promotion. That ships everything else waiting on `main` and restarts every prd env pod ([SOP](https://github.com/pvginkel/SOPs/blob/main/sops/kubecoder-promote-prd.md)).
4. **Verify:** `zfs list -o name,used,avail,quota -r <pool>`.

## Failure modes

- **`terraform plan` proposes more than the disk grow.** Stop and investigate before applying — drift on other fields signals an out-of-band PVE UI edit or a forgotten `lifecycle.ignore_changes` line.
- **Role asserts "Rescan did not pick up the new size."** The kernel didn't see the SCSI capacity change. Reboot the VM (`ssh <host> sudo reboot`), wait for it to come back, re-run the playbook.
- **`zpool list` shows the old size after `zpool online -e`.** `lsblk` shows the old disk size too, which means the rescan didn't land. Reboot the VM and run the ZFS step 2 again.
- **`growpart` fails with anything other than `NOCHANGE`.** Usually means the partition layout is not what the role expects (LVM under the partition, dm-crypt, btrfs subvolumes). The role's contract is "partition is a direct child of the disk and holds an ext4 filesystem"; anything else needs a role extension.
