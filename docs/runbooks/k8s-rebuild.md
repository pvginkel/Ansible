# Rebuilding k8s VMs (drain → shutdown → TF → Ansible)

End-to-end procedure for rebuilding `k8s_prd` and `k8s_dev` nodes from the adoption shape onto the from-scratch shape. Folds the Phase 4b parity event per `docs/decisions.md` "Adoption is a waypoint; rebuild is the parity event."

This runbook is the orchestrator. Each step is operator-driven; nothing automates the full sequence. Per `decisions.md` "Terraform and Ansible are peer tools — neither invokes the other."

Assumes `docs/plans/01-pam-credentials.md` has been applied: passthrough disks are TF-managed, not reattached by Ansible.

## Order

1. **Pre-flight on `srvk8ss2`** — cordon + drain + uncordon. No rebuild. Sanity check that the cluster handles a worker-shaped outage cleanly.
2. **`srvk8ss1` → `srvk8s2`** (worker, no passthrough; first real rebuild).
3. **`srvk8ss2` → `srvk8s3`** (worker, no passthrough).
4. **`srvk8sl1` → `srvk8s1`** (primary; NVMe passthrough + ZFS reattach; old VM is destroyed because the new VM needs the NVMe).
5. **`wrkdevk8s`** (greenfield single-node dev — different specifics).

After step 5 the parity event closes. The HelmCharts cluster on `wrkdevk8s` will need to be re-deployed (single-node, fully wiped — no carryover from the old VM).

## Prerequisites — every rebuild

- All nodes `Ready`, no PDB-blocked pods. `microk8s kubectl get nodes -o wide`, `microk8s kubectl get pdb -A`.
- SSH agent loaded with both operator and ansible keys (see `operator-workstation.md`).
- Workstation has a secondary DNS resolver configured (per `decisions.md` "DNS and hostnames"). A node reboot blacks out resolution from the workstation otherwise.
- Maintenance window for prd. `wrkdevk8s` is dev only.
- `git status` clean. `terraform plan` shows only the queued rebuild entries from plan 01 (no unrelated drift).

## Pre-drain hand-off

Each rebuild's eviction (and every `update-k8s.yml` run) hands off opt-in workloads to a healthy peer before `kubectl drain` fires: cordon → `kubectl rollout restart` of any Deployment carrying `iac.webathome.org/pre-drain=true` → wait Ready → drain. The shared task file is `ansible/playbooks/tasks/pre-drain-handoff.yml`; the rebuild flow consumes it through `evict-k8s.yml`.

Today's opt-ins:

- `keycloak` (Deployment, `RollingUpdate maxSurge:1 / maxUnavailable:0`). Two-pod window during surge — ~60s. No sticky sessions on the in-house ingress, so a mid-login request may re-auth. Accepted.
- `keycloak-db` (Deployment, `Recreate`, RWO PVC, single Postgres). ~30s outage during the controlled swap. Better than the same swap landing mid-drain.

Forward contract: a Deployment that needs a controlled hand-off (single replica, RWO PVC, etc.) opts in by setting `iac.webathome.org/pre-drain: "true"` on **both** `metadata.labels` and `spec.template.metadata.labels`. The first lets `kubectl get deploy -l ...` enumerate opt-ins; the second is what the hand-off's pod-list query actually matches on. DaemonSets and StatefulSets must NOT carry the label — the Pod → ReplicaSet → Deployment walk silently ignores them, so labeling them is dormant config.

If a labeled rollout fails to reach Ready inside 5 minutes, the play aborts before draining. Fix the workload, then re-run.

Single-node clusters (today: `wrkdevk8s`) skip the hand-off and the drain together — same peer-count gate.

## Pre-flight — observe an eviction on `srvk8ss2`

Before any rebuild, exercise the eviction path against a live worker so you've seen the cluster's reaction (hand-off + drain) before you also remove the VM.

```sh
poetry run ansible-playbook playbooks/evict-k8s.yml \
    -e evict_target=srvk8ss2

# Observe — pods reschedule onto srvk8sl1 / srvk8ss1, no PDB blocks, system stable.
microk8s kubectl get pods -A -o wide

poetry run ansible -i inventories/prd srvk8sl1 -m command \
    -a 'microk8s kubectl uncordon srvk8ss2' --become
```

If anything misbehaves here (PDB-blocked pod, stuck terminating workload, hand-off rollout that won't reach Ready, anything that doesn't resolve on its own), fix before going further. The rebuilds assume a clean eviction.

## Worker rebuild — `srvk8ss1` → `srvk8s2` and `srvk8ss2` → `srvk8s3`

Same flow for both. The first one through also creates the from-scratch shape's shared TF resources (host keys, cloud-init snippet, image download).

| target    | old hostname | old VMID | new VMID | new MAC              |
|-----------|--------------|----------|----------|----------------------|
| `srvk8s2` | `srvk8ss1`   | 104      | 911      | `02:A7:F3:03:8F:NN`  |
| `srvk8s3` | `srvk8ss2`   | 107      | 912      | `02:A7:F3:03:90:NN`  |

### 1. Evict, leave, remove

```sh
poetry run ansible-playbook playbooks/evict-k8s.yml \
    -e evict_target=<old-hostname>
poetry run ansible -i inventories/prd <old-hostname> -m command \
    -a 'microk8s leave' --become
poetry run ansible -i inventories/prd srvk8sl1 -m command \
    -a 'microk8s remove-node <old-hostname>' --become
```

`evict-k8s.yml` runs the pre-drain hand-off (cordon + rollout restart of opt-in Deployments) and the drain; see "Pre-drain hand-off" above. `microk8s leave` (on the leaving node) plus `microk8s remove-node` (on the primary) is the canonical removal — clears both the node's local cluster state and the dqlite voter list. `kubectl delete node` only removes the kubelet Node object; it leaves dqlite stale.

### 2. Shut down the old VM (don't destroy)

```sh
ssh root@pve qm shutdown <old-vmid>
```

The shut-down VMID stays on PVE as an escape hatch. Note that after `microk8s leave` its on-disk dqlite state is no longer trusted by the cluster — to reuse the old VM in an emergency, `qm start <old-vmid>` + `microk8s reset` + `microk8s join` it back as a fresh member.

### 3. Drop the old hostname from the static-hosts file

The new VM's reservation is registered with the sidecar API by the
`terraform apply` in step 5; nothing to do here for the new entry.
The old hostname still resolves via the HelmCharts static-hosts file
though — remove it (`<old-hostname>` line) in HelmCharts and roll the
dnsmasq StatefulSet so the old entry stops resolving.

```sh
dig +short <old-hostname>.home @<dnsmasq-replica-ip>
# expect: no answer.
```

### 4. Rename the inventory

```sh
cd /work/Ansible
git mv ansible/inventories/prd/host_vars/<old-hostname>.yml \
       ansible/inventories/prd/host_vars/<new-hostname>.yml
```

Edit the renamed `host_vars/<new-hostname>.yml`: bump `vm_id` (104 → 911 or 107 → 912).

Edit `ansible/inventories/prd/hosts.yml`: replace the entry under `k8s_prd.hosts`.

`group_vars/k8s_prd.yml`'s `microk8s_primary_host` stays `srvk8sl1` for both worker rebuilds — it's still alive and serves as the join token mint.

```sh
git add -A
git commit -m "ansible: rename <old-hostname> → <new-hostname> in inventory"
```

### 5. `terraform apply` — create the new VM

First worker rebuild — also pulls in the from-scratch shape's shared resources:

```sh
cd terraform/prd
terraform apply \
    -target='module.vm["<new-hostname>"]' \
    -target=tls_private_key.host_ed25519 \
    -target=local_file.known_hosts_prd \
    -target=proxmox_download_file.ubuntu_cloud_image \
    -target=proxmox_virtual_environment_file.cloud_init
```

`tls_private_key` runs for all four from-scratch VMs at once — the local_file aggregates host keys from all of them; targeting only one leaves the local_file's content unknown at plan time and TF errors. Snippets and image downloads similarly. Subsequent rebuilds reuse these.

Second worker rebuild — those resources already exist:

```sh
terraform apply -target='module.vm["<new-hostname>"]'
```

TF blocks until the new VM's qemu-guest-agent reports its IP back to PVE — typically 1–3 minutes including cloud-init.

### 6. `rebuild-k8s.yml` — bootstrap, baseline, microk8s join

```sh
cd ../../ansible
poetry run ansible-playbook playbooks/rebuild-k8s.yml \
    -e rebuild_target=<new-hostname>
```

Applies bootstrap+baseline+microk8s on the new VM. The microk8s role mints a join token from `srvk8sl1` (still primary) and joins.

### 7. Verify

```sh
poetry run ansible -i inventories/prd srvk8sl1 -m command \
    -a 'microk8s kubectl get nodes -o wide' --become
# expect: srvk8sl1 Ready, <new-hostname> Ready, plus the other worker (old or new)

poetry run ansible-playbook playbooks/site.yml --limit <new-hostname> --check --diff
# expect: changed=0
```

### 8. Drop the old VM from TF state

```sh
cd terraform/prd
terraform state rm 'module.vm["<old-hostname>"].proxmox_virtual_environment_vm.this'
terraform plan
# expect: zero diff for <old-hostname>; only the remaining queued rebuilds
```

The shut-down VMID stays on PVE; TF stops managing it. After both worker rebuilds are validated and you're confident, `qm destroy <old-vmid>` reclaims the disk space (no rush).

## Primary rebuild — `srvk8sl1` → `srvk8s1` (NVMe passthrough)

Done after both workers are healthy on the new shape. The old VM is destroyed (not just shut down) because the new `srvk8s1` needs the NVMe — qemu can't open a device claimed by another running VM.

| target    | old hostname | old VMID | new VMID | new MAC              |
|-----------|--------------|----------|----------|----------------------|
| `srvk8s1` | `srvk8sl1`   | 103      | 910      | `02:A7:F3:03:8E:00`  |

### 1. Flip the cluster primary off `srvk8sl1`

```sh
$EDITOR ansible/inventories/prd/group_vars/k8s_prd.yml
# microk8s_primary_host: srvk8sl1 → microk8s_primary_host: srvk8s2
git add -A
git commit -m "ansible: flip k8s_prd primary off srvk8sl1 ahead of rebuild"
```

`srvk8sl1` is about to disappear; the join-token delegate has to live on a node that won't.

### 2. Evict, leave, remove

```sh
poetry run ansible-playbook playbooks/evict-k8s.yml \
    -e evict_target=srvk8sl1
poetry run ansible -i inventories/prd srvk8sl1 -m command \
    -a 'microk8s leave' --become
poetry run ansible -i inventories/prd srvk8s2 -m command \
    -a 'microk8s remove-node srvk8sl1' --become
```

The hand-off step runs delegated to `srvk8s2` (the new primary, set in step 1) — the join-token / kubectl delegate already lives there. `zpool2`-pinned workloads (storage chart, Prometheus) drain off `srvk8sl1` and have nowhere to go (no other node carries `homelab.local/storage=zpool2`). They stay Pending until step 7 imports the pool on the new node — that's expected storage-path downtime.

### 3. Shut down + destroy the old VM

```sh
ssh root@pve 'qm shutdown 103 ; sleep 5 ; qm destroy 103'
```

This frees the NVMe (qemu releases the device on destroy; the ZFS pool's on-disk metadata stays for re-import). The new VM in step 6 can now claim it.

### 4. Drop `srvk8sl1` from the static-hosts file

`srvk8s1`'s reservation is registered with the sidecar API by the
`terraform apply` in step 6. Remove the `srvk8sl1` line from
HelmCharts' static-hosts and roll the dnsmasq StatefulSet so the old
hostname stops resolving.

### 5. Update the inventory

```sh
git mv ansible/inventories/prd/host_vars/srvk8sl1.yml \
       ansible/inventories/prd/host_vars/srvk8s1.yml
```

Edit `host_vars/srvk8s1.yml`:
- `vm_id`: 103 → 910
- `zpools_to_import: [zpool2]` (read by `rebuild-k8s.yml`'s ZFS import task)

Passthrough is declared in `terraform/prd/vms.tf` (post plan 01) — no `passthrough_disks` in host_vars.

Edit `hosts.yml`: `srvk8sl1` → `srvk8s1` under `k8s_prd.hosts`.

```sh
git add -A
git commit -m "ansible: rename srvk8sl1 → srvk8s1 in inventory"
```

### 6. `terraform apply` — create the new VM with NVMe attached

```sh
cd terraform/prd
terraform apply -target='module.vm["srvk8s1"]'
```

The shared from-scratch resources already exist from the worker rebuilds. TF creates the VM and attaches `/dev/disk/by-id/nvme-Samsung_SSD_980_500GB_S64DNX0RC21332X` at scsi2 in the same apply (the plan 01 payoff).

### 7. `rebuild-k8s.yml` — ZFS import, bootstrap, microk8s join

```sh
cd ../../ansible
poetry run ansible-playbook playbooks/rebuild-k8s.yml \
    -e rebuild_target=srvk8s1
```

Imports `zpool2` (the on-disk metadata is still there, `zpool import` reattaches it), then bootstrap+baseline+microk8s. The cluster-join task mints a token from `srvk8s2` (the current primary).

### 8. Drop the old VM from TF state

```sh
cd ../terraform/prd
terraform state rm 'module.vm["srvk8sl1"].proxmox_virtual_environment_vm.this'
terraform plan
# expect: zero diff
```

VMID 103 is already destroyed; this is just stub cleanup.

### 9. Verify

```sh
poetry run ansible -i inventories/prd k8s_prd -m command \
    -a 'microk8s kubectl get nodes -o wide' --become
# expect: srvk8s1, srvk8s2, srvk8s3 all Ready

poetry run ansible -i inventories/prd srvk8s1 -m command \
    -a 'zpool list -H -o name,health' --become
# expect: zpool2 ONLINE

poetry run ansible-playbook playbooks/site.yml --limit srvk8s1 --check --diff
# expect: changed=0
```

`zpool2`-pinned workloads (storage chart, Prometheus) reschedule onto `srvk8s1` once the import is done.

## Rebuild — `wrkdevk8s` (single-node, greenfield in TF)

Different shape: the live `wrkdevk8s` (VMID 119) is a manual VM never imported into TF state. The rebuild creates VMID 919 from scratch via TF; we destroy VMID 119 manually.

The dev cluster is a single node and is its own primary — there's no drain or hand-off (nothing to drain to). The peer-count gate skips the whole eviction shape, so `evict-k8s.yml` is not part of this flow. The cluster gets fully replaced; HelmCharts deployments under `wrkdevk8s` need to be re-deployed afterward (operator workflow, separate from this runbook).

```sh
# 1. Drop wrkdevk8s from the HelmCharts static-hosts file (and roll the
#    dnsmasq StatefulSet). The new reservation is registered with the
#    sidecar API by the terraform apply in step 4.

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

# Drop them from ansible.cfg's UserKnownHostsFile too.
$EDITOR ansible/ansible.cfg

git add -A
git commit -m "ansible: retire adoption known_hosts files (k8s_prd, k8s_dev)"
```

Once the worker rebuilds are stable long-term, `qm destroy 104` and `qm destroy 107` to reclaim the shut-down workers' disk space.

Phase 4b closes. Update `docs/plan.md` and `docs/phases/phase-4b-microk8s-rebuild.md`'s status.

## If a rebuild goes sideways

There's no "uncordon to roll back" — by the time `terraform apply` runs the old VM is shut down (workers) or destroyed (`srvk8s1`). Recovery is forward.

- **TF errors at create:** fix the TF error, retry the targeted apply. For workers, the old shut-down VM is still on PVE; in a real emergency `qm start <old-vmid>` + `microk8s reset` + `microk8s join` it as a fresh worker. For `srvk8s1`, the old VM is gone — fix forward only.
- **Cloud-init never finishes (TF times out waiting for IP):** `qm console <new-vmid>` on PVE to inspect; usually the snippet's `qemu-guest-agent` install failed. Fix and `terraform apply -replace='module.vm["<new-hostname>"].proxmox_virtual_environment_vm.this'`.
- **`rebuild-k8s.yml` fails on cluster-join:** the new VM exists but isn't in the cluster. `microk8s status` on the rebuild target tells you what's happening. Common cause: stale `/var/snap/microk8s/common/var/lib/dqlite` from a half-completed prior attempt. `microk8s reset` on the new node, re-run the playbook.
- **`zpool import zpool2` fails on `srvk8s1`:** ZFS sees the pool was last mounted on a different host (the destroyed VM's hostid). `zpool import -f zpool2` is the override. If this hits, fold the `-f` into `rebuild-k8s.yml`.

Verify `site.yml --check --diff` reports zero changes against the rebuild target before declaring success.

## What this runbook does not cover

- Microceph rebuilds (Phase 5).
- HelmCharts redeploy on `wrkdevk8s` after rebuild — operator workflow, see HelmCharts repo.
- Recovering from a corrupted dqlite database — `microk8s reset` is the reset hammer; deeper recovery is per microk8s upstream docs.
