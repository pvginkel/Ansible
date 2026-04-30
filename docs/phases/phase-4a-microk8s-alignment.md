# Phase 4a — microk8s alignment + upgrade

**Status**: ✅ Done

## Goal

Take the microk8s role from "exercised on scratch" (Phase 4) to "every k8s host adopted under Ansible, on the verified upgrade path, with all role-side drift reconciled." Complete the role's missing reconciliation pieces (capability labels, MetalLB IPAddressPool), adopt the live prod and dev clusters additively, drive the HelmCharts label migration to a clean slate, remove the legacy `size=*` labels + taint, and deliver the drain-aware upgrade playbook. Cluster ends up in target shape pre-rebuild — the actual VM rebuild lands in Phase 4b.

## What landed

The `microk8s` role grew capability-label and MetalLB IPAddressPool/L2Advertisement reconciliation, the `metallb` addon's IP-range arg got hidden behind a sentinel inside the role, the Calico CLUSTER_TYPE drift got patched, and the addon's `default-addresspool` is now upserted in place by the role rather than coexisting with a parallel role-owned pool.

Inventory: per-cluster `group_vars/k8s_prd.yml` and `group_vars/k8s_dev.yml` carry channel, CIDRs, MetalLB pool, addons, primary host, users. `srvk8sl1`'s `host_vars` carries `homelab.local/performance=high` + `homelab.local/storage=zpool2`. `wrkdevk8s` has a transitional `microk8s_channel: 1.30/stable` override (lifted at the Phase 4b rebuild). `playbooks/microk8s.yml` retired into `site.yml`'s third play.

Adoption: the four k8s nodes were brought under Ansible via `adopt.yml` (host keys → `files/known_hosts.d/k8s_prd` and `k8s_dev`; bootstrap + baseline applied). HelmCharts migrated off `size=*` to `homelab.local/*` affinity; the legacy `size=large/small` labels and the `PreferNoSchedule` taint are gone from the live nodes.

Upgrade workflow: `playbooks/update-k8s.yml` (drain → snap refresh → apt full-upgrade → reboot if needed → uncordon, `serial: 1`, single-node skip) plus `playbooks/refresh-k8s-addons.yml` (`microk8s addons repo update core` + disable/enable cycle + role's metallb reconcile, primary-only). Both backed by `docs/runbooks/k8s-upgrade.md`. The version-skip pre-flight in `update-k8s.yml` refuses jumps of more than one minor.

`refresh-k8s-addons.yml` is committed but unsmoked — operational acceptance lands at the next maintenance window, not as a closure prerequisite.

## What did NOT land here

The TF rework, `rebuild-k8s.yml`, and the actual VM rebuilds (originally steps 8–11 in this doc) moved to **Phase 4b** ([phase-4b-microk8s-rebuild.md](phase-4b-microk8s-rebuild.md)). The cluster sits in target shape pre-rebuild — `homelab.local/*` semantics correct, channel + CIDR + addon-list discrepancies on `wrkdevk8s` settle at the dev rebuild in 4b.
