# Rebuilding a k8s VM (drain → TF → Ansible)

End-to-end procedure for rebuilding one k8s node from the adoption shape onto the from-scratch shape. Folds the Phase 4b parity event for `k8s_prd` and `k8s_dev` per `docs/decisions.md` "Adoption is a waypoint; rebuild is the parity event."

This runbook is the orchestrator. Each step is operator-driven; nothing automates the full sequence. Per `decisions.md` "Terraform and Ansible are peer tools — neither invokes the other."

## Order

Do them in this order. Each sub-procedure is below.

1. **Smoke `rebuild-k8s.yml` against scratch.** Before the first prd rebuild only.
2. **`srvk8s1`** first (the trickiest — NVMe passthrough + ZFS reattach; tackle it on the cluster's primary so we know how to rescue if anything wedges).
3. **`srvk8s2`**.
4. **`srvk8s3`** (also resolves the seabios → ovmf flip).
5. **`wrkdevk8s`** (greenfield in TF; single-node — different specifics).

After step 5, the parity event closes. The HelmCharts cluster on `wrkdevk8s` will need to be re-deployed (single-node, fully wiped — no carryover from the old VM).

## Prerequisites — every rebuild

- All nodes `Ready`, no PDB-blocked pods. Run `microk8s kubectl get nodes -o wide` and `microk8s kubectl get pdb -A` first.
- SSH agent loaded with both operator and ansible keys (see `operator-workstation.md`).
- Workstation has a secondary DNS resolver configured (per `decisions.md` "DNS and hostnames"). A node reboot during the rebuild blacks out resolution from the workstation otherwise.
- Maintenance window for prd. Single-node `wrkdevk8s` cluster is dev only.
- Branch is clean: `git status` shows nothing pending, `terraform plan` is empty (any unrelated drift would surface alongside the rebuild's diff and is hard to read).

## Smoke against scratch (one-time, before first prd rebuild)

`rebuild-k8s.yml` is exercised cleanly the first time by destroying and rebuilding `wrkscratchk8s2` while `wrkscratchk8s1` stays Ready.

```sh
# 1. Confirm both scratch nodes are Ready
poetry run ansible -i inventories/scratch k8s_scratch -m command \
    -a 'microk8s status --wait-ready --timeout 60' --become

# 2. Drain wrkscratchk8s2 from the scratch cluster (delegated to wrkscratchk8s1)
poetry run ansible -i inventories/scratch wrkscratchk8s1 -m command \
    -a 'microk8s kubectl drain wrkscratchk8s2 --ignore-daemonsets --delete-emptydir-data --timeout=300s' \
    --become

# 3. Replace the VM
cd terraform/scratch
terraform apply -replace='proxmox_virtual_environment_vm.scratch["wrkscratchk8s2"]'

# 4. Run rebuild-k8s.yml against the rebuilt scratch node
cd ../../ansible
poetry run ansible-playbook playbooks/rebuild-k8s.yml \
    -i inventories/scratch -e rebuild_target=wrkscratchk8s2

# 5. Remove the stale node entry from the scratch cluster
poetry run ansible -i inventories/scratch wrkscratchk8s1 -m command \
    -a 'microk8s kubectl delete node wrkscratchk8s2 --ignore-not-found' --become
# (re-add happens as the rebuilt node joins through the role's cluster-join task)

# 6. Verify zero residual
poetry run ansible-playbook playbooks/site.yml \
    -i inventories/scratch --limit wrkscratchk8s2 --check --diff
# expect changed=0
```

If any step fails, fix before pointing the playbook at prd. **Do not** smoke against `srvk8sl1` directly.

## Rebuild — `srvk8s1` (NVMe passthrough)

The hardest path; everything else is a subset of this.

### 1. Drain the old node from the cluster

```sh
poetry run ansible -i inventories/prd srvk8sl1 -m command \
    -a 'microk8s kubectl drain srvk8sl1 --ignore-daemonsets --delete-emptydir-data --timeout=300s' \
    --become
```

Drain runs on `srvk8sl1` itself; that's still valid because the node is alive. If it hangs on a PodDisruptionBudget, see [`k8s-upgrade.md`](k8s-upgrade.md) "Drain blocked by a PodDisruptionBudget."

### 2. Update dnsmasq reservation

The dnsmasq pod on the prd k8s cluster carries the IP reservations. Edit the reservation set in HelmCharts (or wherever the operator manages it):

- Remove `srvk8sl1` (old) — MAC `BC:24:11:3D:56:09`.
- Add `srvk8s1` (new) — MAC `02:A7:F3:03:8E:00`. Allocate a new IPv4 reservation.

Roll the dnsmasq StatefulSet so the change is picked up. Verify with:

```sh
dig +short srvk8s1.home @<dnsmasq-replica-ip>
```

`srvk8sl1.home` should stop resolving; `srvk8s1.home` should resolve. (The live VM's lease on the old IP persists until the VM is destroyed, so traffic to the old IP still works for now.)

### 3. Rename the inventory

```sh
cd /work/Ansible
git mv ansible/inventories/prd/host_vars/srvk8sl1.yml ansible/inventories/prd/host_vars/srvk8s1.yml
```

Edit `ansible/inventories/prd/host_vars/srvk8s1.yml`:

- Update `vm_id` from 103 to 910.
- Add the passthrough disk declaration (read by `proxmox_host` at apply time):
  ```yaml
  passthrough_disks:
    - interface: scsi2
      path_in_datastore: /dev/disk/by-id/nvme-Samsung_SSD_980_500GB_S64DNX0RC21332X
  ```
- Add the ZFS pools to import on first boot (read by `rebuild-k8s.yml`):
  ```yaml
  zpools_to_import:
    - zpool2
  ```

Edit `ansible/inventories/prd/hosts.yml`: replace `srvk8sl1:` with `srvk8s1:` under `k8s_prd.hosts`.

Edit `ansible/inventories/prd/group_vars/k8s_prd.yml`: change `microk8s_primary_host: srvk8sl1` to `microk8s_primary_host: srvk8s1`.

```sh
git add -A
git commit -m "ansible: rename srvk8sl1 → srvk8s1 in inventory"
```

### 4. `terraform apply` — create the new VM

The new VM's resources (image download on `pve`, host key, cloud-init snippet, the new VM itself) are not yet in state. Other rebuild commits (srvk8s2/3/wrkdevk8s) are pending in `vms.tf` but should not apply yet. Targeted apply:

```sh
cd terraform/prd
terraform plan \
    -target='module.vm["srvk8s1"]' \
    -target=tls_private_key.host_ed25519 \
    -target=local_file.known_hosts_prd \
    -target=proxmox_download_file.ubuntu_cloud_image \
    -target=proxmox_virtual_environment_file.cloud_init

terraform apply \
    -target='module.vm["srvk8s1"]' \
    -target=tls_private_key.host_ed25519 \
    -target=local_file.known_hosts_prd \
    -target=proxmox_download_file.ubuntu_cloud_image \
    -target=proxmox_virtual_environment_file.cloud_init
```

`tls_private_key` runs for all four from-scratch VMs at once (the local_file aggregates host keys from all of them; targeting only one leaves the local_file's content unknown at plan time and TF errors). Snippets and image downloads similarly. Subsequent rebuilds reuse the keys + snippets created here.

The old VM (`srvk8sl1`, VMID 103) stays running and untouched by this apply — it's an orphan in TF state. We deal with it after the new node is in the cluster.

TF blocks until the new VM's qemu-guest-agent reports its IP back to PVE — typically 1–3 minutes including cloud-init.

### 5. Reattach the NVMe passthrough + apply roles

```sh
cd ../../ansible
poetry run ansible-playbook playbooks/rebuild-k8s.yml \
    -e rebuild_target=srvk8s1
```

Per the playbook header — applies bootstrap+baseline+microk8s on `srvk8s1`, runs `proxmox_host` on `pve` so the passthrough task attaches `nvme-Samsung_SSD_980_500GB_S64DNX0RC21332X` at scsi2 (`qm set --scsi2 ...,backup=0`), imports `zpool2` on the rebuilt node, then waits for `microk8s status --wait-ready`.

If the role's microk8s install fails to join the cluster: the cluster primary in `group_vars/k8s_prd.yml` is now `srvk8s1` (the new node, which is what's bootstrapping). On a fresh single-node start this works. The remaining nodes (`srvk8ss1`, `srvk8ss2`) still appear under their old names in the cluster but are alive and Ready — `srvk8s1` joins them via cluster-join.

### 6. Remove the stale node from the cluster

```sh
poetry run ansible -i inventories/prd srvk8s1 -m command \
    -a 'microk8s kubectl delete node srvk8sl1 --ignore-not-found' --become
```

Run from the rebuilt `srvk8s1` (the new primary). The cluster now has 3 nodes: `srvk8s1` (rebuilt), `srvk8ss1`, `srvk8ss2`.

### 7. Destroy the old VM and clean up TF state

```sh
ssh root@pve 'qm shutdown 103 ; sleep 5 ; qm destroy 103'

cd ../terraform/prd
terraform state rm 'module.vm["srvk8sl1"].proxmox_virtual_environment_vm.this'
terraform plan   # expect: zero diff for srvk8sl1; pending diffs for srvk8s2/3/wrkdevk8s only
```

VMID 103 is now free. The old known_hosts.d entry for `srvk8sl1` in `files/known_hosts.d/k8s_prd` lingers; cleaned up at end-of-phase (see "Closing the parity event" below).

### 8. Verify

```sh
poetry run ansible -i inventories/prd k8s_prd -m command \
    -a 'microk8s kubectl get nodes -o wide' --become
# expect: srvk8s1 Ready, srvk8ss1 Ready, srvk8ss2 Ready

poetry run ansible-playbook playbooks/site.yml --limit srvk8s1 --check --diff
# expect: changed=0

poetry run ansible -i inventories/prd srvk8s1 -m command \
    -a 'zpool list -H -o name,health' --become
# expect: zpool2 ONLINE
```

`srvk8s1` is done. The HelmCharts workloads pinned to `homelab.local/storage=zpool2` (storage chart, Prometheus) reschedule onto it.

## Rebuild — `srvk8s2` and `srvk8s3`

Same flow as srvk8s1, **minus the passthrough and zpool steps** (no NVMe; `host_vars/srvk8s2.yml` and `srvk8s3.yml` carry no `passthrough_disks` or `zpools_to_import`). Specifics that differ:

- Old → new: `srvk8ss1` → `srvk8s2` (VMID 104 → 911, MAC `02:A7:F3:03:8F:NN`); `srvk8ss2` → `srvk8s3` (VMID 107 → 912, MAC `02:A7:F3:03:90:NN`).
- `srvk8ss2`'s rebuild also flips bios from `seabios` to `ovmf` — folded into the from-scratch shape, no manual step.
- `microk8s_primary_host` in `group_vars/k8s_prd.yml` stays `srvk8s1` for these rebuilds (don't touch).
- `terraform apply` step uses fewer `-target`s (the keys, snippets, image downloads, and `local_file` already exist from `srvk8s1`'s apply):
  ```sh
  terraform apply -target='module.vm["srvk8s2"]'
  ```
- After both, re-run `--check --diff` against the cluster:
  ```sh
  poetry run ansible-playbook playbooks/site.yml --limit k8s_prd --check --diff
  # expect changed=0
  ```

## Rebuild — `wrkdevk8s` (single-node, greenfield in TF)

Different shape: the live `wrkdevk8s` (VMID 119) is a manual VM never imported into TF state. The rebuild creates VMID 919 from scratch via TF, then we destroy VMID 119 manually.

The dev cluster is a single node and is its own primary — there's no drain (nothing to drain to). The cluster gets fully replaced; HelmCharts deployments under `wrkdevk8s` need to be re-deployed afterward (operator workflow, separate from this runbook).

```sh
# 1. Update dnsmasq:
#    Remove wrkdevk8s old MAC BC:24:11:F0:36:14
#    Add    wrkdevk8s new MAC 02:A7:F3:03:97:00, new IPv4

# 2. Update inventory: drop the microk8s_channel override from
#    host_vars/wrkdevk8s.yml so group_vars/k8s_dev.yml's 1.32/stable
#    takes over, and bump vm_id from 119 to 919.
#    No rename of the host_vars file (hostname stays wrkdevk8s).
git add -A
git commit -m "ansible: wrkdevk8s — drop 1.30 channel override, vm_id 119 → 919"

# 3. Manually destroy the live VM:
ssh root@pve 'qm shutdown 119 ; sleep 5 ; qm destroy 119'

# 4. terraform apply:
cd terraform/prd
terraform apply -target='module.vm["wrkdevk8s"]'

# 5. Apply roles:
cd ../../ansible
poetry run ansible-playbook playbooks/rebuild-k8s.yml \
    -e rebuild_target=wrkdevk8s

# 6. Verify:
poetry run ansible -i inventories/prd wrkdevk8s -m command \
    -a 'microk8s kubectl get nodes' --become
# expect: wrkdevk8s Ready (1.32.x)
```

The HelmCharts dev deployments are gone with the old VM. Re-deploy them via the HelmCharts repo's normal `configs/dev` flow.

## Closing the parity event

After all four rebuilds:

```sh
# Retire the adoption known_hosts files — every k8s node now has its
# host key in files/known_hosts.d/prd (TF-owned).
git rm ansible/files/known_hosts.d/k8s_prd ansible/files/known_hosts.d/k8s_dev

# Drop them from ansible.cfg's UserKnownHostsFile too:
$EDITOR ansible/ansible.cfg
# Remove "files/known_hosts.d/k8s_prd" and "files/known_hosts.d/k8s_dev"
# from the ssh_args UserKnownHostsFile list.

git add -A
git commit -m "ansible: retire adoption known_hosts files (k8s_prd, k8s_dev)"
```

Phase 4b closes. Update `docs/plan.md` and `docs/phases/phase-4b-microk8s-rebuild.md`'s status.

## If a rebuild goes sideways

`terraform apply -replace` is destructive once TF commits to the destroy phase, but for these rebuilds we use targeted creates (the old VM is still alive until step 7) — that gives a wider rollback window:

- **TF errors at create:** the old VM is still alive and cordoned. Drain is the only mutation so far. Fix the TF error, retry apply. If you need to undo: `microk8s kubectl uncordon srvk8sl1` to bring the old node back.
- **Cloud-init never finishes (TF times out waiting for IP):** `qm console <new-vmid>` on PVE to inspect; usually the snippet's `qemu-guest-agent` install failed. Fix and `terraform apply -replace='module.vm["srvk8s1"].proxmox_virtual_environment_vm.this'`.
- **`rebuild-k8s.yml` fails on the cluster-join step:** the new VM exists but isn't in the cluster. `microk8s status` on the rebuild target tells you what's happening. Common cause: stale `/var/snap/microk8s/common/var/lib/dqlite` from a half-completed prior attempt. `microk8s reset` on the new node, then re-run the playbook.
- **PVE rejects the passthrough attach (`qm set` fails):** the disk-by-id path is wrong or the disk is still claimed by the destroyed VM. `lsblk` and `ls /dev/disk/by-id/` on the PVE node to check; correct `passthrough_disks` in `host_vars/srvk8s1.yml` and re-run the playbook.

Leave the rebuild target cordoned/drained until `site.yml --check --diff` reports zero changes. Only then declare the rebuild successful.

## What this runbook does not cover

- Microceph rebuilds (Phase 5).
- HelmCharts redeploy on `wrkdevk8s` after rebuild — operator workflow, see HelmCharts repo.
- Recovering from a corrupted dqlite database — `microk8s reset` is the reset hammer; deeper recovery is per microk8s upstream docs.
