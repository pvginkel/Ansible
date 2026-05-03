# Phase 4c — k8s VM rebuild execution

**Status**: ⏳ Planned

## Goal

Drive the four k8s VM rebuilds that Phase 4b staged. Closes the parity event for `k8s_prd` and `k8s_dev` per `docs/decisions.md` "Adoption is a waypoint; rebuild is the parity event."

## Inputs from Phase 4b

The repo is staged; nothing has been applied:

- TF entries under from-scratch shape: `srvk8s1` (910), `srvk8s2` (911), `srvk8s3` (912), `wrkdevk8s` (919).
- managed-vm module: `cloud_init` + `machine` inputs, `lifecycle.ignore_changes` for `disk[0].file_id`.
- prd-level scaffold: image download per `pve_node`, `tls_private_key` per from-scratch VM, cloud-init snippet, `local_file` writing `files/known_hosts.d/prd` (gated, materialises on first apply).
- `ansible.cfg`'s `UserKnownHostsFile` lists `files/known_hosts.d/prd` at the head.
- `rebuild-k8s.yml` drives the post-TF Ansible work.
- `docs/runbooks/k8s-rebuild.md` is the operator orchestrator.
- Phase 4a smoke for `update-k8s.yml` against scratch passed (real run, no `--check`) so the snap-refresh rework is verified end-to-end.

## Decisions carried forward

- **Hostname rename at rebuild**: `srvk8sl1 → srvk8s1` (on `pve`), `srvk8ss1 → srvk8s2` (on `pve1`), `srvk8ss2 → srvk8s3` (on `pve2`). `wrkdevk8s` keeps its name.
- **`srvk8s1`'s NVMe passthrough**: TF attaches `/dev/disk/by-id/nvme-Samsung_SSD_980_500GB_S64DNX0RC21332X` at scsi2 atomically with VM creation (declared in `vms.tf` per plan 01); `zpool import zpool2` runs from `rebuild-k8s.yml`.
- **Manual dnsmasq reservations** until Phase 9. Each (hostname, MAC, IP) update is a hand-edit before `terraform apply`.
- **Channel override on `wrkdevk8s`** (`1.30/stable` in `host_vars`) is removed at the wrkdevk8s rebuild so `group_vars/k8s_dev.yml`'s `1.32/stable` takes effect.
- **Adoption known_hosts files** (`k8s_prd`, `k8s_dev`) retire after all four nodes are on TF-managed keys.

## Scope

In:
- Smoke `rebuild-k8s.yml` against the scratch fleet (rebuild `wrkscratchk8s2` while `wrkscratchk8s1` stays Ready).
- Real rebuilds in order: `srvk8s1` (with passthrough), `srvk8s2`, `srvk8s3`, `wrkdevk8s`.
- Per-rebuild inventory rename commits: `host_vars/srvk8sl1.yml → srvk8s1.yml` etc.; `hosts.yml` entries; `host_vars/srvk8s1.yml` adds `zpools_to_import`; `group_vars/k8s_prd.yml`'s `microk8s_primary_host` updates at srvk8s1's rebuild.
- `host_vars/wrkdevk8s.yml` update: drop the `microk8s_channel` override, bump `vm_id` 119 → 919.
- Manual destroy of old VMs (`qm destroy`) and TF state cleanup of orphans (`terraform state rm`).
- End-of-phase commit: retire `files/known_hosts.d/k8s_prd` and `k8s_dev`; drop them from `ansible.cfg`.

Out:
- Microceph rebuilds (Phase 5).
- DNS reservation TF resource (Phase 9). dnsmasq updates remain manual.
- Self-hosted Jenkins agent + CI-driven runs (Phase 10).
- HelmCharts redeploy on `wrkdevk8s` after rebuild — operator workflow, separate repo.

## Sequencing

The runbook ([`docs/runbooks/k8s-rebuild.md`](../runbooks/k8s-rebuild.md)) is the operator playbook. High-level:

1. Smoke against scratch.
2. `srvk8s1` (the trickiest — ZFS reattach + cluster primary handoff).
3. `srvk8s2`.
4. `srvk8s3`.
5. `wrkdevk8s`.
6. Close-the-parity-event commit (retire adoption known_hosts files).

## Recovery + risks

- **dqlite quorum risk during prd rebuild**: with two voters left while rebuilding the third, a second failure during the window risks quorum. Mitigation: serial rebuilds with `microk8s status` health checks between each; rebuild prd in a maintenance window.
- **HelmCharts hostPath workloads on `srvk8s1`**: `homelab.local/storage=zpool2` workloads (storage chart, Prometheus) are pinned via required affinity. While `srvk8s1` is being rebuilt those pods are unschedulable. Acceptable for the rebuild window; document expected unavailability in the maintenance announcement.
- **dnsmasq reservation drift**: forgetting to update the reservation before `terraform apply` lands the new VM with no DHCP lease at its expected IP, breaking host-key verification + role apply. Runbook checklist covers it.
- **TF apply destroying everything at once**: with all four entries renamed in `vms.tf` simultaneously, an unscoped `terraform apply` proposes destroying all four old VMs and creating four new ones. Always scope with `-target='module.vm["<name>"]'`. Runbook commands include the targeting.

## Live state vs. target state

The drift items still settled by phase 4c (everything else closed in 4a/4b):

| What | Live (today) | Target |
|---|---|---|
| Channel (dev) | `1.30/stable` (pinned via host_vars) | `1.32/stable` from group_vars |
| Cluster CIDR (dev) | `10.3.0.0/16` | `172.18.0.0/16` |
| Service CIDR (dev) | `10.153.183.0/24` | `172.19.0.0/16` |
| `core/ingress` (dev) | enabled | disabled |
| VMIDs | 103/104/107/119 | 910/911/912/919 |
| MAC addresses | Proxmox-generated `BC:24:11:...` | deterministic `02:A7:F3:VV:VV:00` |
| Hostnames (prd) | `srvk8sl1/ss1/ss2` | `srvk8s1/2/3` |
| BIOS (`srvk8ss2`) | `seabios` | `ovmf` |
| TF state | adoption shape (only OLD VMs imported) | from-scratch shape |
| `srvk8sl1`'s `zpool2` | ONLINE on `nvme1n1` | preserved through rebuild via reattach + `zpool import` |
| Host keys | `files/known_hosts.d/k8s_prd`, `k8s_dev` (adoption) | `files/known_hosts.d/prd` (TF-owned) |

## What this phase deliberately does not solve

- Cluster autoscaling, NFD, gpu-operator, anything addon-shaped beyond the configured set.
- MetalLB BGP. Tabled.
- etcd / dqlite snapshot backup beyond vzdump. Phase 10 follow-up if needed.
- CI-scheduled drift detection. Phase 10.
- DNS reservation TF resource. Phase 9; until then dnsmasq updates are manual.
