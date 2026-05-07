# Rebuilding k8s VMs (drain → shutdown → TF → Ansible)

End-to-end procedure for rebuilding `k8s_prd` and `k8s_dev` nodes from scratch. The phase-4 parity event that retired the adoption shape across the fleet closed in phase 4d (2026-05-07); the same flow applies to any future single-node rebuild (disk failure, hardware migration, etc.).

This runbook is the orchestrator. Each step is operator-driven; nothing automates the full sequence. Per `decisions.md` "Terraform and Ansible are peer tools — neither invokes the other."

**Networking shape**: prd k8s + Ceph nodes are bring-up tier — static IPs in `vms.tf`, hostname → IP triples curated by hand in HelmCharts `configs/prd/dnsmasq.yaml`. They opt out of the dynamic `homelab_dns_reservation` API via `static_ip = true` on the `managed-vm` module, because the dnsmasq sidecar runs in-cluster and the cluster nodes can't get their own DNS from a service they're required to bring up. wrkdevk8s is dev-tier — single node, no in-cluster registry/dnsmasq dependency, dynamic reservation via the standard module path. See `decisions.md` "Ceph nodes and prd k8s nodes are static infrastructure".

## Order

For a multi-node rebuild (e.g. recovering after a host failure that takes more than one VM with it, or a future doctrine pivot that touches every k8s node):

1. **Pre-flight** — cordon + drain + uncordon a single worker. No rebuild. Sanity check that the cluster handles a worker-shaped outage cleanly.
2. **Workers first**, one at a time, leaving the primary intact through both worker rebuilds (the surviving primary mints join tokens for the new workers).
3. **Primary last** — workers serve as the join-token mint, which is also where the role's runtime election lands the new primary.
4. **wrkdevk8s** (single-node dev) is independent — rebuild any time.

For a single-node rebuild (the common steady-state case), drop the pre-flight step and run the matching subsection directly.

## Prerequisites — every rebuild

- All nodes `Ready`, no PDB-blocked pods. `microk8s kubectl get nodes -o wide`, `microk8s kubectl get pdb -A`.
- SSH agent loaded with both operator and ansible keys (see `operator-workstation.md`).
- Workstation has a secondary DNS resolver configured (per `decisions.md` "DNS and hostnames"). A node reboot blacks out resolution from the workstation otherwise.
- Maintenance window for prd. `wrkdevk8s` is dev only.
- `git status` clean. `terraform plan` shows only the queued rebuild entries (no unrelated drift). If a previous half-rebuild left an orphan `module.vm["<old-hostname>"]` in state, `terraform state rm 'module.vm["<old-hostname>"].proxmox_virtual_environment_vm.this'` first — `for_each` orphan reconciliation isn't suppressed by `-target`, and a leftover orphan otherwise queues a destroy alongside the targeted apply.

## Pre-drain hand-off

Each rebuild's eviction (and every `update-k8s.yml` run) hands off opt-in workloads to a healthy peer before `kubectl drain` fires: cordon → `kubectl rollout restart` of any Deployment carrying `iac.webathome.org/pre-drain=true` → wait Ready → drain. The shared task file is `ansible/playbooks/tasks/pre-drain-handoff.yml`; the rebuild flow consumes it through `evict-k8s.yml`.

Today's opt-ins:

- `keycloak` (Deployment, `RollingUpdate maxSurge:1 / maxUnavailable:0`). Two-pod window during surge — ~60s. No sticky sessions on the in-house ingress, so a mid-login request may re-auth. Accepted.
- `keycloak-db` (Deployment, `Recreate`, RWO PVC, single Postgres). ~30s outage during the controlled swap. Better than the same swap landing mid-drain.

Forward contract: a Deployment that needs a controlled hand-off (single replica, RWO PVC, etc.) opts in by setting `iac.webathome.org/pre-drain: "true"` on **both** `metadata.labels` and `spec.template.metadata.labels`. The first lets `kubectl get deploy -l ...` enumerate opt-ins; the second is what the hand-off's pod-list query actually matches on. DaemonSets and StatefulSets must NOT carry the label — the Pod → ReplicaSet → Deployment walk silently ignores them, so labeling them is dormant config.

If a labeled rollout fails to reach Ready inside 5 minutes, the play aborts before draining. Fix the workload, then re-run.

Single-node clusters (today: `wrkdevk8s`) skip the hand-off and the drain together — same peer-count gate.

## Pre-flight — observe an eviction on a worker

Before a multi-node rebuild, exercise the eviction path against a live worker so you've seen the cluster's reaction (hand-off + drain) before you also remove the VM. Skip for a single-node rebuild — the eviction path is the same one `rebuild-k8s.yml` will exercise next anyway.

```sh
poetry run ansible-playbook playbooks/evict-k8s.yml \
    -e evict_target=<worker>

# Observe — pods reschedule onto the surviving workers, no PDB blocks, system stable.
microk8s kubectl get pods -A -o wide

poetry run ansible -i inventories/prd <other-survivor> -m command \
    -a 'microk8s kubectl uncordon <worker>' --become
```

If anything misbehaves here (PDB-blocked pod, stuck terminating workload, hand-off rollout that won't reach Ready, anything that doesn't resolve on its own), fix before going further. The rebuilds assume a clean eviction.

## Worker rebuild — `srvk8s2` and `srvk8s3`

Same flow for both. Use this when a worker needs to be rebuilt (disk failure, doctrine change, etc.).

| target    | VMID | MAC                  |
|-----------|------|----------------------|
| `srvk8s2` | 911  | `02:A7:F3:03:8F:NN`  |
| `srvk8s3` | 912  | `02:A7:F3:03:90:NN`  |

### 1. Evict, leave, remove

```sh
poetry run ansible-playbook playbooks/evict-k8s.yml \
    -e evict_target=<target>
poetry run ansible -i inventories/prd <target> -m command \
    -a 'microk8s leave' --become
poetry run ansible -i inventories/prd <other-survivor> -m command \
    -a 'microk8s remove-node <target>' --become
```

`evict-k8s.yml` runs the pre-drain hand-off (cordon + rollout restart of opt-in Deployments) and the drain; see "Pre-drain hand-off" above. `microk8s leave` (on the leaving node) plus `microk8s remove-node` (delegated to a survivor — runtime election picks one) is the canonical removal: clears both the node's local cluster state and the dqlite voter list. `kubectl delete node` only removes the kubelet Node object; it leaves dqlite stale.

### 2. Shut down the old VM (don't destroy)

```sh
ssh root@<pve-node> qm shutdown <vmid>
```

The shut-down VMID stays on PVE as an escape hatch. After `microk8s leave` its on-disk dqlite state is no longer trusted by the cluster — to reuse the old VM in an emergency, `qm start <vmid>` + `microk8s reset` + `microk8s join` it back as a fresh member.

### 3. (Static-hosts is hand-curated; no edit unless the IP changes)

The target's entry in HelmCharts `configs/prd/dnsmasq.yaml` is operator-curated alongside Ceph + printers + IoT. As long as the rebuild keeps the same hostname → IP mapping (the typical case), no change is needed. If the IP changes, edit there and roll the dnsmasq StatefulSet before step 4.

### 4. `terraform apply` — create the new VM

```sh
cd terraform/prd
terraform apply \
    -replace='proxmox_virtual_environment_file.cloud_init["<target>"]' \
    -replace='module.vm["<target>"].proxmox_virtual_environment_vm.this' \
    -target='module.vm["<target>"]' \
    -target=proxmox_virtual_environment_file.cloud_init
```

The `-replace` on `proxmox_virtual_environment_file.cloud_init["<target>"]` forces a fresh snippet (deterministic content, but explicit re-render keeps cloud-init first-boot semantics intact). The `-replace` on the VM resource destroys + recreates it from the cloud image. Drop the `-replace` flags for a greenfield create where the resource doesn't exist in state yet.

Shared resources (`tls_private_key.host_ed25519`, `local_file.known_hosts_prd`, `proxmox_download_file.ubuntu_cloud_image`) already exist from the original phase-4 rebuilds and are reused; if you somehow have a fresh state without them, add `-target=tls_private_key.host_ed25519 -target=local_file.known_hosts_prd -target=proxmox_download_file.ubuntu_cloud_image`. `tls_private_key` runs for all from-scratch VMs at once — the `local_file` aggregates host keys from all of them; targeting only one leaves the file content unknown at plan time and TF errors.

TF blocks until the new VM's qemu-guest-agent reports its IP back to PVE — typically 1–3 minutes including cloud-init.

### 5. `rebuild-k8s.yml` — bootstrap, baseline, managed_filesystems, microk8s join

```sh
cd ../../ansible
poetry run ansible-playbook playbooks/rebuild-k8s.yml \
    -e rebuild_target=<target>
```

Applies bootstrap → baseline → managed_filesystems → microk8s on the new VM. `managed_filesystems` partitions, formats, and mounts the scsi1 = 80 GB data volume at `/var/snap` *before* microk8s installs — without that the snap state piles up on the 19 GB root and kubelet's image GC fails. The microk8s role then mints a join token from whichever surviving node the runtime election picks and joins.

After the join, the playbook cordons the new node, waits for every pod scheduled on it (DaemonSets — Ceph CSI, Calico, MetalLB speaker, Prometheus node-exporter, SMB CSI — plus any workload pod that landed in the brief race window) to reach Ready, then uncordons. Ceph CSI is the slow one (~3 minutes); without this gate, ordinary workloads land on the new node before the CSI is up and fail to mount their PVs. If the wait times out, the node stays cordoned — triage the unhealthy pod, then re-run `rebuild-k8s.yml -e rebuild_target=<target>` (cordon + waits + uncordon are idempotent).

### 6. Verify

```sh
poetry run ansible -i inventories/prd k8s_prd -m command \
    -a 'microk8s kubectl get nodes -o wide' --become
# expect: all three workers Ready

poetry run ansible-playbook playbooks/site.yml --limit <target> --check --diff
# expect: changed=0
```

## Primary rebuild — `srvk8s1` (NVMe passthrough)

Use this section when rebuilding the node that currently holds the cluster primary role. The old VM is destroyed (not just shut down) because the new VM needs the NVMe — qemu can't open a device claimed by another running VM.

| target    | VMID | MAC                  |
|-----------|------|----------------------|
| `srvk8s1` | 910  | `02:A7:F3:03:8E:00`  |

### 1. (No primary flip needed)

The `microk8s` role elects the primary at runtime — see `roles/microk8s/tasks/elect-primary.yml`. The election runs against the surviving cluster members (i.e., not the rebuild target), so a survivor is automatically picked as the join-token mint for the duration of the rebuild. Once the rebuilt node joins, the next role apply re-elects naturally. No inventory edit required.

### 2. Evict, leave, remove

```sh
poetry run ansible-playbook playbooks/evict-k8s.yml \
    -e evict_target=srvk8s1
poetry run ansible -i inventories/prd srvk8s1 -m command \
    -a 'microk8s leave' --become
poetry run ansible -i inventories/prd srvk8s2 -m command \
    -a 'microk8s remove-node srvk8s1' --become
```

The hand-off step delegates to whichever surviving node the runtime election picks (`srvk8s2` or `srvk8s3`) — the join-token / kubectl delegate lives there for the rebuild window. `zpool2`-pinned workloads (storage chart, Prometheus) drain off `srvk8s1` and have nowhere to go (no other node carries `homelab.local/storage=zpool2`). They stay Pending until step 7 imports the pool on the new node — that's expected storage-path downtime.

### 3. Shut down + destroy the old VM

```sh
ssh root@pve 'qm shutdown 910 ; sleep 5 ; qm destroy 910'
```

This frees the NVMe (qemu releases the device on destroy; the ZFS pool's on-disk metadata stays for re-import). The new VM in step 6 can now claim it.

### 4. (Static-hosts is hand-curated; no edit unless the IP changes)

`srvk8s1`'s entry in HelmCharts `configs/prd/dnsmasq.yaml` is operator-curated alongside Ceph + printers + IoT. As long as the rebuild keeps the same hostname → IP mapping (the typical case), no change is needed. If the IP changes, edit there and roll the dnsmasq StatefulSet before step 6.

### 5. (Inventory unchanged)

`host_vars/srvk8s1.yml` carries `vm_id: 910` and `zpools_to_import: [zpool2]` from the original phase-4 rebuild — still correct. Skip unless something specifically changed.

### 6. `terraform apply` — create the new VM with NVMe attached

```sh
cd terraform/prd
terraform apply -target='module.vm["srvk8s1"]'
```

The shared from-scratch resources already exist from the worker rebuilds. TF creates the VM and attaches `/dev/disk/by-id/nvme-Samsung_SSD_980_500GB_S64DNX0RC21332X` at scsi2 in the same apply (the plan 01 payoff).

### 7. `rebuild-k8s.yml` — bootstrap, baseline, managed_filesystems, microk8s, ZFS import, cordon-gated DaemonSet wait, uncordon

```sh
cd ../../ansible
poetry run ansible-playbook playbooks/rebuild-k8s.yml \
    -e rebuild_target=srvk8s1
```

Order in the playbook: bootstrap → baseline → managed_filesystems → microk8s (install + join) → cordon → `zpool import zpool2` → wait for the rebuild target to report Ready → wait for every pod scheduled on the new node to reach Ready → uncordon. The role uses `zpool import -f` because rebuilds always cross hostids (the new VM has a different ZFS hostid than the destroyed one); the multi-mount risk that `-f` overrides cannot apply because the source VM is destroyed before the NVMe is reattached.

`managed_filesystems` partitions/formats/mounts scsi1 at `/var/snap` before the snap install. The cluster-join task mints a token from whichever surviving node the runtime election picked.

If the wait times out, the node stays cordoned — triage the unhealthy pod, then re-run `rebuild-k8s.yml -e rebuild_target=srvk8s1` (cordon + waits + uncordon are idempotent).

### 8. Verify

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

## Rebuild — `wrkdevk8s` (single-node dev)

Different shape from prd: dev cluster is a single node, dev-tier networking (DHCP via the standard `homelab_dns_reservation`, not a static-hosts entry — see "Networking shape" at the top), no inter-node traffic, no zpool, no Ceph. The peer-count gate skips the eviction shape, so `evict-k8s.yml` is not part of this flow. The cluster gets fully replaced; HelmCharts deployments under `wrkdevk8s` need re-deployment afterward (operator workflow, separate from this runbook).

```sh
# 1. Destroy the live VM:
ssh root@pve 'qm shutdown 919 ; sleep 5 ; qm destroy 919'

# 2. terraform apply (creates the homelab_dns_reservation + VM):
cd terraform/prd
terraform apply -target='module.vm["wrkdevk8s"]'

# 3. Apply roles:
cd ../../ansible
poetry run ansible-playbook playbooks/rebuild-k8s.yml \
    -e rebuild_target=wrkdevk8s

# 4. Verify:
poetry run ansible -i inventories/prd wrkdevk8s -m command \
    -a 'microk8s kubectl get nodes' --become
# expect: wrkdevk8s Ready (1.32.x or current channel), STATUS without
# SchedulingDisabled
```

**Known wrinkle on retry**: several `microk8s status`-based tasks in the role (addons.yml, elect-primary.yml) substring-match `" Ready "` in `kubectl get nodes`. A cordoned single-node cluster prints `Ready,SchedulingDisabled` and the gate fails. If the playbook fails partway through and you re-run, manually uncordon first:

```sh
ssh ansible@wrkdevk8s.home 'sudo microk8s kubectl uncordon wrkdevk8s'
```

The role-wide cordon-safety pass is queued as a slice; only bites on partial-rerun against single-node dev.

## If a rebuild goes sideways

There's no "uncordon to roll back" — by the time `terraform apply` runs the old VM is shut down (workers) or destroyed (`srvk8s1`). Recovery is forward.

- **TF errors at create:** fix the TF error, retry the targeted apply. For workers, the old shut-down VM is still on PVE; in a real emergency `qm start <old-vmid>` + `microk8s reset` + `microk8s join` it as a fresh worker. For `srvk8s1`, the old VM is gone — fix forward only.
- **Cloud-init never finishes (TF times out waiting for IP):** `qm console <new-vmid>` on PVE to inspect; usually the snippet's `qemu-guest-agent` install failed. Fix and `terraform apply -replace='module.vm["<new-hostname>"].proxmox_virtual_environment_vm.this'`.
- **`rebuild-k8s.yml` fails on cluster-join:** the new VM exists but isn't in the cluster. `microk8s status` on the rebuild target tells you what's happening. Common cause: stale `/var/snap/microk8s/common/var/lib/dqlite` from a half-completed prior attempt. `microk8s reset` on the new node, re-run the playbook.
- **`zpool import zpool2` fails on `srvk8s1`:** the role passes `-f` because rebuilds always cross hostids (the new VM has a different ZFS hostid than the destroyed one). The multi-mount risk that `-f` overrides cannot apply because the source VM is destroyed before the NVMe is reattached. If a future rebuild surfaces a different import failure, fix forward — `microk8s reset` on the new node and re-run the playbook is the reset hammer.

Verify `site.yml --check --diff` reports zero changes against the rebuild target before declaring success.

## What this runbook does not cover

- Microceph rebuilds (Phase 5).
- HelmCharts redeploy on `wrkdevk8s` after rebuild — operator workflow, see HelmCharts repo.
- Recovering from a corrupted dqlite database — `microk8s reset` is the reset hammer; deeper recovery is per microk8s upstream docs.
