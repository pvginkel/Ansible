# Phase 4b — k8s VM rebuild

**Status**: ⏳ Planned

## Goal

Rebuild all four k8s VMs from scratch on the from-scratch shape, ending the adoption transition for `k8s_prd` and `k8s_dev`. Per-VM Terraform modules get reworked to match the cloud-init + deterministic-MAC + UEFI shape used elsewhere; each VM is then destroyed and recreated one at a time via a drain-aware playbook. Closes the parity event per `decisions.md` "Adoption is a waypoint; rebuild is the parity event."

## Inputs from Phase 4a

The cluster is already in target shape pre-rebuild:

- Channel: prd on `1.32/stable`; dev on `1.30/stable` (pinned in `host_vars/wrkdevk8s.yml` until rebuild lifts the override).
- All four k8s nodes adopted under Ansible (bootstrap + baseline + microk8s reconciled).
- Capability labels: `homelab.local/performance=high`, `homelab.local/storage=zpool2` on `srvk8sl1`.
- MetalLB pool + L2Advertisement reconciled.
- Calico autodetect + CLUSTER_TYPE drift fixed.
- Legacy `size=*` labels and `PreferNoSchedule` taint removed.
- HelmCharts migrated to `homelab.local/*` affinity.
- `update-k8s.yml` exercised on scratch + wrkdevk8s + k8s_prd; `state: refreshed` was a dead end (community.general.snap doesn't expose it), playbook now uses `snap refresh` via `command:` directly.
- `refresh-k8s-addons.yml` committed but unsmoked — operational acceptance happens at the next maintenance window, not a phase 4b prerequisite.

## Decisions carried forward

All locked in `decisions.md`; recapping the load-bearing ones for the rebuild:

- **Hostname rename at rebuild**: `srvk8sl1 → srvk8s1` (on `pve`), `srvk8ss1 → srvk8s2` (on `pve1`), `srvk8ss2 → srvk8s3` (on `pve2`). `wrkdevk8s` keeps its name.
- **VMIDs**: `srvk8s1=910`, `srvk8s2=911`, `srvk8s3=912`, `wrkdevk8s=919`.
- **`serial: 1` in `rebuild-k8s.yml`** is non-negotiable. Parallel `microk8s join` calls race on dqlite cluster-state writes.
- **`srvk8sl1 → srvk8s1` ZFS passthrough**. The Samsung 980 NVMe at `nvme1n1` carries `zpool2`. TF can't recreate the passthrough block (decisions.md "Disk passthrough on managed VMs"). Rebuild path: drop `passthrough_disks` from the new TF module before `apply -replace`; reattach via `qm set` from the rebuild flow; `zpool import zpool2` runs on first boot from existing pool metadata. Re-add the block to the module post-import so plan reflects reality.
- **`srvk8ss2 → srvk8s3` BIOS flip**. Live VM is `seabios`; rebuild commit flips to `ovmf` and adds the EFI disk.
- **Manual dnsmasq reservations** until Phase 9. Every (hostname, MAC, IP) change is a hand-edit of the dnsmasq deployment before `terraform apply`.
- **Channel override on wrkdevk8s** (`1.30/stable` in host_vars) is removed at the wrkdevk8s rebuild — group_vars's `1.32/stable` takes over.
- **Adoption known_hosts files**: `files/known_hosts.d/k8s_prd` and `k8s_dev` (created by `adopt.yml`) are transitional. The TF rework's `local_file` resources extend `files/known_hosts.d/prd` for the rebuilt fleet; the adoption files get retired once all four nodes are on TF-managed keys.

## Scope

In:
- Per-VM TF module rework for `srvk8s1`, `srvk8s2`, `srvk8s3` (existing entries reworked) and `wrkdevk8s` (greenfield — currently absent from TF).
- `ansible/playbooks/rebuild-k8s.yml` — drain → TF replace → role apply → reattach passthroughs → wait Ready → uncordon, `serial: 1`.
- `docs/runbooks/k8s-rebuild.md` — operator procedure including dnsmasq updates and the ZFS passthrough reattach.
- Real rebuilds: prd nodes one at a time (`srvk8s1` first, the trickiest), then `wrkdevk8s`.
- inventory updates: rename `host_vars/srvk8sl1.yml → host_vars/srvk8s1.yml` etc. as each rebuild completes. Drop the wrkdevk8s channel override.
- known_hosts consolidation: as TF takes ownership of host keys, retire the adoption files.

Out:
- microceph rebuilds (Phase 5).
- DNS reservation TF resource (Phase 9). Reservation updates remain manual.
- Self-hosted Jenkins agent + CI-driven runs (Phase 10). Operator-driven throughout.
- HelmCharts changes (separate repo, operator-driven).
- `refresh-k8s-addons.yml` smoke — operational, not a phase deliverable.

## Deliverables

- `terraform/prd/vms.tf` — four entries reworked: three k8s prd VMs to from-scratch shape (one commit per VM), `wrkdevk8s` added.
- `ansible/playbooks/rebuild-k8s.yml`.
- `docs/runbooks/k8s-rebuild.md`.
- `ansible/inventories/prd/host_vars/srvk8s{1,2,3}.yml` (replacing `srvk8sl1/ss1/ss2.yml`) and updated `wrkdevk8s.yml` (channel override gone).
- `ansible/files/known_hosts.d/prd` populated with the four rebuilt nodes; the `k8s_prd` and `k8s_dev` adoption files retired.

## Sequencing

Each step leaves the repo in a usable state.

1. **Per-VM TF module rework**, one commit per VM in deliberate order: `srvk8s1`, `srvk8s2`, `srvk8s3`, `wrkdevk8s`. Each commit adds the cloud-init template + `proxmox_virtual_environment_file`, a per-VM `tls_private_key`, the `local_file` extending `ansible/files/known_hosts.d/prd`, the deterministic MAC, the VMID rotation into the 910-range, and `bios = "ovmf"` (folds in the `srvk8ss2 → srvk8s3` BIOS flip implicitly). `srvk8s1` retains the NVMe passthrough block; the rebuild flow in step 2 drops it pre-replace and re-adds post-import. **No `terraform apply` yet** — the commits sit ready.

2. **Build `rebuild-k8s.yml`**. Per host (`serial: 1`):
   1. Drain via the cluster primary (skipped on single-node clusters).
   2. `terraform apply -replace=module.vm["<host>"]` (called from the playbook controller).
   3. Wait for the new VM's SSH to come up.
   4. Apply `site.yml` against the rebuilt host (microk8s role does install + join + reconcile).
   5. Reattach passthrough disks (`srvk8s1` only; `qm set <vmid> --scsiN /dev/disk/by-id/<serial>,backup=0`).
   6. `zpool import zpool2` (`srvk8s1` only).
   7. Wait microk8s `Ready`.
   8. Uncordon.

   Smoke against scratch first by destroying and rebuilding `wrkscratchk8s2` while `wrkscratchk8s1` stays Ready. Don't smoke against `srvk8sl1` until scratch is clean.

3. **Rebuild prd, one node at a time.** `rebuild-k8s.yml -l srvk8s1` first — the host with the ZFS passthrough, the trickiest path; tackle it on a node we know how to rescue. Operator pre-step per VM: update the dnsmasq deployment with the new (hostname, MAC, IP) triple. Inventory rename happens between rebuilds (rename `host_vars/srvk8sl1.yml → srvk8s1.yml` and add the new hostname to `inventories/prd/hosts.yml`). Verify zero residual against the rebuilt node before moving on. Repeat for `srvk8s2`, `srvk8s3`. After all three, re-run `site.yml --check --diff` against the cluster — expect a clean report.

4. **Rebuild `wrkdevk8s`.** Same flow, single-node — no drain, no rename. New CIDRs (`172.18.0.0/16` / `172.19.0.0/16`) replace the live `10.3.0.0/16`. `core/ingress` does not get re-enabled. Drop the `microk8s_channel` override from `host_vars/wrkdevk8s.yml` so `1.32/stable` from `k8s_dev.yml` takes effect. Closes the parity event for `k8s_dev`.

After step 4: every k8s host is from-scratch built off the role, on the pinned LTS channel, with deterministic MACs, UEFI BIOS, TF-managed host keys. Hostnames are clean (`srvk8s1/2/3` + `wrkdevk8s`). Phase 4b closes.

## Recovery + risks

- **TF apply -replace with passthrough**: the create path will fail if `passthrough_disks` is declared. Either the TF module for `srvk8s1` drops the block before the rebuild starts (re-add post-import), or the rebuild flow stages the change explicitly via `terraform state rm` + restore. Decided when the rebuild runs.
- **dnsmasq reservation drift**: forgetting to update the reservation before `terraform apply` lands the new VM with no DHCP lease for its expected IP, breaking host-key verification + role apply. Runbook makes this a checklist item.
- **dqlite quorum risk during rebuild**: with two voters left while rebuilding the third, a second failure during the window risks quorum. Mitigation: serial rebuilds with `microk8s status` health checks between each; rebuild prd in a maintenance window.
- **HelmCharts hostPath workloads on srvk8sl1**: `homelab.local/storage=zpool2` workloads (storage chart, Prometheus) are pinned to `srvk8sl1` via required affinity. While `srvk8s1` is being rebuilt, those pods are unschedulable. Acceptable for the rebuild window; document expected unavailability.

## Live state vs. target state

The drift items still settled by phase 4b (everything else closed in 4a):

| What | Live (today) | Target |
|---|---|---|
| Channel (dev) | `1.30/stable` (pinned via host_vars) | `1.32/stable` from group_vars (override removed at rebuild) |
| Cluster CIDR (dev) | `10.3.0.0/16` | `172.18.0.0/16` |
| Service CIDR (dev) | `10.153.183.0/24` | `172.19.0.0/16` |
| `core/ingress` (dev) | enabled | disabled |
| VMIDs | 103/104/107/119 (legacy) | 910/911/912/919 (TF range) |
| MAC addresses | Proxmox-generated `BC:24:11:...` | deterministic `02:A7:F3:VV:VV:00` per VMID |
| Hostnames (prd) | `srvk8sl1/ss1/ss2` | `srvk8s1/2/3` |
| BIOS (`srvk8ss2`) | `seabios` | `ovmf` |
| TF state | adoption shape (no cloud-init, no host-key resource) | from-scratch shape |
| `srvk8sl1`'s `zpool2` | ONLINE on `nvme1n1` | preserved through rebuild via reattach + `zpool import` |
| Host keys | `files/known_hosts.d/k8s_prd`, `k8s_dev` (adoption) | `files/known_hosts.d/prd` (TF-owned) |

## What this phase deliberately does not solve

- **Cluster autoscaling, NFD, gpu-operator**, anything addon-shaped beyond the configured set. Add later if a workload needs it.
- **MetalLB BGP.** Tabled.
- **etcd / dqlite snapshot backup beyond vzdump.** Phase 10 follow-up if needed.
- **CI-scheduled drift detection.** Phase 10.
- **DNS reservation TF resource.** Phase 9; until then dnsmasq updates are manual per-VM.
