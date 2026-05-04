# Phase 4d — k8s rebuild completion

**Status**: ⏳ Planned

## Goal

Finish the parity event Phase 4c started: rebuild `srvk8s3`, `srvk8s1`, and `wrkdevk8s`, retire the legacy adoption known_hosts files, and close the worker rebuilds out by destroying the parked old VMs. After this phase, every k8s node is on the from-scratch + static-IP shape and the cluster has no remaining adoption-shape state.

## Carry-over from 4c

- `srvk8s2` is rebuilt and joined as worker #1 of the new shape (`10.1.0.28/16` + `10.2.0.28/16` + `192.168.188.28/24`, static netplan from cloud-init). Soaking overnight; verification list below.
- The static-IP pivot for k8s nodes (`static_ip = true` + per-NIC `addresses`/`gateway`/`nameservers` in `vms.tf`, cloud-init renders netplan) is committed and applies to all four from-scratch k8s VMs. `srvk8s3`, `srvk8s1`, `wrkdevk8s` already carry the right IP fields — their rebuilds use the originally-planned flow without the mid-flight detour.
- VMID 104 (old `srvk8ss1`) is shut down on `pve1`, kept as escape hatch.
- TF state is clean: the three orphan k8s VM module instances were `state rm`'d before the first apply; no leftover destroys queued on subsequent applies.
- HelmCharts `static-hosts.yaml` carries the new entries for srvk8s1/2/3 + wrkdevk8s (operator landed them ahead of the apply).
- Plan 07 (`pre-drain hand-off readiness check`) captured during the pre-flight; not a blocker for 4d, fold in opportunistically.

## Morning checks — srvk8s2 soak

Run before kicking off the next rebuild:

```sh
# Pod inventory on srvk8s2 — restart counts, anything Pending/CrashLoop?
poetry run ansible -i inventories/prd srvk8sl1 -m command \
    -a 'microk8s kubectl get pods -A -o wide --field-selector spec.nodeName=srvk8s2' --become

# Recent events touching srvk8s2:
poetry run ansible -i inventories/prd srvk8sl1 -m shell \
    -a "microk8s kubectl get events -A --sort-by='.lastTimestamp' | grep srvk8s2 | tail -30" --become

# Node conditions / taints:
poetry run ansible -i inventories/prd srvk8sl1 -m command \
    -a 'microk8s kubectl describe node srvk8s2' --become

# Kubelite / containerd journal — clean, no panics or restart loops?
ssh ansible@srvk8s2 'journalctl -u snap.microk8s.daemon-kubelite --since "12 hours ago" | grep -iE "panic|fatal|restart|error" | tail -20'

# MetalLB speaker on srvk8s2 — advertising on the workload VLAN?
poetry run ansible -i inventories/prd srvk8sl1 -m command \
    -a 'microk8s kubectl logs -n metallb-system speaker-jdw68 --tail=50' --become
```

Pod-name in the speaker log line will rotate; substitute current.

## Label parity — verify before stress test

The post-rebuild srvk8s2 carried only kubernetes-default labels — no `homelab.local/*` capability or workload-class labels. The `microk8s` role's `labels.yml` reconciles labels from inventory; if `host_vars/srvk8s2.yml` doesn't declare them, the role reports `changed=0` because there's nothing to apply.

That's why the CI build pod landed (bare Pod, schedule-anywhere) but no Helm-pinned workloads have appeared on srvk8s2 — they're pinned via `homelab.local/*` and won't schedule here without those labels.

Action before resuming rebuilds:

```sh
# What srvk8sl1 (the still-adoption-shape primary) carries:
poetry run ansible -i inventories/prd srvk8sl1 -m command \
    -a 'microk8s kubectl get node srvk8sl1 --show-labels' --become

# Same for srvk8ss2:
poetry run ansible -i inventories/prd srvk8sl1 -m command \
    -a 'microk8s kubectl get node srvk8ss2 --show-labels' --become
```

Diff the lists. The `homelab.local/*` labels (capability + storage-class per `decisions.md` "k8s node capability labels") need to land in `host_vars/srvk8s2.yml` so the role reconciles them onto the new node. If we don't, the `srvk8s3` and `srvk8s1` rebuilds inherit the same gap.

This is the actionable item before kicking off srvk8s3.

## Remaining rebuilds

In order, per the runbook (`docs/runbooks/k8s-rebuild.md`):

### 1. `srvk8ss2` → `srvk8s3` (worker #2)

Standard worker rebuild. Old VMID 107 on `pve2`, new VMID 912. Same flow as srvk8s2 minus the first-rebuild scaffolding (cloud-init/tls/known_hosts already exist):

```sh
# evict, leave, remove
poetry run ansible-playbook playbooks/evict-k8s.yml -e evict_target=srvk8ss2
poetry run ansible -i inventories/prd srvk8ss2 -m command -a 'microk8s leave' --become
poetry run ansible -i inventories/prd srvk8sl1 -m command -a 'microk8s kubectl delete node srvk8ss2' --become
# (note: "delete node" not "remove-node" if the leave already cleared dqlite — confirm via runbook)

# shutdown old, drop static-hosts entry for srvk8ss2 from HelmCharts
ssh root@pve2 qm shutdown 107

# inventory rename
git mv ansible/inventories/prd/host_vars/srvk8ss2.yml \
       ansible/inventories/prd/host_vars/srvk8s3.yml
# edit srvk8s3.yml: vm_id 107 → 912, add capability labels per the parity action above

# apply (no -replace needed — VM doesn't yet exist in state)
cd terraform/prd
terraform plan -target='module.vm["srvk8s3"]' -out=tfplan
terraform apply tfplan

# rebuild
cd ../../ansible
poetry run ansible-playbook playbooks/rebuild-k8s.yml -e rebuild_target=srvk8s3
```

Soak briefly between this and srvk8s1 — at minimum confirm `kubectl get nodes` is 4 Ready (counting srvk8sl1 still on the old shape), no system-pod regressions.

### 2. `srvk8sl1` → `srvk8s1` (primary, NVMe passthrough)

Trickier:
- Flip primary off srvk8sl1 first (`microk8s_primary_host` in `group_vars/k8s_prd.yml` → `srvk8s2`).
- The old VM gets **destroyed** (not just shut down) — qemu can't release the NVMe to the new VM otherwise.
- `zpool import zpool2` runs from `rebuild-k8s.yml` after first boot.
- HelmCharts workloads pinned to `homelab.local/storage=zpool2` are unschedulable during the window (storage chart, Prometheus). Document the maintenance window.

Per runbook "Primary rebuild" section. No extra TF detours expected.

### 3. `wrkdevk8s` (single-node dev, greenfield)

Different shape: the live `wrkdevk8s` (VMID 119) is a manual VM never imported into TF state. Operator destroys VMID 119 manually; TF creates VMID 919 from scratch. Single-node cluster — no eviction or hand-off needed (peer-count gate skips it).

Drop the `microk8s_channel: 1.30/stable` override from `host_vars/wrkdevk8s.yml` so `group_vars/k8s_dev.yml`'s `1.32/stable` takes effect. Bump `vm_id` 119 → 919.

HelmCharts dev deployments are gone with the old VM and need re-deployment via the HelmCharts repo's normal `configs/dev` flow — operator workflow, separate from this phase.

## Close-the-parity-event commit

After all four rebuilds:

- Retire `ansible/files/known_hosts.d/k8s_prd` and `ansible/files/known_hosts.d/k8s_dev` (the adoption-era files).
- Drop both from `ansible/ansible.cfg`'s `UserKnownHostsFile`.
- `qm destroy 104` (old srvk8ss1), `qm destroy 107` (old srvk8ss2), once both worker rebuilds have soaked clean.

Single commit: `ansible: retire adoption known_hosts files (k8s_prd, k8s_dev)`.

## Runbook + decisions.md follow-ups

Fold these in once the phase closes — quiet edits, no ops impact:

- `docs/runbooks/k8s-rebuild.md` step 5 ("first worker rebuild — also pulls in the from-scratch shape's shared resources"): the targeting now needs to include `proxmox_virtual_environment_file.cloud_init` because the cloud-init content is meaningful per-VM (not just a thin wrapper). Update the example apply command.
- `docs/runbooks/k8s-rebuild.md`: add a note about the `terraform state rm` of orphan k8s module instances *before* the first targeted apply — `for_each` orphan reconciliation isn't suppressed by `-target`, so the pre-apply state cleanup that 4c discovered needs to be in the runbook.
- Same runbook: note the static-IP pivot — k8s rebuilds no longer go through `homelab_dns_reservation` and the operator owns `static-hosts.yaml` entries instead.
- `docs/decisions.md` "Bring-up-tier hosts" already updated in 4c. No further edit needed.

## Out of scope

- Microceph rebuilds (Phase 5).
- Sidecar/StatefulSet side of DNS automation (Phase 9). The reservation TF resource itself is in use for non-bring-up-tier hosts; the rest of Phase 9 is its own phase.
- HelmCharts redeploy on `wrkdevk8s` after rebuild — operator workflow.
- Plan 07 (pre-drain hand-off readiness check) — separate effort, can land any time.
