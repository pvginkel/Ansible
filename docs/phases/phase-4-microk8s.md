# Phase 4 — microk8s roles and upgrade

**Status**: ⏳ Planned

## Goal

Bring every k8s VM under Ansible-owned cluster lifecycle: install, join, HA, upgrade, rebuild. After this phase a fresh k8s node is reproducible from inventory + `site.yml`, and a routine OS or microk8s upgrade is one playbook invocation against `k8s_prd` (or `k8s_dev`) with serialized cordon/drain/reboot/uncordon. The phase ends when the three production nodes have been **rebuilt** from scratch — the parity event per `docs/decisions.md` "Adoption is a waypoint; rebuild is the parity event."

## Source material

`/work/Obsidian/Kubernetes.md` is the procedural runbook to port. Caveats:

- Dutch headings and inline notes; the imperative shell snippets are the parts to lift.
- The doc carries **two sets of CIDRs** (`OUD` / `NIEUW`). Use the `NIEUW` blocks — `OUD` is the pre-renumber state and is no longer in service. Prod cluster: `172.16.0.0/16` cluster CIDR, `172.17.0.0/16` service CIDR. Dev cluster: `172.18.0.0/16` / `172.19.0.0/16`.
- Several sections end with "Is verplaatst naar HelmCharts" / "naar KubernetesConfig" — those are workload concerns and stay out of Ansible's scope (CSI install, IPAddressPool spec, CoreDNS rewrites, registry pod). The Ansible role only owns what the cluster needs to be *ready to host* those workloads.
- The Calico section toggles BGP mode. That was tied to the abandoned MetalLB-BGP attempt (per `decisions.md` "Network topology"); default to Calico's stock VXLAN unless we deliberately re-open BGP. Not a Phase 4 decision to make implicitly — see "Decisions to lock down" below.

## Scope

In:

- `microk8s` role: kernel modules, prerequisite packages (Ceph client tooling), snap install pinned to a channel, `.microk8s.yaml` (CIDR + extraSANs), Calico `IP_AUTODETECTION_METHOD` for `ens18`, addon enablement (community, dns, dashboard, metallb shell), registry mirrors, group membership + `kubectl` alias, taints/labels, idempotent join.
- Inventory data per cluster (channel, CIDR/service ranges, registry mirror list, node role/label set).
- `update-k8s.yml` playbook: `serial: 1`, cordon/drain → `apt full-upgrade` → `snap refresh microk8s --channel=<pinned>` → conditional reboot → uncordon. Same playbook covers `k8s_prd` and `k8s_dev`; the single-node case skips drain.
- Rebuild flow for k8s VMs end-to-end: per-VM TF module extended to the from-scratch shape (Phase 3a "Carried forward"), `rebuild-k8s.yml` playbook that drives drain → TF apply → role apply → join → uncordon, one node at a time. The cluster-member section of `docs/runbooks/vm-rebuild.md` becomes concrete.
- Rebuild executed against the three prod nodes (`srvk8sl1`, `srvk8ss1`, `srvk8ss2`) and against `wrkdevk8s` — see "Decisions to lock down" for the dev-cluster carve-out.

Out:

- Ceph CSI install / config (HelmCharts owns; `decisions.md` "Tool split").
- Ceph users, pools, fs subvolumes (Phase 7).
- MetalLB `IPAddressPool` spec — the addon ships, the pool definition is HelmCharts.
- CoreDNS rewrites for registry / external domains (HelmCharts).
- Container registry pod (HelmCharts).
- microceph install on the Ceph nodes (Phase 5; the k8s nodes only get the *client* side of Ceph here).
- DNS reservation Terraform resource (Phase 9). Each rebuild's MAC change is a hand-edit of `static-hosts.yaml` for now.
- Self-hosted Jenkins agent and CI-triggered runs (Phase 10). The phase is operator-triggered throughout.

## Deliverables

- `ansible/roles/microk8s/` — install + configure + idempotent join. Reads channel, CIDRs, addon list, registry mirrors, ceph-client toggle, taints/labels from inventory. Joins via the existing cluster's `microk8s add-node` token; first-node bootstrap is a one-shot guarded by a "is this node already in `microk8s status` cluster output" check.
- `ansible/playbooks/update-k8s.yml` — drain-aware rolling upgrade.
- `ansible/playbooks/rebuild-k8s.yml` — drain → TF replace → role apply → uncordon, `serial: 1`. Calls back into `site.yml` for the role apply step rather than duplicating it.
- `terraform/prd/vms.tf` (and supporting per-VM resources) — the three k8s VMs flipped from adoption shape to from-scratch shape. New deterministic MACs, VMIDs rotated into the 900-range, `srvk8ss2` flipped to `bios = "ovmf"` with EFI disk, `srvk8sl1`'s `nvme1n1` passthrough block removed (Ansible reattaches).
- Inventory: `group_vars/k8s.yml` (defaults shared across both clusters), `group_vars/k8s_prd.yml` / `group_vars/k8s_dev.yml` (per-cluster CIDRs, channel, registry mirrors, MetalLB shell). Per-host `host_vars/<srvk8s*>.yml` gains the new VMID and any node-local labels/taints.
- `docs/runbooks/k8s-upgrade.md` — operator procedure for `update-k8s.yml`, including rollback (`snap revert microk8s`) and the kernel-pin caveat from Obsidian (the 6.8.0-57 ip6tables regression that forced 6.8.0-55 last cycle — keep the pin until microk8s tracks past it).
- `docs/runbooks/k8s-rebuild.md` — concrete rebuild procedure per node, including the `srvk8sl1` ZFS-passthrough reattach and the `srvk8ss2` BIOS flip caveats. Replaces the forward-looking "k8s and Ceph cluster members" section in `vm-rebuild.md`.
- Updates to `docs/decisions.md`: a "k8s version policy" subsection mirroring the existing "Ceph version policy" — LTS-only, channel pinned in inventory.
- `wrkdevk8s` modeled in Terraform (under `terraform/prd/vms.tf`) at the from-scratch shape, with VMID rotation. Adds it to the rebuild path.

## Sequencing

Each step leaves the repo in a usable state. No step depends on the next landing.

1. **Role on a scratch microk8s VM.** Stand up a single-node microk8s on `wrkscratch` (or a sibling scratch VM if we want to preserve `wrkscratch` for non-k8s exercise). Build the `microk8s` role against it. End state: a fresh scratch VM goes from cloud-init → managed → microk8s-Ready in one `site.yml` invocation, and a `--check --diff` rerun is zero-residual.
2. **Multi-node join exercise.** Spin up a second scratch k8s VM. Verify the join path is idempotent — re-running the role against an already-joined node is a no-op, and a fresh second node finds the existing cluster and joins. Tear down the scratch cluster; the role is the artifact.
3. **Adopt the live clusters at Ansible level (no destructive change).** Add `microk8s` to `site.yml` for `k8s_prd` + `k8s_dev`. Run `--check --diff` against `wrkdevk8s` and the three prod nodes. Reconcile drift the role surfaces — codify into the role if the live state is correct, or note as deliberate divergence to flatten at rebuild. Outcome: every k8s host is under role coverage even before the rebuild.
4. **Build and exercise `update-k8s.yml`.** First against `wrkdevk8s` (single-node, no drain — useful smoke). Then against `k8s_prd` with `serial: 1` and a real drain/uncordon cycle. Pin the target microk8s channel against current state per the version-policy subsection.
5. **Run the upgrade against `k8s_prd`.** Land the cluster on the chosen LTS channel under the new playbook. Soak under real workload. This step is intentionally before rebuild — same rationale as Ceph (decisions.md "Ceph rebuild path"): rebuild has no rollback, upgrade does. Doing the well-understood step first means rebuild starts from a verified baseline.
6. **Extend the TF modules to the from-scratch shape.** One commit per VM, in the order Phase 3a outlined (cloud-init snippet, `tls_private_key`, `local_file` for `known_hosts.d/`, deterministic MAC, VMID rotation into the 900-range, `passthrough_disks` removal). `srvk8ss2` also flips bios in the same commit. No `terraform apply` yet — the commits sit ready.
7. **Rebuild prod, one node at a time.** `rebuild-k8s.yml -l <node>` per node, in worker → control-plane order if HA disposition makes that meaningful (see "Decisions to lock down"). Manual pre-step per VM: update the dnsmasq reservation to the new MAC (and new IP if the rotation moves it). Operator runs the playbook; Ansible drains, calls TF, calls back into `site.yml`, reattaches passthroughs (srvk8sl1), waits for Ready, uncordons. Verify zero residual against the rebuilt node before moving to the next.
8. **Rebuild `wrkdevk8s`.** Same flow, single-node — no drain, but TF replace + role apply + reattach (none on this VM today). Closes the parity event for `k8s_dev`.

After step 8: every k8s host is from-scratch built off the role, on a known LTS channel, with a tested upgrade and rebuild path.

## Decisions to lock down

Surface these explicitly at the start of the phase rather than letting them slide in implicitly.

1. **microk8s channel.** Pick the current LTS-equivalent channel (`1.32-strict/stable` or whatever the operator picks against the state of microk8s at phase start). Pin in inventory per cluster — both clusters can move independently if we ever want to soak a new version in dev first. Document the pick in `docs/decisions.md` under a new "k8s version policy" subsection.
2. **HA topology.** Default microk8s 1.19+ promotes joined nodes to control-plane automatically once three are joined. With `srvk8sl1` (large) + `srvk8ss1`/`srvk8ss2` (small), is the target three full control-plane voters, or one CP + two workers? The Obsidian taint/label scheme (`size=large:PreferNoSchedule` + `size=small`) is a soft scheduling hint, not a CP/worker split — implies all three are CP. Confirm before the rebuild step.
3. **Calico mode.** Default VXLAN unless we want BGP back. Today's live cluster's mode is the answer, modulo whether we want to change it during the rebuild. Default recommendation: leave at VXLAN; the BGP-with-MetalLB attempt is shelved per `decisions.md`.
4. **MetalLB pool.** Ansible enables the addon (`microk8s enable metallb`); the `IPAddressPool` spec is HelmCharts territory (per the "Is verplaatst naar KubernetesConfig" notes in Obsidian). Confirm — alternative is to ship the spec from Ansible too, which crosses the "Helm owns workloads" line.
5. **`wrkdevk8s` rebuild.** The decisions matrix lists "k8s VMs" rebuilt in Phase 4. `wrkdevk8s` is in `k8s_dev` so it qualifies, but it's not in TF state today. The right call is to model it under `terraform/prd/vms.tf` in step 6 and rebuild it in step 8. Confirm before step 6.
6. **VMID assignment.** `wrkscratch` is `900`. Pick a stable scheme for the k8s rotation — e.g. `srvk8sl1`→`910`, `srvk8ss1`→`911`, `srvk8ss2`→`912`, `wrkdevk8s`→`919`, leaving `913–918` for future k8s VMs. Locked at step 6 commit time, pinned forever after (deterministic MAC depends on it).

## Constraints carried forward (Phase 3a → 4)

- **Per-VM TF module shape extension.** The six prod-grade VMs are imported in adoption shape (`terraform/prd/vms.tf`). Phase 4 owns the rework for the three k8s entries; Phase 5 owns the three Ceph entries.
- **`srvk8ss2` BIOS flip.** Currently `bios = "seabios"` matching the live VM (per the inline comment in `vms.tf`). The flip lands as part of the rebuild commit — a `terraform apply -replace` on the existing VM will not flip BIOS; only a from-scratch rebuild will. (Operator memory: `project_srvk8ss2_uefi.md`.)
- **`srvk8sl1` ZFS passthrough.** The Samsung 980 NVMe at `nvme1n1` carries the cloud-sync ZFS pool. TF imports it cleanly today but cannot recreate it (decisions.md "Disk passthrough on managed VMs"). Rebuild path: drop the `passthrough_disks` block from the module before `apply -replace`; reattach via `qm set` from the role; `zpool import` runs on first boot from the existing pool metadata on the disk. Re-add the block to the module once the rebuilt VM is up so plan reflects reality.
- **Manual dnsmasq reservation per rebuild.** Until Phase 9, every MAC change is a hand-edit of the operator's `static-hosts.yaml` (or whatever the dnsmasq deployment reads) before `terraform apply`. Same procedure as wrkscratch's first provision.
- **Inventory contract.** `vm_id`, `pve_node`, `workload_class` already declared per host in `host_vars/<srvk8s*>.yml`. The VMID rotation in step 6 updates `vm_id`; affinity reconciliation in `proxmox_host` picks up the new value automatically.

## Cluster-state details to preserve across rebuild

Anything below that *isn't* re-applied by the role at rebuild has to be documented and re-applied manually — or codified into the role first.

- **Custom `.microk8s.yaml`** (CIDR + extraSANs) — codified.
- **Calico `IP_AUTODETECTION_METHOD=interface=ens18`** — codified, applied via patch on the live `cni.yaml` daemonset env.
- **Registry mirrors under `/var/snap/microk8s/current/args/certs.d/`** — codified, list comes from inventory.
- **`pvginkel` group membership for `microk8s` and `~/.kube` ownership** — codified.
- **Kernel modules `nf_conntrack`, `ceph`, `rbd`, `nbd` via `/etc/modules-load.d/`** — codified.
- **Ceph client tools (`ceph-common`, `rbd-nbd`)** — codified.
- **Taints/labels** (`size=large` / `size=small`, `size=large:PreferNoSchedule`) — codified, applied via `kubectl` from one CP node per cluster (idempotent guard: check existing taints/labels before applying).
- **MetalLB enable** — codified (the addon enable). Pool spec is HelmCharts.
- **CoreDNS rewrites for registry/`webathome.org`/`ginbov.nl`** — explicitly *not* codified; HelmCharts.
- **`/etc/hosts` entry for `registry`** — explicitly *not* codified; the dnsmasq DNS path makes this a HelmCharts-side concern once the pod has a stable name.
- **`srvk8sl1` ZFS pool on `nvme1n1`** — survives on disk; reattach + import. The systemd `zfs-import-cache.service` does the right thing once `/etc/zfs/zpool.cache` is regenerated. Possibly worth an explicit `zpool import -a` step in the rebuild playbook.

## What this phase deliberately does not solve

- **OpenBao integration.** k8s nodes need no OpenBao knowledge; CSI secrets, External Secrets Operator, and so on are HelmCharts territory and arrive in Phase 6.
- **Cluster autoscaling, node-feature-discovery, gpu-operator, anything addon-shaped beyond the four listed.** Add later if a workload needs it.
- **Backup of `etcd` / dqlite.** microk8s HA on dqlite has its own snapshot mechanism; we lean on the cluster-vzdump from `proxmox_host` (Phase 2) for VM-level backup. Cluster-state backup beyond that is a Phase 10 follow-up if it earns its keep.
- **Drift detection.** `--check --diff` is the manual mechanism. CI-scheduled drift runs land in Phase 10.
