# Phase 4 — microk8s roles and upgrade

**Status**: ⏳ Planned

## Goal

Bring every k8s VM under Ansible-owned cluster lifecycle: install, join, HA, upgrade, rebuild. After this phase a fresh k8s node is reproducible from inventory + `site.yml`, and a routine OS or microk8s upgrade is one playbook invocation against `k8s_prd` (or `k8s_dev`) with serialized cordon/drain/reboot/uncordon. The phase ends when the three production nodes have been **rebuilt** from scratch under their new names (`srvk8s1/2/3`) and the dev node has been rebuilt onto the canonical CIDRs — the parity event per `docs/decisions.md` "Adoption is a waypoint; rebuild is the parity event."

## Source material

`/work/Obsidian/Kubernetes.md` is the procedural runbook to port. `/work/KubernetesConfig` is the second source — its YAMLs (`microk8s/lc-{prd,dev}.yaml`, `metallb/{prd,dev}.yaml`) get absorbed into role defaults / inventory; its `installatie/*.md` is largely superseded by Obsidian. After Phase 4 lands, `/work/KubernetesConfig` is archived.

Caveats on Obsidian:

- Dutch headings; the imperative shell snippets are the parts to lift.
- Two sets of CIDRs (`OUD` / `NIEUW`). Use `NIEUW` — `OUD` is pre-renumber. Prod: `172.16.0.0/16` cluster, `172.17.0.0/16` service. Dev: `172.18.0.0/16` / `172.19.0.0/16` (live dev is currently on `10.3.0.0/16` — drift; the rebuild aligns it to the canonical doc value).
- Sections ending "Is verplaatst naar HelmCharts" are workload concerns and stay out of scope: Ceph CSI install, application-side CoreDNS rewrites, Helm-deployed registry pod.
- The Calico BGP toggle was tied to an abandoned MetalLB-BGP attempt. Calico stays VXLAN; BGP is tabled completely (no preserved scaffolding).

## Decisions (locked)

Settled at phase start; durable choices flow into `docs/decisions.md`. Procedural choices stay here.

1. **Channel**: `1.32/stable` for both clusters. Strict variant rejected. Pinned per-cluster in `group_vars/k8s_{prd,dev}.yml`. Recorded in `decisions.md` "k8s version policy."
2. **HA topology**: all three prod nodes are full control-plane voters (microk8s 1.19+ auto-promotes on join). Scheduling labels are hints, not a CP/worker split.
3. **Calico**: VXLAN, `IP_AUTODETECTION_METHOD=interface=ens18`. BGP tabled — no preserved BGP YAMLs; revisit fresh when the operator wants to actually exercise it.
4. **MetalLB**: addon enabled by Ansible; `IPAddressPool` and `L2Advertisement` specs reconciled by Ansible too (per the absorbed KubernetesConfig). Pools per cluster from inventory: prd `10.2.1.1-10.2.1.199` (+ IPv6 `2a10:3781:565a:0:7912:b75b::/96`); dev `10.1.2.1-10.1.2.199` (+ IPv6 `2a10:3781:565a:1:7912:b75a::/96`).
5. **`wrkdevk8s` rebuild**: included. The dev node is on `1.30/stable`, wrong CIDR (`10.3.0.0/16` instead of `172.18.0.0/16`), and has `core/ingress` enabled — none of which match the canonical state. Rebuild aligns it.
6. **Node labels and naming**: `homelab.local/performance=high` and `homelab.local/storage=zpool2` per `decisions.md` "k8s node capability labels." Hostnames drop the size suffix at rebuild — `srvk8sl1/ss1/ss2` become `srvk8s1/2/3`. The `size=` labels and `PreferNoSchedule` taint are migrated off the live cluster *before* rebuild (see sequencing below) so HelmCharts has a clean slate to migrate against.
7. **VMID assignment**: `srvk8s1=910`, `srvk8s2=911`, `srvk8s3=912`, `wrkdevk8s=919`. `913–918` reserved for future k8s VMs. Scratch range `900–909`: `wrkscratchk8s1=901`, `wrkscratchk8s2=902` (`wrkscratch` (900) destroyed at the start of step 1).
8. **Disabled addons**: `core/ingress` and `core/registry` stay disabled on both clusters. Operator runs their own ingress controller and own container registry from HelmCharts. The `registry-dev:5000` mirror config in Obsidian is stale — not codified.

## Scope

In:

- `microk8s` role: kernel modules, prerequisite packages (Ceph client tooling), snap install pinned to a channel, `.microk8s.yaml` (CIDR + extraSANs), Calico VXLAN + `IP_AUTODETECTION_METHOD=interface=ens18`, addon enablement (community, dns, dashboard, helm, helm3, ha-cluster, metallb, metrics-server), MetalLB `IPAddressPool` + `L2Advertisement` reconciliation, group membership + `kubectl` alias, capability labels, idempotent join.
- Inventory data per cluster (channel, CIDR/service ranges, MetalLB pool, addon list).
- `update-k8s.yml` playbook: `serial: 1`, cordon/drain → `apt full-upgrade` → `snap refresh microk8s --channel=<pinned>` → conditional reboot → uncordon. Same playbook for prd and dev; single-node case skips drain.
- Rebuild flow for k8s VMs: per-VM TF module extended to from-scratch shape, `rebuild-k8s.yml` driving drain → TF apply → role apply → join → uncordon, one node at a time. Rebuild also renames prod nodes to `srvk8s1/2/3`.

Out:

- Ceph CSI install / config (HelmCharts owns).
- Ceph users, pools, fs subvolumes (Phase 7).
- Application-level CoreDNS rewrites (HelmCharts).
- Helm-deployed registry pod, application IngressRoutes (HelmCharts).
- microceph install on the Ceph nodes (Phase 5; k8s nodes only get the *client* side here).
- DNS reservation Terraform resource (Phase 9). Each rebuild's MAC change is a hand-edit of the dnsmasq deployment for now.
- Self-hosted Jenkins agent and CI-triggered runs (Phase 10). The phase is operator-triggered throughout.

## Deliverables

- `ansible/roles/microk8s/` — install + configure + idempotent join. Reads channel, CIDRs, MetalLB pool, addon list, ceph-client toggle, capability labels from inventory.
- `ansible/playbooks/update-k8s.yml` — drain-aware rolling upgrade.
- `ansible/playbooks/rebuild-k8s.yml` — drain → TF replace → role apply → uncordon, `serial: 1`.
- `terraform/scratch/` — refactored to use `modules/managed-vm` with a `vms` map, hosting `wrkscratchk8s1` and `wrkscratchk8s2`. Replaces the existing single-VM `wrkscratch` config.
- `terraform/prd/vms.tf` — three new entries (`srvk8s1/2/3`) at from-scratch shape; `wrkdevk8s` added (also from-scratch shape, currently absent from TF). Existing `srvk8sl1/ss1/ss2` entries removed at rebuild time, one per commit. Deterministic MACs, VMIDs in 900-range, all `bios = "ovmf"`. `srvk8s1` declares the NVMe passthrough block (Ansible reattaches via `qm set`).
- Inventory: `group_vars/k8s.yml` (defaults shared across clusters), `group_vars/k8s_prd.yml` / `group_vars/k8s_dev.yml` (CIDRs, channel, MetalLB pool, IPv6). Per-host `host_vars/srvk8s{1,2,3}.yml` and `host_vars/wrkdevk8s.yml` carry `vm_id`, `pve_node`, `workload_class`, and `k8s_node_labels`.
- `docs/runbooks/k8s-upgrade.md` — operator procedure for `update-k8s.yml`, including rollback (`snap revert microk8s`) and the kernel-pin caveat from Obsidian (the 6.8.0-57 ip6tables regression that forced 6.8.0-55 last cycle — keep the pin until microk8s tracks past it).
- `docs/runbooks/k8s-rebuild.md` — concrete rebuild procedure per node, including the `srvk8s1` ZFS passthrough reattach and the prod hostname rename (dnsmasq updates, known_hosts cleanup). Replaces the forward-looking k8s sections in `vm-rebuild.md`.
- `docs/decisions.md` updates — landed alongside this phase doc: "k8s version policy," "k8s node capability labels," tool-split clarification, KubernetesConfig absorption note.

## Sequencing

Each step leaves the repo in a usable state. No step depends on the next landing.

1. **Replace scratch with two scratch k8s VMs.** Operator destroys `wrkscratch`. Refactor `terraform/scratch/` to use `managed-vm` + a `vms` map; declare `wrkscratchk8s1` (vm_id=901) and `wrkscratchk8s2` (vm_id=902), both 4 GB / 2 cores. Operator applies; cluster bootstrap is exercised against the first VM. Inventory: `inventories/scratch/hosts.yml` updated (drop `wrkscratch`, add the two new hosts).
2. **Build the `microk8s` role on `wrkscratchk8s1`.** Single-node first: kernel modules, ceph-common, snap install at the pinned channel, `.microk8s.yaml`, Calico autodetect, addon enablement, MetalLB pool, capability labels, group membership. End state: a fresh scratch VM goes from cloud-init → managed → microk8s-Ready in one `site.yml` invocation; `--check --diff` rerun is zero-residual.
3. **Multi-node join exercise on `wrkscratchk8s2`.** Verify the join path is idempotent — re-running the role against an already-joined node is a no-op; a fresh second node finds the existing cluster and joins. Tear down the scratch cluster after; the role is the artifact.
4. **Adopt the live clusters at Ansible level (additive, no destructive change).** Add `microk8s` to `site.yml` for `k8s_prd` + `k8s_dev`. Run `--check --diff` against `wrkdevk8s` and the three prod nodes. Apply only the *additive* changes: install missing labels (`homelab.local/performance=high`, `homelab.local/storage=zpool2` on `srvk8sl1`), reconcile the MetalLB IPAddressPool spec, codify the `.microk8s.yaml` file. Leave the legacy `size=` labels and the `PreferNoSchedule` taint **in place** — those come off after HelmCharts is migrated.
5. **Operator migrates HelmCharts off `size=`.** Out-of-band repo work in `/work/HelmCharts`: switch `node-affinity.require-large` to require `homelab.local/storage=zpool2` (for hostPath workloads) or `homelab.local/performance=high` (for Jenkins agents, Plex). Drop `node-affinity.allow-large` entirely. Verify pods still schedule correctly with both label sets present.
6. **Drop legacy labels + taint.** Once HelmCharts no longer references `size=`, the role removes the legacy labels and the `PreferNoSchedule` taint from the live cluster. End state: live cluster carries only the new label scheme. Cluster is now in the target shape pre-rebuild.
7. **Build and exercise `update-k8s.yml`.** First against `wrkdevk8s` (single-node, no drain — useful smoke). Then against `k8s_prd` with `serial: 1` and a real drain/uncordon cycle. Prod is already on `1.32/stable`, so the snap refresh is a no-op; the playbook still exercises drain/cordon. Dev is on `1.30/stable` and gets upgraded to `1.32/stable` here unless we choose to rebuild straight from 1.30 (see step 10).
8. **Extend the TF modules to the from-scratch shape.** One commit per VM, in a deliberate order (cloud-init snippet, `tls_private_key`, `local_file` for `known_hosts.d/`, deterministic MAC, VMID rotation into the 900-range, all `bios = "ovmf"`). `srvk8s1` retains the NVMe passthrough block; the rebuild flow drops it pre-replace and re-adds post-import. No `terraform apply` yet — the commits sit ready.
9. **Rebuild prod, one node at a time.** `rebuild-k8s.yml -l <node>` per node. Manual pre-step per VM: update the dnsmasq deployment with the new hostname (`srvk8s1/2/3`) and new MAC. Operator runs the playbook; Ansible drains the old name, calls TF (which creates the new VM with the new name and destroys the old), calls back into `site.yml`, reattaches passthroughs (`srvk8s1` only), waits for Ready, uncordons. Verify zero-residual against the rebuilt node before moving on.
10. **Rebuild `wrkdevk8s`.** Same flow, single-node — no drain, no rename. New CIDRs (`172.18.0.0/16` / `172.19.0.0/16`) replace the live `10.3.0.0/16`. `core/ingress` does not get re-enabled. Closes the parity event for `k8s_dev`.

After step 10: every k8s host is from-scratch built off the role, on the pinned LTS channel, with a tested upgrade and rebuild path. Hostnames are clean (`srvk8s1/2/3` + `wrkdevk8s`). Capability labels carry semantics; HelmCharts charts pin via affinity by capability.

## Constraints carried forward (Phase 3a → 4)

- **Per-VM TF module shape extension.** The six prod-grade VMs are imported in adoption shape (`terraform/prd/vms.tf`). Phase 4 owns the rework for the three k8s entries (which become `srvk8s1/2/3`); Phase 5 owns the three Ceph entries.
- **Hostname rename at rebuild.** `srvk8sl1 → srvk8s1` (on `pve`), `srvk8ss1 → srvk8s2` (on `pve1`), `srvk8ss2 → srvk8s3` (on `pve2`). The rename is rebuild-time only — the new VM with the new name replaces the old. Old `host_vars/srvk8s{l1,s1,s2}.yml` files get deleted; new `host_vars/srvk8s{1,2,3}.yml` files take over. `wrkdevk8s` keeps its name.
- **`srvk8ss2` BIOS flip.** Currently `bios = "seabios"`. The rename + rebuild flips to `ovmf` with EFI disk in the same commit. Operator memory: `project_srvk8ss2_uefi.md`.
- **`srvk8sl1` ZFS passthrough.** The Samsung 980 NVMe at `nvme1n1` carries `zpool2`. TF cannot recreate the passthrough block (decisions.md "Disk passthrough on managed VMs"). Rebuild path: drop `passthrough_disks` from the new `srvk8s1` module before `apply -replace`; reattach via `qm set` from the role; `zpool import zpool2` runs on first boot from existing pool metadata. Re-add the block to the module post-import so plan reflects reality.
- **Manual dnsmasq reservation per rebuild.** Until Phase 9, every (hostname, MAC, IP) change is a hand-edit of the dnsmasq deployment before `terraform apply`. Operator handles the new `srvk8s1/2/3` reservations + the existing `wrkdevk8s` MAC change. (Operator has already added the two scratch reservations: `wrkscratchk8s1=10.1.0.35`, `wrkscratchk8s2=10.1.0.36`.)
- **Inventory contract.** `vm_id`, `pve_node`, `workload_class`, `k8s_node_labels` declared per host in `host_vars/<host>.yml`. The `proxmox_host` role on `pve` enumerates VMs whose `pve_node` matches itself and reconciles affinity from the new VMIDs automatically.

## Live state vs. target state

Drift surfaced during phase planning, codified into the role's target state — not preserved as-is.

| What | Live (today) | Target |
|---|---|---|
| Channel (prd) | `1.32/stable` | `1.32/stable` ✓ |
| Channel (dev) | `1.30/stable` | `1.32/stable` |
| Cluster CIDR (prd) | `172.16.0.0/16` | `172.16.0.0/16` ✓ |
| Cluster CIDR (dev) | `10.3.0.0/16` | `172.18.0.0/16` |
| Calico mode | VXLAN | VXLAN ✓ |
| Calico autodetect | env shows `bgp` cluster-type | drop the env; codify `interface=ens18` |
| MetalLB pool (prd) | `10.2.1.1-10.2.1.199` (+ IPv6) | same ✓ |
| MetalLB pool (dev) | `10.1.2.1-10.1.2.199` (+ IPv6) | same ✓ |
| `core/ingress` (dev) | enabled | disabled |
| `core/registry` (prd) | enabled | disabled |
| `size=large` label, `PreferNoSchedule` taint | on `srvk8sl1` | removed (after HelmCharts migration, step 6) |
| `size=small` label | on `srvk8ss2` only — `srvk8ss1` lacks it | irrelevant; both labels go away |
| `srvk8sl1` `nvme1n1` passthrough → `zpool2` | ONLINE, 271G/464G used | preserved through rebuild via reattach + `zpool import` |
| Registry mirror dirs (`/var/snap/microk8s/current/args/certs.d/`) | empty on srvk8sl1 | not codified (operator runs own registry; mirror config was a stale test) |

## Cluster-state details to preserve across rebuild

What the role must reproduce or the operator must reapply by hand. Anything *not* on this list is deliberately dropped at rebuild.

Codified by the role:

- `.microk8s.yaml` (CIDR + extraSANs).
- Calico `IP_AUTODETECTION_METHOD=interface=ens18`, applied via patch on the live `cni.yaml` daemonset env.
- Kernel modules `nf_conntrack`, `ceph`, `rbd`, `nbd` via `/etc/modules-load.d/`.
- Ceph client tools (`ceph-common`, `rbd-nbd`).
- `pvginkel` group membership for `microk8s`, `~/.kube` ownership.
- Addon enablement: community, dashboard, dns, ha-cluster, helm, helm3, metallb, metrics-server.
- MetalLB `IPAddressPool` + `L2Advertisement` spec (per cluster).
- Capability labels (`homelab.local/performance=high`, `homelab.local/storage=zpool2`) via `kubectl label` from one CP node, idempotent guard on existing labels.

Explicitly *not* codified (HelmCharts territory or stale):

- Application CoreDNS rewrites (`webathome.org`, `ginbov.nl` zones, `registry` alias).
- Helm-deployed registry pod and any `/etc/hosts` entry pointing to it.
- `core/ingress` (operator runs own).
- `core/registry` (operator runs own).
- Registry mirrors under `certs.d/` (no longer used).

Out-of-band per rebuild:

- `srvk8s1` ZFS pool on `nvme1n1` survives on disk; reattach via `qm set` + `zpool import zpool2` on first boot. The systemd `zfs-import-cache.service` does the right thing once `/etc/zfs/zpool.cache` is regenerated. The rebuild playbook performs an explicit `zpool import` step rather than relying on import-cache.

## What this phase deliberately does not solve

- **OpenBao integration.** k8s nodes need no OpenBao knowledge; CSI secrets, ESO, and so on are HelmCharts territory and arrive in Phase 6.
- **Cluster autoscaling, NFD, gpu-operator, anything addon-shaped beyond the listed set.** Add later if a workload needs it.
- **MetalLB BGP.** Tabled completely — not preserved in the role as a templated alternative. Revisit fresh when the operator wants to actually exercise it.
- **Backup of `etcd` / dqlite.** microk8s HA on dqlite has its own snapshot mechanism; we lean on the cluster-vzdump from `proxmox_host` (Phase 2) for VM-level backup. Cluster-state backup beyond that is a Phase 10 follow-up if it earns its keep.
- **Drift detection.** `--check --diff` is the manual mechanism. CI-scheduled drift runs land in Phase 10.
