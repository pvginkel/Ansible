# Rebuilding a Terraform-managed VM

How to destroy and recreate a managed VM end-to-end. Rebuild is the canonical path for upgrading the OS, recovering from drift that adoption can't reach, and (eventually) for routine cluster upgrades. Per `docs/decisions.md` "Adoption is a waypoint; rebuild is the parity event," every adopted VM has a planned rebuild that ends the transition.

What persists across rebuild:

- VMID, name, MAC, SSH host key (Terraform-generated `tls_private_key` survives in tfstate; cloud-init re-embeds it).
- Disk **identities** — the LVM-thin volumes (`vm-<vmid>-disk-N`) survive when a VM is destroyed via the bpg provider only if they're declared with `import_from` or attached as `path_in_datastore`; **a plain `disk { datastore_id = "local-lvm", size = N }` is reformatted on recreate**. For the production VMs in `terraform/prd/`, the boot and data disks are reformatted on rebuild; their content lives in Ansible roles + Ceph/ZFS, not in PVE storage volumes that need to survive.
- Inventory (`host_vars/<host>.yml`) — never touched by rebuild.

What does **not** persist:

- Anything written to the rootfs by hand outside the role definitions. If it isn't in the role, it dies at rebuild — that's the point.

## When to rebuild

| Scenario | Action |
|---|---|
| `wrkscratch` after a role change | Rebuild on demand; that's its job. |
| k8s node | Phase 4 — drain → rebuild → uncordon, `serial: 1`. |
| Ceph node | Phase 5 — `noout` → drain → rebuild → reattach OSDs, `serial: 1`. |
| `wrkdev` | Operator-scheduled. |
| pve hosts | Never. Bare metal, fidelity-only. |

`pve` hosts have no scheduled rebuild; their drift is reconciled in place against `proxmox_host`. Anything below assumes a guest VM, not a PVE node.

## Rebuild flow — `wrkscratch` (the validated path)

The simplest case. No cluster impact. Use this exercise to sanity-check role changes before pointing them at a real cluster member.

### 1. Baseline check before destroying anything

```sh
cd terraform/scratch
terraform plan -detailed-exitcode ; echo "exit=$?"        # expect 0
```

```sh
cd ../../ansible
poetry run ansible-playbook playbooks/site.yml \
    -i inventories/scratch --limit wrkscratch --check --diff
# expect: changed=0, failed=0
```

If the baseline isn't clean, fix what surfaces before rebuilding — otherwise the post-rebuild verification can't tell role drift from rebuild drift.

### 2. Rebuild via Terraform

```sh
cd ../terraform/scratch
terraform apply -replace='proxmox_virtual_environment_vm.scratch'
```

`-replace` destroys and recreates only the VM resource. The Ubuntu cloud image (`proxmox_download_file`), cloud-init snippet, and `tls_private_key` are independent resources — they aren't touched, so the new VM boots with the same MAC, same SSH host key, and the same `ansible/files/known_hosts.d/scratch` entry stays valid.

Terraform blocks until `qemu-guest-agent` reports an IP back to PVE — typically 1–3 minutes including cloud-init.

### 3. Apply roles to the fresh VM

```sh
cd ../../ansible
poetry run ansible-playbook playbooks/site.yml \
    -i inventories/scratch --limit wrkscratch
```

First run on a fresh VM has `changed > 0` — that's the role landing. Watch for failures.

### 4. Verify zero residual

```sh
poetry run ansible-playbook playbooks/site.yml \
    -i inventories/scratch --limit wrkscratch --check --diff
# expect: changed=0
```

Non-zero `changed` here means either a baseline imperfection in a role (idempotency bug — fix the role) or an artefact of the rebuild flow itself. Either way, fix before declaring the rebuild successful.

## Rebuild flow — k8s and Ceph cluster members

The current `terraform/prd/` root is the **adoption** shape: it models live VMs but lacks the from-scratch assets (cloud-init, `tls_private_key`, `local_file` for known_hosts.d) that `terraform/scratch/` carries. **A rebuild requires extending the configuration to the from-scratch shape first** — that's a deliberate commit, not a transparent operation.

The full procedure lands when Phase 4 (k8s) and Phase 5 (Ceph) need it. Outline so the constraints are visible now:

1. **Pre-rebuild drain.** Cordon + drain (k8s) or `ceph osd set noout` and stop the OSD/mon (Ceph). Owned by Ansible; Phase 4/5 builds the playbook.
2. **Configuration update commit.** The VM's entry in `terraform/prd/vms.tf` and the supporting per-VM resources switch from "model what's there" to the from-scratch shape:
   - Add `proxmox_download_file`, `proxmox_virtual_environment_file` (cloud-init snippet), `tls_private_key`, `local_file` (known_hosts.d/<host> entry).
   - Add `initialization { user_data_file_id = ... }` to the VM resource.
   - Switch from BC:24:11:... MAC to deterministic `02:A7:F3:VV:VV:EE` (decisions.md "MAC addressing"). VMID likely also moves into the 900-and-up range; if so, the deterministic MAC moves accordingly.
   - **Remove the `passthrough_disks` input** if any. Per `decisions.md` "Disk passthrough on managed VMs," PVE rejects API tokens for filesystem-path operations — a recreate would fail. Ansible re-attaches passthroughs after the VM is up.
3. **dnsmasq reservation update.** New MAC → new reservation (or new IP allocation). Must land before `terraform apply` so the first DHCP lease on the rebuilt VM lands correctly.
4. **`terraform apply -replace`** on the VM resource. Same as wrkscratch.
5. **Reattach passthroughs** via Ansible (root over SSH on the PVE host, `qm set <vmid> --scsiN /dev/disk/by-id/...,backup=0`). Phase 4/5 builds this into the role.
6. **`site.yml`** — bootstrap + baseline + microk8s/microceph role lands the cluster bits.
7. **Re-join the cluster.** k8s: uncordon. Ceph: `noout` lifted, OSDs come back, wait for `HEALTH_OK`.
8. **Verify zero residual** with `--check --diff` against the rebuilt host.

Phase 4 / 5 will produce concrete playbooks for steps 1, 5, 7. Until then, this section is forward-looking — don't try to rebuild a k8s or Ceph node by hand without the playbook backing.

## Disk passthrough — replacing a failing OSD disk

When a Ceph OSD's underlying SSD fails and is swapped for a new physical disk, the `/dev/disk/by-id/<serial>` path changes. Procedure (operator runs as root on the PVE host that owns the VM):

```sh
# 1. Identify the new disk's by-id path
ls /dev/disk/by-id/                          # find the new ata-Samsung_..._<newserial>

# 2. Detach the old, attach the new — VM running, hot-swap
qm set <vmid> --delete scsi2                 # detach old passthrough
qm set <vmid> --scsi2 /dev/disk/by-id/<newpath>,backup=0,size=<bytes>K
```

Capacity in `size=` matches what `qm config <vmid>` shows on the surviving Ceph nodes (the Samsung 870 EVO 2TB drives report `1953514584K`).

After PVE has the new path:

```sh
# 3. Update the VM's entry in terraform/prd/vms.tf
$EDITOR terraform/prd/vms.tf
# change passthrough_disks[*].path_in_datastore to the new path under the VM's key

# 4. Catch tfstate up to PVE
cd terraform/prd
terraform refresh -target='module.vm["<host>"]'
terraform plan -target='module.vm["<host>"]'  # expect zero diff
```

`terraform refresh` reads PVE current state into tfstate without modifying anything. After it runs, both state and module declare the new path.

The Ceph side (re-adding the OSD on top of the new disk, balancing) is owned by the microceph role — operator workflow today, role-driven once Phase 5 lands.

## If a rebuild goes sideways

`terraform apply -replace` is destructive once Terraform commits to the destroy phase. If it fails after destroy and before create, the VM is gone but state may be partially written.

- **TF errors before destroy:** safe — no live impact, fix the error and retry.
- **TF errors during create:** state may have a tainted resource. `terraform plan` will surface a `-/+` (replace) on the next run. Fix the underlying cause (image download, network, snippet upload) and `terraform apply` again.
- **VM created but cloud-init never finished:** Terraform times out waiting for the IP. SSH to the PVE host and `qm console <vmid>` or check `journalctl -u cloud-init` on the VM. Usually `qemu-guest-agent` failing to install — fix the snippet, `terraform apply -replace` again.
- **`site.yml` fails on the rebuilt VM:** the VM exists, just isn't fully roled. Fix the role and re-run; cloud-init has done its part (`ansible` user + host key) and bootstrap can re-run idempotently.

For cluster members, leave the node cordoned/drained until `site.yml --check --diff` reports zero changes. Only then bring it back to the cluster.
