# Phase 4a — microk8s alignment, upgrade, and rebuild

**Status**: ⏳ Planned

## Goal

Take the microk8s role from "exercised on scratch" (Phase 4) to "every k8s host built from scratch off the role on a verified upgrade and rebuild path." Complete the role's missing reconciliation pieces (capability labels, MetalLB IPAddressPool), adopt the live prod and dev clusters under the role additively, drive the HelmCharts label migration to a clean slate, deliver the drain-aware upgrade playbook, and rebuild all four k8s VMs (`srvk8sl1/ss1/ss2 → srvk8s1/2/3` plus `wrkdevk8s`) on the from-scratch shape. The phase closes when the four nodes are running off the role with no residual drift — that's the parity event per `decisions.md` "Adoption is a waypoint; rebuild is the parity event."

## Decisions carried forward from Phase 4

All locked in `decisions.md`; recapping the load-bearing ones:

- **Channel**: `1.32/stable` for both clusters. Strict variant rejected.
- **Calico**: VXLAN. Autodetect via `ansible_default_ipv4.interface`. BGP tabled.
- **Capability labels**: `homelab.local/storage=zpool2` (the ZFS passthrough), `homelab.local/performance=high` (the 8-core node) — declared per host in `host_vars/<node>.yml` as operator intent, not auto-derived from TF facts.
- **Hostname rename at rebuild**: `srvk8sl1 → srvk8s1` (pve), `srvk8ss1 → srvk8s2` (pve1), `srvk8ss2 → srvk8s3` (pve2). `wrkdevk8s` keeps its name.
- **VMIDs**: `srvk8s1=910`, `srvk8s2=911`, `srvk8s3=912`, `wrkdevk8s=919`.
- **Disabled addons**: `core/ingress`, `core/registry` (operator runs own).
- **`serial: 1` in `rebuild-k8s.yml` is non-negotiable.** Beyond two nodes, parallel `microk8s join` calls race on dqlite cluster-state writes; the rebuild playbook must process one node at a time. Same constraint for the upgrade playbook.

## Scope

In:

- `microk8s` role: capability label reconciliation, MetalLB `IPAddressPool` + `L2Advertisement` reconciliation. Both primary-only, both via `kubernetes.core.k8s`.
- prd + dev group_vars: CIDRs, channel, MetalLB pool, addon list, primary host. Per-host `host_vars` for every k8s node carries `vm_id`, `pve_node`, `workload_class`, `k8s_node_labels`.
- Adopt live clusters via `--check --diff` then additive apply. Legacy `size=` labels and `PreferNoSchedule` taint stay until HelmCharts has migrated.
- HelmCharts migration off `size=` is operator-driven and out-of-band; this phase doc captures the gate (when migration completes, role removes the legacy labels + taint).
- `update-k8s.yml` playbook + runbook. Drain-aware rolling upgrade, `serial: 1`, single-node case skips drain.
- Per-VM TF module rework to the from-scratch shape (one commit per VM).
- `rebuild-k8s.yml` playbook + runbook. Drain → TF replace → role apply → uncordon, `serial: 1`.
- Rebuild execution: prd k8s nodes one-at-a-time, then `wrkdevk8s`. Hostname rename happens at rebuild.

Out:

- Ceph CSI install / config (HelmCharts).
- Application-level CoreDNS rewrites, registry pod, MetalLB BGP (HelmCharts / deferred).
- microceph install on Ceph nodes (Phase 5).
- DNS reservation Terraform resource (Phase 9). Each rebuild's MAC change is a hand-edit of the dnsmasq deployment.
- Self-hosted Jenkins agent + CI-driven runs (Phase 10). Operator-driven throughout.

## Deliverables

- `ansible/roles/microk8s/tasks/labels.yml` — kubernetes.core-driven label reconciliation, primary-only.
- `ansible/roles/microk8s/tasks/metallb.yml` — IPAddressPool + L2Advertisement reconciliation, primary-only.
- `ansible/inventories/prd/group_vars/k8s_prd.yml`, `k8s_dev.yml` — channel, CIDRs, MetalLB pool, extraSANs, addon list, `microk8s_primary_host`.
- `ansible/inventories/prd/host_vars/srvk8s{1,2,3}.yml`, `wrkdevk8s.yml` — `vm_id`, `pve_node`, `workload_class`, `k8s_node_labels`.
- `ansible/playbooks/update-k8s.yml` — drain-aware rolling upgrade, `serial: 1`.
- `ansible/playbooks/rebuild-k8s.yml` — drain → TF replace → role apply → uncordon, `serial: 1`.
- `terraform/prd/vms.tf` — three k8s entries reworked to from-scratch shape (one commit per VM); `wrkdevk8s` added (currently absent from TF).
- `docs/runbooks/k8s-upgrade.md` — operator procedure for `update-k8s.yml`, including rollback (`snap revert microk8s`) and the kernel-pin caveat from Obsidian (the 6.8.0-57 ip6tables regression that forced 6.8.0-55 last cycle — keep the pin until microk8s tracks past it).
- `docs/runbooks/k8s-rebuild.md` — concrete rebuild per node, including `srvk8s1`'s ZFS passthrough reattach and the prd hostname-rename + dnsmasq updates.
- `ansible/playbooks/microk8s.yml` consolidated into `site.yml` once group_vars exist for prd + dev (after step 3).

## Sequencing

Each step leaves the repo in a usable state.

1. **Capability labels reconciliation in the role.** `tasks/labels.yml` reads `k8s_node_labels` from each host's `host_vars`, applies via `kubernetes.core.k8s` from the primary, idempotent. Smoke against scratch — no labels declared today, so it's a no-op there. End state: scratch's `--check --diff` still reports `changed=0`; the role grew a feature.
2. **MetalLB pool reconciliation in the role.** `tasks/metallb.yml` renders an `IPAddressPool` + `L2Advertisement` spec from inventory variables, applies via `kubernetes.core.k8s`, primary-only. Add the `metallb` addon to scratch's enabled list and a small test pool to scratch's group_vars to exercise it end-to-end on the scratch cluster.
3. **Build prd + dev inventory.** `inventories/prd/group_vars/k8s_prd.yml` (channel `1.32/stable`, CIDRs `172.16.0.0/16` + `172.17.0.0/16`, IPv6 `fd01::/64` + `fd98::/108`, MetalLB pool `10.2.1.1-10.2.1.199` + IPv6, primary `srvk8sl1` until rebuild renames it). `k8s_dev.yml` (CIDRs `172.18.0.0/16` + `172.19.0.0/16`, pool `10.1.2.1-10.1.2.199` + IPv6, primary `wrkdevk8s`). Per-host `host_vars/srvk8sl1.yml` etc. gain `k8s_node_labels` — only `srvk8sl1` carries entries today: `homelab.local/storage=zpool2`, `homelab.local/performance=high`. Add the `microk8s` play to `site.yml` for `hosts: k8s` and retire `playbooks/microk8s.yml`.
4. **Adopt the live clusters at Ansible level (additive, no destructive change).** `--check --diff` against `wrkdevk8s` and the three prod nodes. Reconcile drift the role surfaces — codify what's correct, flag what's deliberately divergent. Then apply for real, additively: new capability labels land on `srvk8sl1`, MetalLB pool reconciled, `.microk8s.yaml` codified, autodetect drift fixed (drop the `bgp` cluster-type env on prod). **Legacy `size=large/small` labels and the `PreferNoSchedule` taint stay in place** so HelmCharts has both label sets to migrate against.
5. **HelmCharts migrates off `size=` (operator-driven, out-of-band).** Switch charts in `/work/HelmCharts`: `node-affinity.require-large` → `homelab.local/storage=zpool2` for hostPath workloads (storage chart, Prometheus); `node-affinity.require-large` → `homelab.local/performance=high` for the Jenkins agent template + Plex. Drop `node-affinity.allow-large` entirely. Operator verifies pods schedule correctly while both label sets exist on the cluster.
6. **Remove legacy `size=` labels + taint.** Once HelmCharts no longer references `size=`, the role's labels reconciliation removes `size=large`/`size=small` (or the operator does it once with `kubectl label … size-`); the `PreferNoSchedule` taint comes off `srvk8sl1` the same way. End state: live clusters carry only the new label scheme. Cluster is now in target shape pre-rebuild.
7. **Build and exercise `update-k8s.yml`.** First against `wrkdevk8s` (single-node, no drain — useful smoke). Then against `k8s_prd` with `serial: 1` and a real drain/uncordon cycle. prd is already on `1.32/stable`, so the snap refresh is a no-op exercising the orchestration. Skip running the upgrade against `k8s_dev` (it's on `1.30/stable`) because step 11 rebuilds dev clean from `1.32/stable` anyway.
8. **Extend the per-VM TF modules to the from-scratch shape.** One commit per VM in deliberate order: `srvk8s1`, `srvk8s2`, `srvk8s3`, `wrkdevk8s`. Each commit adds the cloud-init template + `proxmox_virtual_environment_file`, a per-VM `tls_private_key`, the `local_file` extending `ansible/files/known_hosts.d/prd`, the deterministic MAC, the VMID rotation into the 910-range, and `bios = "ovmf"` (which folds in the `srvk8ss2 → srvk8s3` BIOS flip implicitly). `srvk8s1` retains the NVMe passthrough block; the rebuild flow in step 9 drops it pre-replace and re-adds post-import. No `terraform apply` yet — the commits sit ready.
9. **Build `rebuild-k8s.yml`.** Drain → `terraform apply -replace=module.vm["<host>"]` → role apply (call back into `site.yml`'s tag) → reattach passthroughs (`srvk8s1` only) → wait Ready → uncordon. **`serial: 1`** so cluster join calls don't race on dqlite (per the constraint above). Single-node case (`wrkdevk8s`) skips the drain step. Smoke-test against scratch first by destroying and rebuilding `wrkscratchk8s2` while `wrkscratchk8s1` stays Ready.
10. **Rebuild prd, one node at a time.** `rebuild-k8s.yml -l srvk8s1` first — the host with the ZFS passthrough, the trickiest path; tackle it on a node we know how to rescue. Operator pre-step per VM: update the dnsmasq deployment with the new (hostname, MAC, IP) triples for `srvk8s1/2/3`. Verify zero-residual against the rebuilt node before moving on. Repeat for `srvk8s2`, `srvk8s3`. After all three, re-run `--check --diff` against the cluster — expect a clean report.
11. **Rebuild `wrkdevk8s`.** Same flow, single-node — no drain, no rename. New CIDRs (`172.18.0.0/16` / `172.19.0.0/16`) replace the live `10.3.0.0/16`. `core/ingress` does not get re-enabled. Closes the parity event for `k8s_dev`.

After step 11: every k8s host is from-scratch built off the role, on the pinned LTS channel, with a tested upgrade and rebuild path. Hostnames are clean (`srvk8s1/2/3` + `wrkdevk8s`). Capability labels carry semantics; HelmCharts charts pin via affinity by capability. Phase closes.

## Constraints carried forward (Phase 4 → 4a)

- **Per-VM TF module shape extension owed for the three k8s entries.** Phase 3a imported the six prod VMs in adoption shape; this phase reworks the three k8s ones to the from-scratch shape and adds `wrkdevk8s` (currently absent from TF).
- **`srvk8ss2 → srvk8s3` BIOS flip.** Live VM is `seabios`. The rename + rebuild commit flips to `ovmf` and adds the EFI disk. (Operator memory: `project_srvk8ss2_uefi.md`.)
- **`srvk8sl1 → srvk8s1` ZFS passthrough.** The Samsung 980 NVMe at `nvme1n1` carries `zpool2`. TF cannot recreate the passthrough block (decisions.md "Disk passthrough on managed VMs"). Rebuild path: drop the `passthrough_disks` block from the new `srvk8s1` module before `apply -replace`; reattach via `qm set` from the role; `zpool import zpool2` runs on first boot from existing pool metadata. Re-add the block to the module post-import so plan reflects reality.
- **Manual dnsmasq reservation per rebuild.** Until Phase 9 lands the dnsmasq Terraform resource, every (hostname, MAC, IP) change is a hand-edit of the dnsmasq deployment before `terraform apply`. Operator handles the four new reservations (`srvk8s1/2/3` + `wrkdevk8s`'s MAC change).
- **`serial: 1` for cluster-member orchestration.** Both `update-k8s.yml` and `rebuild-k8s.yml` enforce one-at-a-time on cluster members. The constraint comes from microk8s's dqlite serialization of cluster-state writes — beyond two nodes, parallel join/upgrade calls race.

## Live state vs. target state

The drift table from the now-closed Phase 4 doc reproduced here, since step 4 is what reconciles it. Anything `✓` is already aligned; everything else gets codified into role state during adoption.

| What | Live (today) | Target |
|---|---|---|
| Channel (prd) | `1.32/stable` | `1.32/stable` ✓ |
| Channel (dev) | `1.30/stable` | `1.32/stable` (settled at step 11 rebuild) |
| Cluster CIDR (prd) | `172.16.0.0/16` | `172.16.0.0/16` ✓ |
| Cluster CIDR (dev) | `10.3.0.0/16` | `172.18.0.0/16` (settled at step 11 rebuild) |
| Calico mode | VXLAN | VXLAN ✓ |
| Calico autodetect env | `CLUSTER_TYPE=k8s,bgp` (drift) | drop the `bgp` cluster-type env; `interface=<ansible_default_ipv4.interface>` |
| MetalLB pool (prd) | `10.2.1.1-10.2.1.199` (+ IPv6) | same ✓ — codified by step 2 |
| MetalLB pool (dev) | `10.1.2.1-10.1.2.199` (+ IPv6) | same ✓ — codified by step 2 |
| `core/ingress` (dev) | enabled | disabled (settled at step 11 rebuild) |
| `core/registry` (prd) | enabled | disabled |
| `size=large` label, `PreferNoSchedule` taint | on `srvk8sl1` | removed at step 6 (after HelmCharts migration) |
| `size=small` label | on `srvk8ss2` only — `srvk8ss1` lacks it | irrelevant; both labels go away at step 6 |
| `srvk8sl1`'s `zpool2` | ONLINE on `nvme1n1` | preserved through rebuild via reattach + `zpool import` |
| Registry mirrors `certs.d/` | empty | not codified (operator runs own registry) |

## What this phase deliberately does not solve

- **OpenBao integration on k8s nodes.** CSI secrets, ESO, and so on are HelmCharts territory and arrive in Phase 6.
- **Cluster autoscaling, NFD, gpu-operator, anything addon-shaped beyond the configured set.** Add later if a workload needs it.
- **MetalLB BGP.** Tabled — not preserved as a templated alternative. Revisit fresh when the operator wants to actually exercise it.
- **etcd / dqlite snapshot backup.** VM-level vzdump from `proxmox_host` (Phase 2) covers it; cluster-state backup beyond that is a Phase 10 follow-up.
- **CI-scheduled drift detection.** `--check --diff` is the manual mechanism; CI runs land in Phase 10.
