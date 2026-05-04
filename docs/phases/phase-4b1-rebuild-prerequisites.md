# Phase 4b1 — Rebuild prerequisites (registry, CoreDNS, ZFS)

**Status**: ⏳ Planned

## Goal

Close the bring-up gaps the live nodes carry as out-of-band hand-edits, so a freshly cloud-init'd k8s VM under Phase 4c rebuild reaches a fully functional steady state from `rebuild-k8s.yml` alone — no manual `kubectl edit cm coredns`, no manual `tee` into `/var/snap/microk8s/current/args/certs.d/`, no missing `zpool` binary.

Per `docs/decisions.md` "Tool split," all of this is Ansible's: registry mirror config, CoreDNS rewrites for cluster-internal names, kernel-module/package prerequisites for storage. The work was implicit in Phase 4 but has not yet landed in any role.

## Scope

In:

1. **Containerd registry-mirror config** — `/var/snap/microk8s/current/args/certs.d/<registry>/hosts.toml` for `registry:5000` and `registry-dev:5000`, per `KubernetesConfig/installatie/01-server-prep.md`. Lands in the `microk8s` role; the file set is driven by an inventory variable so prd and dev can carry different mirrors. The role notifies a handler that runs `microk8s stop && microk8s start` on change.
2. **CoreDNS hosts override** — adds the `hosts { 172.17.0.3 registry registry.home; fallthrough }` block to the `coredns` ConfigMap in `kube-system`, with a rollout-restart of the deployment on change. Reconciled on the cluster primary, alongside the existing addon and label tasks.
3. **Node-local `/etc/hosts` entry** — `172.17.0.3 registry` on every k8s node, so the snap's containerd resolves the alias before CoreDNS is even up (chicken-and-egg during cluster boot). Standard `ansible.builtin.lineinfile` against `/etc/hosts`.
4. **`daemon.json` runtime config** — the `runtimes.runc` block from `KubernetesConfig/docker/daemon.json` (`--default-runtime --allow-shared-mounts`). **Host-class TBD**: this is Docker daemon config and microk8s uses containerd, so it does not apply to k8s nodes as-is. Identify which host actually consumes it (`wrkdev`? a Docker host outside the inventory? a Jenkins agent?) before wiring it into a role. Carried into this phase as a checklist item; placement decided at implementation time.
5. **`zfsutils-linux` package** — added to `baseline_extra_packages` for the k8s group (both `k8s_prd.yml` and `k8s_dev.yml`, since there is no shared `k8s.yml` group_vars file today). Today `rebuild-k8s.yml:32,46` calls `zpool list` / `zpool import` directly with no preceding install; on a freshly cloud-init'd Ubuntu image `zpool` does not exist. Resource cost on hosts with no pools is ~25 MB disk and ~50 MB RAM (steady-state, no growth) — noise on 8 GB+ k8s VMs, so installing on every k8s node is simpler than per-host gating and means any future node that gets a passthrough pool is already provisioned.

Out:

- The Ceph user/pool/CephFS-subvolume bootstrap from `KubernetesConfig/installatie/00-prepare-ceph.md`. That is Phase 7's scope ("Storage — Ceph resources + CSIs").
- CSI driver installs themselves. Phase 7.
- Anything beyond what a freshly rebuilt k8s VM needs to be Ready and able to pull images.

## Sequencing

1. Land the role/inventory changes against `inventories/scratch` first; verify `--check --diff` reports `changed=0` once applied and a re-run is clean.
2. Re-run the `microk8s` role against the live `inventories/prd` k8s nodes additively. The first run reconciles the certs.d files, the CoreDNS ConfigMap, the `/etc/hosts` line, and (for the k8s group) installs `zfsutils-linux`. Expected: small `changed` count corresponding to the items each live node is missing. Subsequent runs are clean.
3. Same against `inventories/dev`.
4. Once 4b1 is green on both clusters, Phase 4c can proceed knowing the role brings new VMs up to a fully functional state.

## Dependencies + risks

- **CoreDNS reconciliation idempotence**: editing `kube-system/coredns` ConfigMap from Ansible needs to match microk8s's stock format closely enough that re-runs don't show false drift. Use a YAML-aware approach (parse, mutate the `Corefile` value, re-emit) rather than naive string replace. Verify with at least three back-to-back applies on scratch.
- **Registry alias collision**: `172.17.0.3` is in the `172.17.0.0/16` service-CIDR (per `inventories/prd/group_vars/k8s_prd.yml`). The address is hand-picked from outside the dynamic allocation range; if we ever expand allocations to overlap, the alias breaks. Document the reservation in the role's defaults so it's discoverable.
- **`microk8s stop && start` handler**: needed for certs.d changes to be picked up by containerd. This is a brief in-cluster outage on the affected node — fine on scratch and during rebuilds, but if 4b1 is applied to live clusters before 4c runs, the handler triggers once per node as the certs.d files first land. Acceptable, but operators should be aware.
- **`zfsutils-linux` install timing**: installing the package on the live srvk8sl1 is a no-op (already installed manually). On the live srvk8ss1/srvk8ss2/wrkdevk8s the package install fires once; nothing should depend on the kernel module being absent.

## Pointers

- Source material: `/work/KubernetesConfig/installatie/01-server-prep.md`, `02-cluster-setup.md`, `docker/daemon.json`.
- Decisions: `docs/decisions.md` lines on tool split (Ansible owns "registry mirror config" and "CoreDNS rewrites that resolve cluster-internal names").
- Continuation: [`phase-4c-microk8s-rebuild-execution.md`](phase-4c-microk8s-rebuild-execution.md).
