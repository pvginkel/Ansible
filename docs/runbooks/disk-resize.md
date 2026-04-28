# Growing a managed VM's disk

How to grow the boot (or a data) disk on a Terraform-provisioned, Ansible-managed VM. Replaces the manual three-step "grow in PVE UI → growpart → resize2fs" routine.

The contract is split across the two tools:

- **Terraform** owns the PVE-side disk geometry — bumping `size_gb` is the only place you touch.
- **Ansible** owns the guest-side filesystem — `disk_resize` reads `qm config` from the VM's PVE host and reconciles the partition + filesystem to match.

Disks can only grow. Shrinking requires destroy + recreate of the VM (the rebuild path).

## Prerequisites

- Both SSH identities loaded in `ssh-agent` per [`operator-workstation.md`](operator-workstation.md): `pve-root` (Terraform → PVE) and the `ansible` service key (Ansible → managed VM).
- The target VM has `vm_id` and `pve_node` in inventory. All managed VMs and `wrkscratch` already do; check `inventories/prd/host_vars/<host>.yml` (or `inventories/scratch/host_vars/wrkscratch.yml` for the scratch VM) if a new VM is missing them.

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
poetry run ansible-playbook playbooks/grow-disks.yml --limit <host> --check --diff
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

By default the role only reconciles `/` (backed by `scsi0`). To extend it to a data disk, set `disk_resize_filesystems` in the VM's host_vars:

```yaml
# inventories/prd/host_vars/srvk8sl1.yml
disk_resize_filesystems:
  - mountpoint: /
    scsi_index: 0
  - mountpoint: /var/snap/microk8s/common
    scsi_index: 1
```

Passthrough disks (Ceph OSD volumes, ZFS-passthrough drives) are deliberately **not** in scope for this role and must not be added to the list — they're owned by their respective stacks (Ceph, ZFS) and have their own resize procedures.

## Failure modes

- **`terraform plan` proposes more than the disk grow.** Stop and investigate before applying — drift on other fields signals an out-of-band PVE UI edit or a forgotten `lifecycle.ignore_changes` line.
- **Role asserts "Rescan did not pick up the new size."** The kernel didn't see the SCSI capacity change. Reboot the VM (`ssh <host> sudo reboot`), wait for it to come back, re-run the playbook.
- **`growpart` fails with anything other than `NOCHANGE`.** Usually means the partition layout is not what the role expects (LVM under the partition, dm-crypt, btrfs subvolumes). The role's contract is "partition is a direct child of the disk and holds an ext4 filesystem"; anything else needs a role extension.
